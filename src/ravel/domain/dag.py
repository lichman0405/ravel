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
from ravel.domain.enums import FailurePolicy, JoinPolicy, NodeStatus, NodeType
from ravel.domain.ids import new_id
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import (
    NODE_TRANSITIONS,
    TransitionCheck,
    can_transition_node,
    executor_for,
    requires_frozen_criteria,
)

#: Node type -> the display prefix its human identifier uses.
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
        if self.executor_role is not executor_for(self.node_type):
            raise ValueError(
                f"a {self.node_type.value} node is executed by "
                f"{executor_for(self.node_type).value}, not {self.executor_role.value}"
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
        """Build a node, deriving its executor and display identifier."""
        return cls(
            display_id=f"{_NODE_PREFIX[node_type]}-{new_id()[:8].upper()}",
            project_id=project_id,
            node_type=node_type,
            objective=objective,
            executor_role=executor_role or executor_for(node_type),
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
        self, has_frozen_acceptance: bool, has_execution_contract: bool
    ) -> TransitionCheck:
        """Whether this node may start executing.

        Two preconditions beyond the state machine, both from the specs:

        - a COMPUTATION/EXPERIMENT node must have acceptance criteria defined
          before it enters RUNNING, so the result cannot be measured against a
          threshold invented after seeing it;
        - every executed node needs an Execution Contract, because a Worker
          with no contract has no authority to act at all.
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
        return TransitionCheck(True, f"{self.display_id} may enter RUNNING")
