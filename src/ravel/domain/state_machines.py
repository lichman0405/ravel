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

from ravel.domain.enums import JobState, JoinPolicy, NodeStatus, NodeType, ProjectStatus
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

#: The statuses in which a node's work has been handed over to be judged.
#:
#: REVIEWING is where a seat leaves a result it has finished: the work is over
#: and the node is waiting for the FINAL checkpoint, which is why it is here
#: rather than among the active ones — nothing is being worked on any more. The
#: other three are what a FINAL verdict moves a node to, so the set is exactly
#: "there is a result, and here is where it stands".
#:
#: Not a synonym for "has a record" or "has artifacts": which of those exist is
#: a property of the node type — a RESEARCH node's result is a Research Record,
#: a COMPUTATION node's is an Execution Record and its artifacts — and this set
#: is about the node, whatever kind of work it handed over.
HANDED_OVER_NODE_STATUSES: frozenset[NodeStatus] = frozenset(
    {
        NodeStatus.REVIEWING,
        NodeStatus.PASSED,
        NodeStatus.FAILED,
        NodeStatus.PARTIAL,
    }
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
            ProjectStatus.INCONCLUSIVE,
            ProjectStatus.CANCELLED,
        }
    ),
    ProjectStatus.PAUSED: frozenset(
        {
            ProjectStatus.EXECUTING,
            ProjectStatus.CANCELLED,
            ProjectStatus.FAILED,
            ProjectStatus.INCONCLUSIVE,
        }
    ),
    ProjectStatus.COMPLETED: frozenset(),
    ProjectStatus.FAILED: frozenset(),
    ProjectStatus.INCONCLUSIVE: frozenset(),
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
#:
#: Partial, and deliberately so: a node type that is not a key here is a type
#: no role performs. REVIEW was one, and is the reason this table stopped
#: being total — see `PLANNABLE_NODE_TYPES`.
NODE_EXECUTOR: dict[NodeType, AgentRole] = {
    NodeType.RESEARCH: AgentRole.RESEARCH,
    NodeType.HYPOTHESIS: AgentRole.MASTER,
    NodeType.COMPUTATION: AgentRole.COMPUTE_WORKER,
    NodeType.EXPERIMENT: AgentRole.EXPERIMENTAL_WORKER,
    NodeType.DECISION: AgentRole.MASTER,
}

#: The node types a node may be created as.
#:
#: A node is a unit of work in the plan, and a plan is only a plan if every
#: node in it has an ending somebody is meant to give it. Five of the six have
#: one. Three — RESEARCH, COMPUTATION, EXPERIMENT — are carried out by a seat
#: that runs the work. Two — HYPOTHESIS and DECISION — are Master's control
#: nodes: the DAG holds a claim or a decision the rest of the plan depends on,
#: Master is asked about them for as long as they are unfinished (the loop's
#: `Situation.unexecutable` is what asks), and cancelling one or replacing it
#: with work that runs is the ordinary ending.
#:
#: **REVIEW is not in this set, and is not a node type any more.** A review is
#: not work the plan contains; it is a checkpoint *on* a node that runs, and
#: RAVEL asks for it through that node's own status: a COMPUTATION or
#: EXPERIMENT node is cleared to run by a PRE_RUN verdict, and every node that
#: ran holds at REVIEWING until a FINAL verdict moves it. A REVIEW node asked
#: for a second copy of that — a node whose whole content was "review
#: something else" — and no seat was ever going to perform it: `NODE_EXECUTOR`
#: assigned it to the Review Agent, whose entire surface is verdicts about
#: *other* nodes, and the loop has no execution path into one. Live runs
#: planned nine of them across a single project and cancelled every one of
#: them, spending forty-three minutes and thirty CANCEL_NODE decisions on work
#: nobody could be given (KNOWN_LIMITATIONS L-27). The type was abolished
#: instead, and the mechanism it duplicated was left as the only one.
#:
#: `NodeType.REVIEW` stays a member of the enum so that a node written while
#: the type still existed can still be read: the DAG is history as well as a
#: plan, and history is allowed to contain a type the plan may no longer hold.
#: Nothing may create one. Ending the ones already written is Master's act and
#: not RAVEL's — cancelling a node is a DAG mutation, and a migration that
#: cancelled every surviving REVIEW node would be RAVEL taking a decision that
#: requires Master — so no migration was written, and the read paths explain
#: such a node instead: `executor_for` answers `None` for it, and
#: `unexecutable_reason` is what Master is told.
PLANNABLE_NODE_TYPES: frozenset[NodeType] = frozenset(
    node_type for node_type in NodeType if node_type is not NodeType.REVIEW
)

#: The node types an execution seat is given: the ones whose executor carries
#: work out rather than deciding about it.
#:
#: Master is not an execution seat, so its own node types are not here. That is
#: the distinction `Situation.unexecutable` turns on — a READY node of one of
#: these types is work somebody will be handed, and a READY node of any other
#: type is a question for Master and is never handed to anybody. Derived from
#: `NODE_EXECUTOR` rather than written out beside it, because a list next to a
#: table is a list that can disagree with it.
SEATED_NODE_TYPES: frozenset[NodeType] = frozenset(
    node_type
    for node_type, seat in NODE_EXECUTOR.items()
    if seat is not AgentRole.MASTER
)

#: Node types whose acceptance criteria must be frozen before they run.
FROZEN_CRITERIA_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.COMPUTATION, NodeType.EXPERIMENT}
)

#: Node types a Worker performs as a *run*: a durable workflow that starts a
#: backend job and reports through its own activities.
#:
#: The rest of the plannable types are performed by an agent inside its own
#: turn. The distinction is invisible in `NodeStatus` — a RESEARCH node and a
#: COMPUTATION node are both `RUNNING` while somebody works on them — and it
#: matters to anything that asks a system *outside* PostgreSQL whether that
#: work is still under way. A workflow id for a RESEARCH node was never
#: started, so Temporal answers that it does not exist, which is true and
#: means nothing: there was never one to lose.
WORKER_RUN_NODE_TYPES: frozenset[NodeType] = frozenset(
    {NodeType.COMPUTATION, NodeType.EXPERIMENT}
)

#: Job state -> the states it may move to.
#:
#: The four terminal states have no outgoing edges, for the same reason the
#: node's do not: a job that ended one way did not end another, and a later
#: report that contradicts an earlier one is a new job, not an edit.
#:
#: `WAITING_EXTERNAL` exists as its own state rather than as a flavour of
#: `RUNNING` because the two are bounded by different things. A running job is
#: bounded by the machine it runs on; a job waiting on a laboratory is bounded
#: by a person, which is why it is the one state a signal can end.
JOB_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.SUBMITTED: frozenset(
        {
            JobState.RUNNING,
            JobState.WAITING_EXTERNAL,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }
    ),
    JobState.RUNNING: frozenset(
        {
            JobState.WAITING_EXTERNAL,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }
    ),
    JobState.WAITING_EXTERNAL: frozenset(
        {
            JobState.RUNNING,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.TIMED_OUT,
        }
    ),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
    JobState.TIMED_OUT: frozenset(),
}


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


def can_transition_job(current: JobState, target: JobState) -> TransitionCheck:
    """Whether a backend job may move from `current` to `target`."""
    if current == target:
        # Same reason as the node's: a poll that reports the state the job is
        # already in is the normal case, not an error.
        return TransitionCheck(True, f"{current.value} is already the job's state")

    allowed = JOB_TRANSITIONS[current]
    if target not in allowed:
        if not allowed:
            return TransitionCheck(
                False,
                f"the job ended as {current.value}; a later report of {target.value} "
                "contradicts a recorded ending, which needs a new job rather than an "
                "amendment",
            )
        reachable = ", ".join(sorted(s.value for s in allowed))
        return TransitionCheck(
            False, f"{current.value} may not become {target.value}; allowed: {reachable}"
        )
    return TransitionCheck(True, f"{current.value} -> {target.value}")


def executor_for(node_type: NodeType) -> AgentRole | None:
    """The role that executes a node type, or `None` when no role does.

    `None` is a real answer and not a missing one. A node type nobody performs
    is a type the plan may not hold (`PLANNABLE_NODE_TYPES`), and the one that
    reached that state — REVIEW — keeps its member in the enum so that rows
    written while it was plannable can still be read. Reading one asks this
    function about it, and "nobody performs this" is what the record should
    yield rather than a `KeyError` out of a history read.
    """
    return NODE_EXECUTOR.get(node_type)


def requires_frozen_criteria(node_type: NodeType) -> bool:
    """Whether a node type must freeze acceptance criteria before RUNNING."""
    return node_type in FROZEN_CRITERIA_NODE_TYPES


def unexecutable_reason(node_type: NodeType) -> str:
    """Why no execution seat is ever handed a node of this type.

    A READY node of such a type is the one thing in a plan that no amount of
    waiting resolves, and there are two ways to be one. A type the domain
    assigns to nobody has no performer at all. A type it assigns to Master has
    a performer who is not an execution seat: Master's part in the loop is to
    be *asked*, not to be handed a task, so a node of its own is resolved by a
    decision rather than by work somebody carries out.

    Read wherever such a node has to be explained, rather than written out at
    each of them: a loop that puts one to Master, the prompt that tells Master
    what is waiting on it, and the project-state read a session makes are three
    windows onto one fact, and a sentence copied into three files is a sentence
    that can come to disagree with itself (KNOWN_LIMITATIONS L-27).
    """
    seat = executor_for(node_type)
    if seat is None:
        return (
            f"nothing in RAVEL performs a {node_type.value} node: it has no "
            "executor, no run, and no seat that would be handed it, so it stays "
            "READY however long the project waits. It was planned while the type "
            "was still plannable; cancelling it, or replacing it with work that "
            "runs, is the only thing that moves it"
        )
    return (
        f"a {node_type.value} node is {seat.display_name}'s own, and that is not "
        "an execution seat: no Worker and no Research session is ever handed "
        "one, because that role's part in the loop is to be asked rather than "
        "given a task. The loop puts it to that role while it is unfinished, and "
        "cancelling it or replacing it with work that runs is how it ends"
    )


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


def is_join_unsatisfiable(
    join_policy: JoinPolicy | str | None,
    join_threshold: int | None,
    dependency_count: int,
    succeeded: int,
    still_running: int,
) -> bool:
    """Whether a fan-in can no longer be satisfied, however the rest turn out.

    The complement of `is_join_satisfied`, and the reason it exists: a node
    whose dependencies have all failed would otherwise sit in PLANNED forever.
    A silently stalled DAG is worse than a blocked node, because nothing in the
    system can tell the difference between "waiting" and "never going to run".

    `still_running` counts dependencies that have not reached a terminal status
    and so might still succeed.

    Raises:
        ValueError: A THRESHOLD join carries no usable threshold.
    """
    if join_policy is None:
        return dependency_count == 0
    policy = JoinPolicy(join_policy)
    if dependency_count == 0:
        return False
    if is_join_satisfied(join_policy, join_threshold, dependency_count, succeeded):
        return False

    best_possible = succeeded + still_running
    if policy is JoinPolicy.ALL:
        return best_possible < dependency_count
    if policy is JoinPolicy.ANY:
        return best_possible < 1
    threshold = 1 if join_threshold is None else join_threshold
    return best_possible < threshold
