"""The Scientific DAG: nodes, edges, and the only path a status takes.

Three rules are enforced here and nowhere else:

- **Only Master mutates the DAG.** `mutate` takes the actor's role and refuses
  anything but `AgentRole.MASTER`. The check is on the role, not on a claim in
  a payload, because the role comes from the runtime's bound scope.
- **A material mutation carries a Decision Record.** `add_node` and `cancel_node`
  require a decision reference, so a DAG change can never be unattributable.
- **A node cannot start without its contracts.** The acceptance-criteria freeze
  and the Execution Contract are both checked against the tables, not against
  what a caller says it has.
"""

from __future__ import annotations

from sqlalchemy import func, select

from ravel.domain.dag import DagEdge, DagNode
from ravel.domain.enums import JoinPolicy, NodeStatus, NodeType
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import (
    ACTIVE_NODE_STATUSES,
    TERMINAL_NODE_STATUSES,
    is_join_satisfied,
    is_join_unsatisfiable,
)
from ravel.state.mapping import build_row, from_row
from ravel.state.outbox import emit
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.tables import (
    AcceptanceContractRow,
    DagEdgeRow,
    DagNodeRow,
    ExecutionContractRow,
)

#: The coarse event a status change is announced as. The payload always carries
#: the exact status, so the stream stays readable without inventing vocabulary
#: that would have to be added to a closed enumeration.
_NODE_EVENT: dict[NodeStatus, ProjectEventType] = {
    NodeStatus.PLANNED: ProjectEventType.NODE_WAITING,
    NodeStatus.READY: ProjectEventType.NODE_READY,
    NodeStatus.RUNNING: ProjectEventType.NODE_STARTED,
    NodeStatus.WAITING_EXTERNAL: ProjectEventType.NODE_WAITING,
    NodeStatus.WAITING_DECISION: ProjectEventType.NODE_WAITING,
    NodeStatus.REVIEWING: ProjectEventType.NODE_WAITING,
    NodeStatus.BLOCKED: ProjectEventType.NODE_WAITING,
    NodeStatus.PASSED: ProjectEventType.NODE_COMPLETED,
    NodeStatus.PARTIAL: ProjectEventType.NODE_COMPLETED,
    NodeStatus.FAILED: ProjectEventType.NODE_FAILED,
    NodeStatus.CANCELLED: ProjectEventType.NODE_CANCELLED,
}


class DagRepository(ProjectScopedRepository[DagNode]):
    """The project's scientific DAG."""

    row_type = DagNodeRow
    record_type = DagNode

    # ── Reading ─────────────────────────────────────────────────────────────

    def node(self, node_id: str) -> DagNode:
        """One node.

        Raises:
            NotFound: This project has no such node.
        """
        return self.get(node_id=node_id)

    def nodes(self) -> list[DagNode]:
        """Every node, oldest first."""
        return self.all()

    def nodes_in_status(self, *statuses: NodeStatus) -> list[DagNode]:
        """Every node currently in one of these statuses."""
        rows = (
            self.session.execute(
                self._scoped(DagNodeRow.status.in_([status.value for status in statuses]))
                .order_by(DagNodeRow.created_at)
            )
            .scalars()
            .all()
        )
        return [from_row(DagNode, row) for row in rows]

    def active_nodes(self) -> list[DagNode]:
        """Nodes occupying a worker right now."""
        return self.nodes_in_status(*ACTIVE_NODE_STATUSES)

    def nodes_of_type(self, node_type: NodeType) -> list[DagNode]:
        """Every node of one type."""
        return self.all(node_type=node_type.value)

    def edges(self) -> list[DagEdge]:
        """Every dependency edge in the project."""
        rows = (
            self.session.execute(
                select(DagEdgeRow)
                .where(DagEdgeRow.project_id == self.project_id)
                .order_by(DagEdgeRow.created_at)
            )
            .scalars()
            .all()
        )
        return [from_row(DagEdge, row) for row in rows]

    def edges_into(self, node_id: str) -> list[DagEdge]:
        """Edges whose target is this node."""
        rows = (
            self.session.execute(
                select(DagEdgeRow).where(
                    DagEdgeRow.project_id == self.project_id, DagEdgeRow.to_node == node_id
                )
            )
            .scalars()
            .all()
        )
        return [from_row(DagEdge, row) for row in rows]

    # ── Mutation ────────────────────────────────────────────────────────────

    def _require_master(self, role: AgentRole) -> None:
        """Refuse a DAG mutation from anything but Master."""
        if role is not AgentRole.MASTER:
            raise PermissionError(
                f"the {role.value} role may not mutate the DAG; only "
                f"{AgentRole.MASTER.value} holds that authority"
            )

    def add_node(self, node: DagNode, *, role: AgentRole, decision_ref: str) -> DagNode:
        """Add a node to the DAG, attributed to a Decision Record.

        Raises:
            PermissionError: The actor is not Master.
            ProjectScopeError: The node belongs to another project.
        """
        self._require_master(role)
        self._require_decision(decision_ref)
        self.add(node)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.DAG_MUTATED,
            actor_type=ActorType.AGENT,
            actor_id=role.value,
            payload={
                "change": "ADD_NODE",
                "node_id": node.node_id,
                "display_id": node.display_id,
                "node_type": node.node_type.value,
                "decision_ref": decision_ref,
            },
        )
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.NODE_CREATED,
            actor_type=ActorType.AGENT,
            actor_id=role.value,
            payload={"node_id": node.node_id, "display_id": node.display_id},
        )
        return node

    def add_edge(
        self, *, from_node: str, to_node: str, role: AgentRole, decision_ref: str
    ) -> DagEdge:
        """Record a dependency between two nodes of this project.

        The edge row and the target node's declared `dependencies` are two views
        of the same fact. Both are written here so they cannot disagree, and
        `tests/integration/state` asserts that for every edge.

        Raises:
            PermissionError: The actor is not Master.
            NotFound: One of the nodes is not in this project.
        """
        self._require_master(role)
        self._require_decision(decision_ref)
        source = self.node(from_node)
        target = self.node(to_node)

        edge = DagEdge(project_id=self.project_id, from_node=from_node, to_node=to_node)
        self.session.add(build_row(DagEdgeRow, edge))

        if from_node not in target.dependencies:
            declared = (*target.dependencies, from_node)
            row = self.session.get(DagNodeRow, to_node)
            assert row is not None
            row.dependencies = list(declared)
            if row.join_policy is None:
                row.join_policy = JoinPolicy.ALL.value

        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.DAG_MUTATED,
            actor_type=ActorType.AGENT,
            actor_id=role.value,
            payload={
                "change": "ADD_EDGE",
                "from_node": source.node_id,
                "to_node": target.node_id,
                "decision_ref": decision_ref,
            },
        )
        return edge

    def cancel_node(
        self, node_id: str, *, role: AgentRole, decision_ref: str, actor_id: str | None = None
    ) -> DagNode:
        """Cancel a node, attributed to a Decision Record.

        Raises:
            PermissionError: The actor is not Master.
        """
        self._require_master(role)
        self._require_decision(decision_ref)
        cancelled = self.transition_node(
            node_id,
            NodeStatus.CANCELLED,
            actor_id=actor_id or role.value,
            decision_ref=decision_ref,
        )
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.DAG_MUTATED,
            actor_type=ActorType.AGENT,
            actor_id=role.value,
            payload={
                "change": "CANCEL_NODE",
                "node_id": node_id,
                "decision_ref": decision_ref,
            },
        )
        return cancelled

    # ── Life cycle ──────────────────────────────────────────────────────────

    def transition_node(
        self,
        node_id: str,
        target: NodeStatus,
        *,
        actor_id: str,
        decision_ref: str | None = None,
    ) -> DagNode:
        """Move a node to a new status and record the change.

        Re-asserting the current status writes nothing and emits nothing, so an
        activity that is retried after a timeout does not put a second
        `NODE_STARTED` in the stream.

        Raises:
            NotFound: This project has no such node.
            TransitionError: The transition is illegal, or a precondition for
                RUNNING is unmet.
        """
        node = self.node(node_id)
        if node.status is target:
            return node

        if target is NodeStatus.RUNNING:
            node.can_enter_running(
                has_frozen_acceptance=self.has_frozen_acceptance(node_id),
                has_execution_contract=self.has_execution_contract(node_id),
            ).raise_if_denied()

        moved = node.transition(target)
        row = self.session.get(DagNodeRow, node_id)
        assert row is not None
        for column in ("status", "started_at", "completed_at"):
            setattr(row, column, getattr(moved, column))

        emit(
            self.session,
            project_id=self.project_id,
            event_type=_NODE_EVENT[target],
            actor_type=ActorType.AGENT,
            actor_id=actor_id,
            payload={
                "node_id": node_id,
                "display_id": node.display_id,
                "from": node.status.value,
                "status": target.value,
                "decision_ref": decision_ref,
            },
        )
        return moved

    def record_artifact(self, node_id: str, artifact_id: str) -> DagNode:
        """Attach an artifact to a node's references."""
        node = self.node(node_id)
        if artifact_id in node.artifact_refs:
            return node
        row = self.session.get(DagNodeRow, node_id)
        assert row is not None
        row.artifact_refs = [*node.artifact_refs, artifact_id]
        return node.model_copy(update={"artifact_refs": (*node.artifact_refs, artifact_id)})

    def bind_acceptance_contract(self, node_id: str, contract_ref: str) -> DagNode:
        """Point a node at its acceptance contract."""
        return self._bind(node_id, "acceptance_contract_ref", contract_ref)

    def bind_execution_contract(self, node_id: str, contract_ref: str) -> DagNode:
        """Point a node at its execution contract."""
        return self._bind(node_id, "execution_contract_ref", contract_ref)

    def _bind(self, node_id: str, column: str, value: str) -> DagNode:
        node = self.node(node_id)
        current = getattr(node, column)
        if current is not None and current != value:
            raise ValueError(
                f"node {node.display_id} already references {current} as its {column}; "
                "rebinding would let an executed result be measured against a "
                "contract chosen afterwards"
            )
        row = self.session.get(DagNodeRow, node_id)
        assert row is not None
        setattr(row, column, value)
        return node.model_copy(update={column: value})

    # ── Preconditions ───────────────────────────────────────────────────────

    def has_frozen_acceptance(self, node_id: str) -> bool:
        """Whether the node has an acceptance contract that has been frozen."""
        found = self.session.execute(
            select(func.count())
            .select_from(AcceptanceContractRow)
            .where(
                AcceptanceContractRow.project_id == self.project_id,
                AcceptanceContractRow.node_id == node_id,
                AcceptanceContractRow.frozen_at.is_not(None),
            )
        ).scalar_one()
        return bool(found)

    def has_execution_contract(self, node_id: str) -> bool:
        """Whether the node has an Execution Contract at all."""
        found = self.session.execute(
            select(func.count())
            .select_from(ExecutionContractRow)
            .where(
                ExecutionContractRow.project_id == self.project_id,
                ExecutionContractRow.node_id == node_id,
            )
        ).scalar_one()
        return bool(found)

    def _require_decision(self, decision_ref: str) -> None:
        if not decision_ref:
            raise ValueError(
                "a DAG mutation requires a Decision Record reference; an "
                "unattributable change to the research plan is not permitted"
            )

    # ── Scheduling ──────────────────────────────────────────────────────────

    def dependency_states(self, node: DagNode) -> tuple[int, int, int]:
        """`(succeeded, still_running, failed)` among a node's dependencies."""
        succeeded = still_running = failed = 0
        for dependency_id in node.dependencies:
            dependency = self.node(dependency_id)
            if dependency.succeeded:
                succeeded += 1
            elif dependency.status in TERMINAL_NODE_STATUSES:
                failed += 1
            else:
                still_running += 1
        return succeeded, still_running, failed

    def join_state(self, node: DagNode) -> str:
        """Whether a node's fan-in is `satisfied`, `waiting`, or `unsatisfiable`."""
        succeeded, still_running, _ = self.dependency_states(node)
        if is_join_satisfied(
            node.join_policy, node.join_threshold, len(node.dependencies), succeeded
        ):
            return "satisfied"
        if is_join_unsatisfiable(
            node.join_policy,
            node.join_threshold,
            len(node.dependencies),
            succeeded,
            still_running,
        ):
            return "unsatisfiable"
        return "waiting"

    def refresh_readiness(self) -> list[DagNode]:
        """Promote planned nodes whose fan-in is settled.

        A node whose join is satisfied becomes READY. One whose fan-in can no
        longer be satisfied becomes BLOCKED, so it is visibly waiting on a
        decision rather than silently waiting forever.

        Returns the nodes that moved.
        """
        moved: list[DagNode] = []
        for node in self.nodes_in_status(NodeStatus.PLANNED, NodeStatus.BLOCKED):
            state = self.join_state(node)
            if state == "satisfied":
                # BLOCKED -> READY is legal, so a node that was blocked by a
                # dependency which later passed is promoted rather than left
                # sitting behind a failure that no longer applies.
                moved.append(
                    self.transition_node(node.node_id, NodeStatus.READY, actor_id="scheduler")
                )
            elif state == "unsatisfiable" and node.status is NodeStatus.PLANNED:
                moved.append(
                    self.transition_node(node.node_id, NodeStatus.BLOCKED, actor_id="scheduler")
                )
        return moved
