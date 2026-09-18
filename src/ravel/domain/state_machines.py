"""Legal transitions.

The spec requires that "state transitions must be validated in code". A
transition table is the smallest thing that satisfies that: it is a value that
can be read, tested, and printed, rather than a set of checks scattered across
the methods that happen to change a status.

Nothing here touches the database. This module answers one question — may a
record go from status X to status Y — and callers that also need authority or
contract checks apply those on top.
"""

from __future__ import annotations

from dataclasses import dataclass

from ravel.domain.enums import JoinPolicy, NodeStatus, NodeType, ProjectStatus
from ravel.domain.roles import AgentRole

#: Node status -> the statuses it may move to.
#:
#: The terminal statuses have no outgoing edges. That is the point of the
#: acceptance-criteria freeze: a FAILED node keeps FAILED, and Master opens a
#: new node rather than editing the record of what happened.
NODE_TRANSITIONS: dict[NodeStatus, frozenset[NodeStatus]] = {
    NodeStatus.PLANNED: frozenset(
        {NodeStatus.READY, NodeStatus.BLOCKED, NodeStatus.CANCELLED}
    ),
    NodeStatus.READY: frozenset(
        {
            NodeStatus.RUNNING,
            NodeStatus.WAITING_DECISION,
            NodeStatus.BLOCKED,
            NodeStatus.CANCELLED,
        }
    ),
    NodeStatus.RUNNING: frozenset(
        {
            NodeStatus.WAITING_EXTERNAL,
            NodeStatus.WAITING_DECISION,
            NodeStatus.REVIEWING,
            NodeStatus.PASSED,
            NodeStatus.FAILED,
            NodeStatus.PARTIAL,
            NodeStatus.BLOCKED,
            NodeStatus.CANCELLED,
        }
    ),
    NodeStatus.WAITING_EXTERNAL: frozenset(
        {NodeStatus.RUNNING, NodeStatus.FAILED, NodeStatus.BLOCKED, NodeStatus.CANCELLED}
    ),
    NodeStatus.WAITING_DECISION: frozenset(
        {NodeStatus.READY, NodeStatus.RUNNING, NodeStatus.BLOCKED, NodeStatus.CANCELLED}
    ),
    NodeStatus.REVIEWING: frozenset(
        {
            NodeStatus.PASSED,
            NodeStatus.FAILED,
            NodeStatus.PARTIAL,
            NodeStatus.BLOCKED,
            NodeStatus.CANCELLED,
        }
    ),
    NodeStatus.BLOCKED: frozenset({NodeStatus.PLANNED, NodeStatus.READY, NodeStatus.CANCELLED}),
    NodeStatus.PASSED: frozenset(),
    NodeStatus.FAILED: frozenset(),
    NodeStatus.PARTIAL: frozenset(),
    NodeStatus.CANCELLED: frozenset(),
}

#: The statuses a node can never leave.
TERMINAL_NODE_STATUSES: frozenset[NodeStatus] = frozenset(
    status for status, allowed in NODE_TRANSITIONS.items() if not allowed
)

#: The statuses in which a node occupies a worker.
ACTIVE_NODE_STATUSES: frozenset[NodeStatus] = frozenset(
    {NodeStatus.RUNNING, NodeStatus.WAITING_EXTERNAL, NodeStatus.REVIEWING}
)

#: Project status -> the statuses it may move to.
PROJECT_TRANSITIONS: dict[ProjectStatus, frozenset[ProjectStatus]] = {
    ProjectStatus.CREATED: frozenset(
        {ProjectStatus.CONTRACT_DEFINED, ProjectStatus.CANCELLED}
    ),
    ProjectStatus.CONTRACT_DEFINED: frozenset(
        {ProjectStatus.EXECUTING, ProjectStatus.PAUSED, ProjectStatus.CANCELLED}
    ),
    ProjectStatus.EXECUTING: frozenset(
        {
            ProjectStatus.PAUSED,
            ProjectStatus.COMPLETED,
            ProjectStatus.FAILED,
            ProjectStatus.CANCELLED,
        }
    ),
    ProjectStatus.PAUSED: frozenset(
        {ProjectStatus.EXECUTING, ProjectStatus.CANCELLED, ProjectStatus.FAILED}
    ),
    ProjectStatus.COMPLETED: frozenset(),
    ProjectStatus.FAILED: frozenset(),
    ProjectStatus.CANCELLED: frozenset(),
}

TERMINAL_PROJECT_STATUSES: frozenset[ProjectStatus] = frozenset(
    status for status, allowed in PROJECT_TRANSITIONS.items() if not allowed
)

#: Which role executes which node type.
#:
#: Enforced rather than advisory. A COMPUTATION node executed by Master would
#: let the role that decides what the result means also produce the result,
#: which is exactly the separation the role model exists to keep.
NODE_EXECUTOR: dict[NodeType, AgentRole] = {
    NodeType.RESEARCH: AgentRole.RESEARCH,
    NodeType.HYPOTHESIS: AgentRole.MASTER,
    NodeType.COMPUTATION: AgentRole.COMPUTE_WORKER,
    NodeType.EXPERIMENT: AgentRole.EXPERIMENTAL_WORKER,
    NodeType.REVIEW: AgentRole.REVIEW,
    NodeType.DECISION: AgentRole.MASTER,
}

#: Node types whose acceptance criteria must be frozen before they run.
FROZEN_CRITERIA_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.COMPUTATION, NodeType.EXPERIMENT}
)


class TransitionError(ValueError):
    """A requested state change is not legal."""


@dataclass(frozen=True, slots=True)
class TransitionCheck:
    """The result of asking whether a transition is legal."""

    allowed: bool
    reason: str

    def raise_if_denied(self) -> None:
        """Turn a denial into an exception.

        Raises:
            TransitionError: The transition is not allowed.
        """
        if not self.allowed:
            raise TransitionError(self.reason)


def can_transition_node(current: NodeStatus, target: NodeStatus) -> TransitionCheck:
    """Whether a node may move from `current` to `target`."""
    if current == target:
        # Idempotent re-assertion is how a retried activity reports the state
        # it already reached. Refusing it would make every retry an error.
        return TransitionCheck(True, f"{current.value} is already the node's status")

    allowed = NODE_TRANSITIONS[current]
    if target not in allowed:
        if not allowed:
            return TransitionCheck(
                False,
                f"{current.value} is terminal; a new node is required instead of a "
                f"transition to {target.value}",
            )
        reachable = ", ".join(sorted(s.value for s in allowed))
        return TransitionCheck(
            False, f"{current.value} may not become {target.value}; allowed: {reachable}"
        )
    return TransitionCheck(True, f"{current.value} -> {target.value}")


def can_transition_project(current: ProjectStatus, target: ProjectStatus) -> TransitionCheck:
    """Whether a project may move from `current` to `target`."""
    if current == target:
        return TransitionCheck(True, f"{current.value} is already the project's status")

    allowed = PROJECT_TRANSITIONS[current]
    if target not in allowed:
        if not allowed:
            return TransitionCheck(
                False, f"{current.value} is terminal; the project cannot become {target.value}"
            )
        reachable = ", ".join(sorted(s.value for s in allowed))
        return TransitionCheck(
            False, f"{current.value} may not become {target.value}; allowed: {reachable}"
        )
    return TransitionCheck(True, f"{current.value} -> {target.value}")


def executor_for(node_type: NodeType) -> AgentRole:
    """The role that executes a node type."""
    return NODE_EXECUTOR[node_type]


def requires_frozen_criteria(node_type: NodeType) -> bool:
    """Whether a node type must freeze acceptance criteria before RUNNING."""
    return node_type in FROZEN_CRITERIA_NODE_TYPES


def is_join_satisfied(
    join_policy: JoinPolicy | str | None,
    join_threshold: int | None,
    dependency_count: int,
    succeeded: int,
) -> bool:
    """Whether a fan-in node's dependencies are satisfied.

    `succeeded` counts dependencies that reached a passing terminal status.
    A THRESHOLD join without a threshold is a configuration fault, not a join
    that is silently treated as ALL.

    Raises:
        ValueError: A THRESHOLD join carries no usable threshold.
    """
    if join_policy is None:
        # No dependencies is the only case where a policy may be absent; a node
        # with dependencies and no policy would have an undefined readiness rule.
        return dependency_count == 0

    policy = JoinPolicy(join_policy)
    if dependency_count == 0:
        return True
    if policy is JoinPolicy.ALL:
        return succeeded >= dependency_count
    if policy is JoinPolicy.ANY:
        return succeeded >= 1
    if join_threshold is None or join_threshold < 1:
        raise ValueError("a THRESHOLD join requires join_threshold >= 1")
    if join_threshold > dependency_count:
        # Unreachable by construction. Reporting it as unsatisfied would make
        # the node wait forever with no explanation.
        raise ValueError(
            f"join_threshold {join_threshold} exceeds the {dependency_count} dependencies"
        )
    return succeeded >= join_threshold
