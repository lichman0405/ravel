"""The Scientific DAG: nodes, edges, and the only path a status takes.

Three rules are enforced here and nowhere else:

- **Only Master mutates the DAG.** Every mutating method takes the actor's role
  and refuses anything but `AgentRole.MASTER`. The check is on the role, not on
  a claim in a payload, because the role comes from the runtime's bound scope.
- **A material mutation carries a Decision Record.** `add_node` and `cancel_node`
  require a decision reference, so a DAG change can never be unattributable.
- **A node cannot start without its contracts.** The acceptance-criteria freeze
  and the Execution Contract are both checked against the tables, not against
  what a caller says it has.

**An edge is not a mutation of its own.** A dependency edge is what a node
declared when it was created, written as a row in the same transaction so the
graph can be read without parsing JSON and so the two views cannot drift. There
is no operation that adds an edge to a node that already exists, because the
guard on `dag_nodes` refuses to rewrite `dependencies` — and it is right to:
a node that gained a dependency after it ran would be measured against inputs
chosen afterwards. Wanting a different dependency means opening a new node.
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import func, select

from ravel.domain.dag import DagEdge, DagNode
from ravel.domain.enums import NodeStatus, NodeType
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
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.tables import (
    AcceptanceContractRow,
    ArtifactRow,
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


def require_master(role: AgentRole) -> None:
    """Refuse a DAG mutation from anything but Master.

    A module function rather than a private method because
    `ravel.state.services.dag` enforces the same rule before it writes
    anything, and two copies of an authority check are two places for it to
    drift apart.

    The check is on the role the runtime bound to the caller's scope, not on a
    claim in a payload, so it cannot be talked around by the caller.

    Raises:
        PermissionError: The actor is not Master.
    """
    if role is not AgentRole.MASTER:
        raise PermissionError(
            f"the {role.value} role may not mutate the DAG; only "
            f"{AgentRole.MASTER.value} holds that authority"
        )


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

    def add_node(self, node: DagNode, *, role: AgentRole, decision_ref: str) -> DagNode:
        """Add a node to the DAG, attributed to a Decision Record.

        Raises:
            PermissionError: The actor is not Master.
            NotFound: The node depends on a node this project does not have.
            ValueError: The node depends on itself.
            ProjectScopeError: The node belongs to another project.
        """
        return self.add_nodes((node,), role=role, decision_ref=decision_ref)[0]

    def add_nodes(
        self, nodes: Sequence[DagNode], *, role: AgentRole, decision_ref: str
    ) -> list[DagNode]:
        """Add nodes together with the dependency edges they declare.

        A node's `dependencies` and the edge rows are two views of one fact, and
        both are written here so they cannot disagree. This is the only moment
        an edge can come into existence: the guard on `dag_nodes` refuses to
        rewrite `dependencies` in place, so there is no separate "add an edge"
        operation that could fall out of step with the declaration.

        Every node row is inserted before any edge, so a node may depend on
        another node in the same call — a stage's analysis runs on that stage's
        measurements. Order within the batch therefore does not matter.

        Raises:
            PermissionError: The actor is not Master.
            NotFound: A dependency is not in this project or in the batch.
            ValueError: The batch would close a dependency cycle.
            ProjectScopeError: A node belongs to another project.
        """
        require_master(role)
        self._require_decision(decision_ref)
        self._require_dependencies(nodes)
        self._require_acyclic(nodes)

        for node in nodes:
            self.add(node)
            self._emit_node_added(node, role=role, decision_ref=decision_ref)
        for node in nodes:
            self._record_edges(node)
        return list(nodes)

    def _emit_node_added(self, node: DagNode, *, role: AgentRole, decision_ref: str) -> None:
        """Announce a new node. The payload carries its fan-in, since the edges
        it declared are part of what was decided, not a later amendment."""
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
                "dependencies": list(node.dependencies),
                "join_policy": node.join_policy.value if node.join_policy else None,
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

    def _record_edges(self, node: DagNode) -> None:
        """Write one edge row per declared dependency.

        Called once every node of the batch exists, because `dag_edges` has
        foreign keys to `dag_nodes` and an edge into a node that is still
        pending would violate them.
        """
        for dependency_id in node.dependencies:
            edge = DagEdge(
                project_id=self.project_id, from_node=dependency_id, to_node=node.node_id
            )
            self.session.add(build_row(DagEdgeRow, edge))

    def _require_dependencies(self, nodes: Sequence[DagNode]) -> None:
        """Refuse a plan that depends on a node which does not exist.

        `dependencies` is a JSONB list with no foreign key behind it, so nothing
        below this would notice a dangling identifier — until `dependency_states`
        followed it and raised `NotFound` out of the scheduler, which is the
        wrong place to learn that the plan was impossible.

        A dependency may be a node already in the project or one being added in
        the same call.

        Raises:
            NotFound: A dependency is neither in this project nor in the batch.
        """
        known = {node.node_id for node in self.nodes()} | {node.node_id for node in nodes}
        for node in nodes:
            missing = [dependency for dependency in node.dependencies if dependency not in known]
            if missing:
                raise NotFound(
                    f"node {node.display_id} depends on {', '.join(missing)}, which "
                    f"this project does not have; a dependency must name a node that "
                    f"exists when the node declaring it is created"
                )

    def _require_acyclic(self, nodes: Sequence[DagNode]) -> None:
        """Refuse a batch that would make the graph depend on itself.

        A cycle is not a crash, which is what makes it worth refusing here: it
        is two nodes that wait for each other forever, and nothing in the event
        stream would ever say why. The graph is acyclic before this call — every
        dependency had to exist when it was declared — so any cycle must run
        through a node in this batch, and walking out from those finds it.

        Raises:
            ValueError: The dependencies lead back to a node in the batch.
        """
        graph = {node.node_id: node.dependencies for node in self.nodes()}
        graph.update({node.node_id: node.dependencies for node in nodes})
        settled: set[str] = set()
        path: list[str] = []

        def walk(node_id: str) -> None:
            if node_id in settled:
                return
            if node_id in path:
                cycle = " -> ".join([*path[path.index(node_id) :], node_id])
                raise ValueError(
                    f"these nodes would wait on each other forever: {cycle}; a "
                    f"scientific DAG is acyclic by definition"
                )
            path.append(node_id)
            for dependency in graph.get(node_id, ()):
                walk(dependency)
            path.pop()
            settled.add(node_id)

        for node in nodes:
            walk(node.node_id)

    def cancel_node(
        self, node_id: str, *, role: AgentRole, decision_ref: str, actor_id: str | None = None
    ) -> DagNode:
        """Cancel a node, attributed to a Decision Record.

        Raises:
            PermissionError: The actor is not Master.
        """
        require_master(role)
        self._require_decision(decision_ref)
        cancelled = self._apply_transition(
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
        """Move a node along its life cycle and record the change.

        This is the path work takes: a node becomes RUNNING when a worker picks
        it up, REVIEWING when a result is handed over, FAILED when the contract
        was not met. Those moves are reported by the activity that performed
        them, and the authority for them is the Execution Contract the activity
        ran under, not this method.

        What it will not do is cancel. Cancelling is not a report of what
        happened, it is Master deciding the work should not happen — so it is a
        DAG mutation, and it goes through `cancel_node`, which demands Master
        and a Decision Record. Allowing it here would leave that requirement
        with a second door beside it.

        Re-asserting the current status writes nothing and emits nothing, so an
        activity that is retried after a timeout does not put a second
        `NODE_STARTED` in the stream.

        Raises:
            PermissionError: The target is CANCELLED; use `cancel_node`.
            NotFound: This project has no such node.
            TransitionError: The transition is illegal, or a precondition for
                RUNNING is unmet.
        """
        if target is NodeStatus.CANCELLED:
            raise PermissionError(
                f"cancelling {node_id} is a DAG mutation, not a status report; it "
                "goes through cancel_node, which requires Master and a Decision "
                "Record. There is no path to CANCELLED that skips them"
            )
        return self._apply_transition(
            node_id, target, actor_id=actor_id, decision_ref=decision_ref
        )

    def _apply_transition(
        self,
        node_id: str,
        target: NodeStatus,
        *,
        actor_id: str,
        decision_ref: str | None,
    ) -> DagNode:
        """Write a status change. Callers are responsible for who may ask."""
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
        """Attach an artifact to a node's references.

        Raises:
            NotFound: This project has no such artifact.
        """
        node = self.node(node_id)
        if artifact_id in node.artifact_refs:
            return node
        self._require_artifact(artifact_id)
        row = self.session.get(DagNodeRow, node_id)
        assert row is not None
        row.artifact_refs = [*node.artifact_refs, artifact_id]
        return node.model_copy(update={"artifact_refs": (*node.artifact_refs, artifact_id)})

    def bind_acceptance_contract(self, node_id: str, contract_ref: str) -> DagNode:
        """Point a node at its acceptance contract.

        Raises:
            NotFound: This project has no such contract.
        """
        AcceptanceContractRepository(self.session, self.project_id).get(
            contract_id=contract_ref
        )
        return self._bind(node_id, "acceptance_contract_ref", contract_ref)

    def bind_execution_contract(self, node_id: str, contract_ref: str) -> DagNode:
        """Point a node at its execution contract.

        Raises:
            NotFound: This project has no such contract.
        """
        ExecutionContractRepository(self.session, self.project_id).get(
            contract_id=contract_ref
        )
        return self._bind(node_id, "execution_contract_ref", contract_ref)

    def _bind(self, node_id: str, column: str, value: str) -> DagNode:
        """Write a contract reference onto a node.

        Both callers check that the contract exists in this project first. The
        reference is a plain string column with no foreign key, so nothing below
        this would notice a dangling or another project's identifier — and a
        node bound to a contract its own project cannot read is a node whose
        acceptance criteria nobody can check.
        """
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

    def _require_artifact(self, artifact_id: str) -> None:
        """Refuse a reference to an artifact this project does not have.

        A scoped count rather than `ArtifactRepository`, which demands the
        object store at construction: checking that a reference can be followed
        must not require the bytes to be reachable from the DAG repository, and
        a node may legitimately be pointed at an artifact whose bytes live
        behind a store this process is not the one serving.

        Raises:
            NotFound: This project has no such artifact.
        """
        found = self.session.execute(
            select(func.count())
            .select_from(ArtifactRow)
            .where(
                ArtifactRow.project_id == self.project_id,
                ArtifactRow.artifact_id == artifact_id,
            )
        ).scalar_one()
        if not found:
            raise NotFound(f"no artifact {artifact_id!r} in {self.project_id}")

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
