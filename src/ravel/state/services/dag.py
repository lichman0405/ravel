"""The one path by which a project's scientific plan changes.

`DagRepository` enforces *who* may mutate the DAG: only Master. This module
enforces *what* may be mutated, and it does so by making the Decision Record a
precondition of the change rather than a comment on it:

- the decision is written first, in the caller's transaction, so a mutation
  that is refused leaves no decision behind and a decision that exists
  describes a change that happened;
- `affected_nodes` is filled in from the mutation that was actually applied,
  so the record cannot disagree with the DAG it authorized;
- a node claimed as work for a roadmap stage must fall inside the rolling
  horizon, which is what stops Master from writing stage five's detailed plan
  before stage two has produced anything.

**Why the split is where it is.** Authority is checked in the repository,
because that is the boundary an attacker would have to cross and it must not
depend on which service happens to be called. Horizon and decision-recording
are checked here, because they are research discipline rather than security:
a caller that genuinely needs to bypass them — a test fixture, a bootstrap
script, a later migration — can reach the repository directly, and doing so is
visible in the call it makes. Both layers refuse a non-Master caller, from the
same function, so the two cannot drift apart.

**Everything here happens inside the caller's transaction.** These methods
stage records; nothing is in PostgreSQL until the caller commits. That is what
makes "a refused mutation leaves no trace" true rather than hopeful.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy.orm import Session

from ravel.domain.dag import DagNode
from ravel.domain.decisions import AffectedNodes, AuthorityCheck, DecisionRecord
from ravel.domain.enums import Confidence, DecisionType
from ravel.domain.planning import PhaseWork, PlanningHorizon, planning_horizon
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TERMINAL_NODE_STATUSES
from ravel.state.repositories.dag import DagRepository, require_master
from ravel.state.repositories.projects import RoadmapRepository
from ravel.state.repositories.records import DecisionRepository


@dataclass(frozen=True, slots=True)
class DecisionDraft:
    """What Master decided, before the service records what it changed.

    `affected_nodes` is deliberately absent. A caller that filled it in could
    name one node and create another, and the record that exists to make a DAG
    change attributable would then be the one document that disagrees with the
    DAG. The service builds it from the mutation it actually applied.

    `decision_type` is required rather than defaulted, because it is the field
    a later reader uses to find this decision, and an operation that does not
    know what kind of decision it is should not be recording one.
    """

    decision_type: DecisionType
    rationale: str
    confidence: Confidence = Confidence.MEDIUM
    trigger_refs: tuple[str, ...] = ()
    alternatives_considered: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    authority_rationale: str = ""
    authority_envelope_ref: str | None = None
    required_approval_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.rationale.strip():
            raise ValueError(
                "a decision draft needs a rationale; a Decision Record whose "
                "rationale is empty records that something changed but not why"
            )

    def record(
        self, *, project_id: str, actor: AgentRole, affected: AffectedNodes
    ) -> DecisionRecord:
        """The Decision Record this draft authorizes, naming what it changed."""
        return DecisionRecord(
            project_id=project_id,
            decision_type=self.decision_type,
            trigger_refs=self.trigger_refs,
            rationale=self.rationale,
            alternatives_considered=self.alternatives_considered,
            evidence_refs=self.evidence_refs,
            affected_nodes=affected,
            confidence=self.confidence,
            authority_check=AuthorityCheck(
                actor_role=actor.value,
                authority_envelope_ref=self.authority_envelope_ref,
                required_approval_ref=self.required_approval_ref,
                permitted=True,
                rationale=self.authority_rationale,
            ),
        )


@dataclass(frozen=True, slots=True)
class PhaseExpansion:
    """What expanding a stage produced."""

    phase: str
    decision: DecisionRecord
    nodes: tuple[DagNode, ...] = ()


class DagMutationService:
    """Master's planning operations, each bound to the decision that justified it."""

    def __init__(self, session: Session, project_id: str) -> None:
        self.session = session
        self.project_id = project_id
        self.dag = DagRepository(session, project_id)
        self.decisions = DecisionRepository(session, project_id)
        self.roadmap = RoadmapRepository(session, project_id)

    # ── The horizon ─────────────────────────────────────────────────────────

    def phase_work(self) -> dict[str, PhaseWork]:
        """What the DAG currently holds for each roadmap stage.

        A stage with no entry here has never been expanded, which is a
        different fact from a stage whose nodes have all finished.
        """
        counts: dict[str, list[int]] = {}
        for node in self.dag.nodes():
            if node.roadmap_phase is None:
                continue
            bucket = counts.setdefault(node.roadmap_phase, [0, 0])
            bucket[0] += 1
            if node.status not in TERMINAL_NODE_STATUSES:
                bucket[1] += 1
        return {
            phase: PhaseWork(nodes=planned, unfinished=unfinished)
            for phase, (planned, unfinished) in counts.items()
        }

    def horizon(self) -> PlanningHorizon:
        """How far ahead the executable plan may currently reach."""
        return planning_horizon(self.roadmap.phases(), work=self.phase_work())

    # ── Mutation ────────────────────────────────────────────────────────────

    def add_node(
        self, node: DagNode, *, role: AgentRole, decision: DecisionDraft
    ) -> DagNode:
        """Add one node, inside the horizon and attributed to a decision.

        Raises:
            PermissionError: The actor is not Master.
            HorizonError: The node is claimed as work for a stage beyond the
                planning horizon.
            NotFound: The node depends on a node this project does not have.
            ValueError: The node depends on itself.
            ProjectScopeError: The node belongs to another project.
        """
        require_master(role)
        self.horizon().require(node.roadmap_phase)
        return self._write_nodes([node], role=role, decision=decision)[0]

    def expand_phase(
        self,
        phase: str,
        nodes: Sequence[DagNode],
        *,
        role: AgentRole,
        decision: DecisionDraft,
    ) -> PhaseExpansion:
        """Commit a stage's concrete nodes under a single decision.

        This is the operation the horizon governs, and the one the spec means
        by "fully expand current stage". The nodes carry the stage name from
        this call rather than from each node, so a plan cannot half-belong to
        the stage it was submitted for.

        The nodes are written as one batch, so a node in the batch may depend on
        another node in it — an analysis on the measurement it reads — whatever
        order they are listed in.

        Raises:
            PermissionError: The actor is not Master.
            HorizonError: The stage is unknown or beyond the planning horizon.
            NotFound: A node depends on a node this project does not have.
            ValueError: The expansion is empty, a node already names a different
                stage, or the batch would close a dependency cycle.
        """
        require_master(role)
        if not nodes:
            raise ValueError(
                f"expanding {phase!r} commits no nodes; a stage is expanded by "
                "the executable work it is given, so there is nothing to record"
            )
        # Refuses both an unknown stage and one too far ahead, and says which.
        self.horizon().require(phase)

        for node in nodes:
            if node.roadmap_phase not in (None, phase):
                raise ValueError(
                    f"node {node.display_id} is claimed as work for "
                    f"{node.roadmap_phase!r} but was submitted for {phase!r}; a "
                    "node belongs to one stage"
                )

        attached = tuple(
            node.model_copy(update={"roadmap_phase": phase}) for node in nodes
        )
        affected = AffectedNodes(created=tuple(node.node_id for node in attached))
        record = self._record(decision, role=role, affected=affected)
        written = tuple(
            self._apply_nodes(attached, role=role, decision_ref=record.decision_id)
        )
        return PhaseExpansion(phase=phase, decision=record, nodes=written)

    def cancel_node(
        self, node_id: str, *, role: AgentRole, decision: DecisionDraft
    ) -> DagNode:
        """Cancel a node and record why.

        Raises:
            PermissionError: The actor is not Master.
            NotFound: This project has no such node.
        """
        require_master(role)
        self.dag.node(node_id)
        record = self._record(
            decision, role=role, affected=AffectedNodes(cancelled=(node_id,))
        )
        return self.dag.cancel_node(
            node_id, role=role, decision_ref=record.decision_id
        )

    # ── Internals ───────────────────────────────────────────────────────────

    def _record(
        self, decision: DecisionDraft, *, role: AgentRole, affected: AffectedNodes
    ) -> DecisionRecord:
        """Write the decision that authorizes a change, before the change."""
        return self.decisions.record(
            decision.record(
                project_id=self.project_id, actor=role, affected=affected
            ),
            role=role,
        )

    def _write_nodes(
        self, nodes: Sequence[DagNode], *, role: AgentRole, decision: DecisionDraft
    ) -> list[DagNode]:
        record = self._record(
            decision,
            role=role,
            affected=AffectedNodes(created=tuple(node.node_id for node in nodes)),
        )
        return self._apply_nodes(nodes, role=role, decision_ref=record.decision_id)

    def _apply_nodes(
        self, nodes: Sequence[DagNode], *, role: AgentRole, decision_ref: str
    ) -> list[DagNode]:
        """Add nodes that carry the decision which authorized them."""
        attributed = [
            node.model_copy(update={"decision_ref": decision_ref}) for node in nodes
        ]
        return self.dag.add_nodes(attributed, role=role, decision_ref=decision_ref)
