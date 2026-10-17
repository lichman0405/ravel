"""Scientific DAG nodes and edges.

The DAG is RAVEL's explicit research plan — typed, dynamic, versioned. It is
not a conversation history, not a Temporal workflow graph, and not a canvas the
user edits. Only Master mutates it, and every material mutation is accompanied
by an immutable Decision Record.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    FailurePolicy,
    JoinPolicy,
    NodeStatus,
    NodeType,
    ReviewOutcome,
)
from ravel.domain.ids import new_id
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import (
    NODE_TRANSITIONS,
    PLANNABLE_NODE_TYPES,
    TransitionCheck,
    can_transition_node,
    executor_for,
    requires_frozen_criteria,
)

#: Node type -> the display prefix its human identifier uses.
#:
#: Total, including the type nothing may create any more: a display identifier
#: is how a human names a node, and a node written under the old vocabulary
#: still has one.
_NODE_PREFIX: dict[NodeType, str] = {
    NodeType.RESEARCH: "RES",
    NodeType.HYPOTHESIS: "HYP",
    NodeType.COMPUTATION: "COMP",
    NodeType.EXPERIMENT: "EXP",
    NodeType.REVIEW: "REV",
    NodeType.DECISION: "DEC",
}


class DagEdge(Record):
    """A dependency: `from_node` must reach a passing state before `to_node` runs.

    Held as an explicit row rather than as a list on the node so that an edge
    can be added and removed as its own auditable fact.
    """

    edge_id: str = Field(default_factory=new_id)
    project_id: str
    from_node: str
    to_node: str
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _no_self_edge(self) -> DagEdge:
        if self.from_node == self.to_node:
            raise ValueError(f"node {self.from_node} cannot depend on itself")
        return self


def _unplannable(node_type: NodeType) -> str:
    """Why a node may not be created as this type, in a planner's terms.

    REVIEW gets a sentence of its own, because it is the one type with a
    mechanism standing behind it. A planner that asked for a REVIEW node wants
    something RAVEL already does for every node that runs, and the refusal is
    the only place that can say so — a node type that is simply missing from a
    list teaches nothing about where the checking went.
    """
    if node_type is NodeType.REVIEW:
        return (
            "a REVIEW node is not a kind of work and cannot be planned. A review "
            "is a checkpoint on a node that runs, asked for through that node's "
            "own status: write what you want checked into the `criteria` of the "
            "COMPUTATION or EXPERIMENT node it is about, and Review is asked to "
            "clear that node before it runs (PRE_RUN) and to judge its result "
            "afterwards (FINAL). Every node of every type that ran waits at "
            "REVIEWING until a FINAL verdict moves it, so a separate node of "
            "review would add a second copy of a question already being asked"
        )
    known = ", ".join(
        kind.value for kind in NodeType if kind in PLANNABLE_NODE_TYPES
    )
    return (
        f"{node_type.value} is not a node type a plan may contain; a node may be "
        f"one of {known}"
    )


class DagNode(Record):
    """One unit of research work.

    Only `status` and its timestamps ever change, and only through `transition`.
    The node's objective, its contracts, and its dependencies are fixed at
    creation: changing what a node is for means opening a new node, because the
    results already produced belong to the old one.
    """

    node_id: str = Field(default_factory=new_id)
    display_id: str
    project_id: str
    node_type: NodeType
    objective: str = Field(min_length=1)
    status: NodeStatus = NodeStatus.PLANNED
    executor_role: AgentRole
    dependencies: tuple[str, ...] = ()
    join_policy: JoinPolicy | None = None
    join_threshold: int | None = None
    failure_policy: FailurePolicy | None = None
    acceptance_contract_ref: str | None = None
    execution_contract_ref: str | None = None
    artifact_refs: tuple[str, ...] = ()
    decision_ref: str | None = None
    roadmap_phase: str | None = None
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _consistent(self) -> DagNode:
        if self.dependencies and self.join_policy is None:
            raise ValueError(
                f"node {self.node_id} has dependencies but no join_policy; its "
                "readiness rule would be undefined"
            )
        if not self.dependencies and self.join_policy is not None:
            raise ValueError(f"node {self.node_id} has a join_policy but no dependencies")
        if self.join_policy is JoinPolicy.THRESHOLD:
            if self.join_threshold is None or self.join_threshold < 1:
                raise ValueError(
                    f"node {self.node_id} is a THRESHOLD join and needs join_threshold >= 1"
                )
            if self.join_threshold > len(self.dependencies):
                raise ValueError(
                    f"node {self.node_id} has join_threshold {self.join_threshold} but "
                    f"only {len(self.dependencies)} dependencies, so it could never fire"
                )
        elif self.join_threshold is not None:
            raise ValueError(
                f"node {self.node_id} carries join_threshold without a THRESHOLD policy"
            )
        if self.node_id in self.dependencies:
            raise ValueError(f"node {self.node_id} lists itself as a dependency")
        # A type nobody performs has no executor to agree with, and a node of
        # one is a row written while it was still a node type: what it holds is
        # the seat the domain assigned then, and it is the record of that.
        seat = executor_for(self.node_type)
        if seat is not None and self.executor_role is not seat:
            raise ValueError(
                f"a {self.node_type.value} node is executed by "
                f"{seat.value}, not {self.executor_role.value}"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        project_id: str,
        node_type: NodeType,
        objective: str,
        created_by: str,
        executor_role: AgentRole | None = None,
        dependencies: tuple[str, ...] = (),
        join_policy: JoinPolicy | None = None,
        join_threshold: int | None = None,
        failure_policy: FailurePolicy | None = None,
        roadmap_phase: str | None = None,
    ) -> DagNode:
        """Build a node, deriving its executor and display identifier.

        Raises:
            ValueError: The type is not one a node may be created as, or the
                domain assigns it to no role at all. Both are the same refusal
                from the caller's side — the plan would hold work with no
                ending — and it is made here rather than at the tool so that
                every path into the DAG meets it, the test fixtures included.
        """
        if node_type not in PLANNABLE_NODE_TYPES:
            raise ValueError(_unplannable(node_type))
        seat = executor_role or executor_for(node_type)
        if seat is None:  # pragma: no cover - PLANNABLE covers this today
            raise ValueError(_unplannable(node_type))
        return cls(
            display_id=f"{_NODE_PREFIX[node_type]}-{new_id()[:8].upper()}",
            project_id=project_id,
            node_type=node_type,
            objective=objective,
            executor_role=seat,
            dependencies=dependencies,
            join_policy=join_policy,
            join_threshold=join_threshold,
            failure_policy=failure_policy,
            roadmap_phase=roadmap_phase,
            created_by=created_by,
        )

    @property
    def requires_frozen_criteria(self) -> bool:
        """Whether this node must have frozen acceptance criteria before RUNNING."""
        return requires_frozen_criteria(self.node_type)

    @property
    def is_terminal(self) -> bool:
        """Whether this node's status can no longer change."""
        return not NODE_TRANSITIONS[self.status]

    @property
    def succeeded(self) -> bool:
        """Whether this node reached a passing terminal status.

        PARTIAL counts: the junction it feeds is deciding whether enough was
        delivered, and a partial result is a delivered one. Whether it is
        scientifically acceptable is Review's question, not this property's.
        """
        return self.status in {NodeStatus.PASSED, NodeStatus.PARTIAL}

    def check_transition(self, target: NodeStatus) -> TransitionCheck:
        """Whether this node may move to a status, ignoring contract preconditions."""
        return can_transition_node(self.status, target)

    def transition(self, target: NodeStatus, at: datetime | None = None) -> DagNode:
        """Return a copy in the target status, or raise.

        Only the state machine is enforced here. The caller is responsible for
        the authority check and for the acceptance-criteria precondition, both
        of which need records this object does not hold.

        Raises:
            TransitionError: The transition is not legal from the current status.
        """
        self.check_transition(target).raise_if_denied()
        moment = at or utcnow()
        update: dict[str, object] = {"status": target}
        if target is NodeStatus.RUNNING and self.started_at is None:
            update["started_at"] = moment
        if target in {NodeStatus.PASSED, NodeStatus.FAILED, NodeStatus.PARTIAL}:
            update["completed_at"] = moment
        return self.model_copy(update=update)

    def can_enter_running(
        self,
        has_frozen_acceptance: bool,
        has_execution_contract: bool,
        pre_run_outcome: ReviewOutcome | None = None,
    ) -> TransitionCheck:
        """Whether this node may start executing.

        Three preconditions beyond the state machine, all from the specs:

        - a COMPUTATION/EXPERIMENT node must have acceptance criteria defined
          before it enters RUNNING, so the result cannot be measured against a
          threshold invented after seeing it;
        - every executed node needs an Execution Contract, because a Worker
          with no contract has no authority to act at all;
        - and a node that owes a pre-flight review must have *passed* one, which
          is what a checkpoint is for. `pre_run_outcome` is the verdict of the
          node's most recent PRE_RUN review, or `None` if it has never been
          reviewed — and both refusals are the point: a gate that only checked
          for a FAIL would let a node run by never being reviewed at all.

        The set of node types that owe a pre-flight review is the set that
        freezes criteria before running, which is why this reads
        `requires_frozen_criteria` rather than a second list: the criteria are
        what the reviewer reads, and a node without them has nothing to
        pre-flight.

        The default is `None` deliberately: a caller that forgets the argument
        fails closed for exactly the node types that need it, rather than
        opening the gate.
        """
        check = can_transition_node(self.status, NodeStatus.RUNNING)
        if not check.allowed:
            return check
        if self.requires_frozen_criteria and not has_frozen_acceptance:
            return TransitionCheck(
                False,
                f"{self.node_type.value} node {self.display_id} cannot run before its "
                "acceptance criteria are frozen",
            )
        if not has_execution_contract:
            return TransitionCheck(
                False,
                f"{self.node_type.value} node {self.display_id} has no Execution Contract; "
                "a worker would have no authority to act",
            )
        if self.requires_frozen_criteria and pre_run_outcome is not ReviewOutcome.PASS:
            reviewed = (
                "has not been reviewed before running"
                if pre_run_outcome is None
                else f"was reviewed before running and the verdict was {pre_run_outcome.value}"
            )
            return TransitionCheck(
                False,
                f"{self.node_type.value} node {self.display_id} {reviewed}; the "
                "pre-flight review is the checkpoint that clears a node to run, and "
                "starting without it is the one thing it exists to prevent",
            )
        return TransitionCheck(True, f"{self.display_id} may enter RUNNING")
