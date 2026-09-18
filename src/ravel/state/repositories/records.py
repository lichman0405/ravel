"""Decision Records, Review Records, Execution Records, and deviations.

These are the records that make the separation of powers checkable. Each one is
written by exactly one role, and the repository enforces which:

- A Decision Record is written by Master. `record_decision` refuses any other
  role, because the record exists to say who decided.
- A Review Record is written by Review, and Review never writes a Decision.
- An Execution Record is written by a Worker, against the contract version it
  actually ran under, so a later change to what workers may do cannot rewrite
  what this one did.

All four are append-only. A deviation is the one exception in spirit: it is
*resolved* by writing a decision reference onto it, which is a single
permitted UPDATE, guarded by a check constraint that the reference and the
timestamp appear together or not at all.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from ravel.domain.clock import utcnow
from ravel.domain.decisions import DecisionRecord, ReviewRecord
from ravel.domain.enums import Confidence, DecisionType
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.execution import DeviationRecord, ExecutionRecord
from ravel.domain.roles import AgentRole
from ravel.state.outbox import emit
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.tables import (
    DecisionRecordRow,
    DeviationRecordRow,
    ExecutionRecordRow,
    ReviewRecordRow,
)


class DecisionRepository(ProjectScopedRepository[DecisionRecord]):
    """Why Master changed the plan."""

    row_type = DecisionRecordRow
    record_type = DecisionRecord

    def record(
        self,
        decision: DecisionRecord,
        *,
        role: AgentRole = AgentRole.MASTER,
    ) -> DecisionRecord:
        """Write a Decision Record.

        Raises:
            PermissionError: The actor is not Master. A decision record is the
                formal scientific decision; only Master holds that authority.
        """
        if role is not AgentRole.MASTER:
            raise PermissionError(
                f"the {role.value} role may not write a Decision Record; only "
                f"{AgentRole.MASTER.value} decides"
            )
        self.add(decision)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.DECISION_CREATED,
            actor_type=ActorType.AGENT,
            actor_id=role.value,
            payload={
                "decision_id": decision.decision_id,
                "display_id": decision.display_id,
                "decision_type": decision.decision_type.value,
                "affected_nodes": list(decision.affected_nodes.all_nodes),
            },
        )
        return decision

    def of_type(self, decision_type: DecisionType) -> list[DecisionRecord]:
        """Every decision of one type."""
        return self.all(decision_type=decision_type.value)

    def high_confidence(self) -> list[DecisionRecord]:
        """Every decision Master recorded as high confidence."""
        return self.all(confidence=Confidence.HIGH.value)


class ReviewRepository(ProjectScopedRepository[ReviewRecord]):
    """What Review found, measured against the criteria frozen before the run."""

    row_type = ReviewRecordRow
    record_type = ReviewRecord

    def submit(self, review: ReviewRecord, *, actor_id: str) -> ReviewRecord:
        """Write a Review Record and announce it.

        Review may report, diagnose, and recommend. It has no method here to
        change a node's status, a contract, or the DAG.
        """
        self.add(review)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.REVIEW_SUBMITTED,
            actor_type=ActorType.AGENT,
            actor_id=actor_id,
            payload={
                "review_id": review.review_id,
                "display_id": review.display_id,
                "node_id": review.node_id,
                "checkpoint": review.checkpoint.value,
                "outcome": review.outcome.value,
                "frozen_acceptance_version": review.frozen_acceptance_version,
            },
        )
        return review

    def for_node(self, node_id: str) -> list[ReviewRecord]:
        """Every review of one node, oldest first."""
        return self.all(node_id=node_id)

    def latest_for_node(self, node_id: str) -> ReviewRecord | None:
        """The most recent review of a node, if any."""
        reviews = self.for_node(node_id)
        return reviews[-1] if reviews else None


class ExecutionRepository(ProjectScopedRepository[ExecutionRecord]):
    """The immutable record of what a Worker actually did."""

    row_type = ExecutionRecordRow
    record_type = ExecutionRecord

    def record(self, execution: ExecutionRecord, *, actor_id: str) -> ExecutionRecord:
        """Write an Execution Record.

        The record names the contract version it ran under. Writing it is the
        only way an execution becomes visible; nothing updates a node's status
        to PASSED on the strength of an execution alone — that is a Decision.
        """
        self.add(execution)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.BACKEND_STATUS_CHANGED,
            actor_type=ActorType.BACKEND,
            actor_id=actor_id,
            payload={
                "execution_id": execution.execution_id,
                "node_id": execution.node_id,
                "backend": execution.backend,
                "termination_status": execution.termination_status.value,
                "delivery_is_complete": execution.delivery_is_complete,
            },
        )
        return execution

    def for_node(self, node_id: str) -> list[ExecutionRecord]:
        """Every execution of one node, oldest first."""
        return self.all(node_id=node_id)


class DeviationRepository(ProjectScopedRepository[DeviationRecord]):
    """Requests a contract did not permit, waiting on Master."""

    row_type = DeviationRecordRow
    record_type = DeviationRecord

    def raise_(self, deviation: DeviationRecord) -> DeviationRecord:
        """Record that a Worker was asked for something its contract did not allow."""
        self.add(deviation)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.DEVIATION_REPORTED,
            actor_type=ActorType.AGENT,
            actor_id=deviation.raised_by,
            payload={
                "deviation_id": deviation.deviation_id,
                "node_id": deviation.node_id,
                "requested_action": deviation.requested_action,
            },
        )
        return deviation

    def open(self) -> list[DeviationRecord]:
        """Every deviation no decision has resolved yet."""
        return [deviation for deviation in self.all() if deviation.is_open]

    def resolve(self, deviation_id: str, *, decision_ref: str) -> DeviationRecord:
        """Close a deviation by pointing it at the decision that settled it."""
        deviation = self.get(deviation_id=deviation_id)
        if not deviation.is_open:
            return deviation
        resolved = deviation.model_copy(
            update={"resolved_by_decision_ref": decision_ref, "resolved_at": utcnow()}
        )
        row = self.session.get(DeviationRecordRow, deviation_id)
        assert row is not None
        row.resolved_by_decision_ref = decision_ref
        row.resolved_at = resolved.resolved_at
        return resolved


class RecordRepositories:
    """The four record repositories for one project, sharing one session.

    A convenience for callers that need several of them — the Gateway, a
    Temporal activity — without threading a session through each construction.
    """

    def __init__(self, session: Session, project_id: str) -> None:
        self.session = session
        self.project_id = project_id
        self.decisions = DecisionRepository(session, project_id)
        self.reviews = ReviewRepository(session, project_id)
        self.executions = ExecutionRepository(session, project_id)
        self.deviations = DeviationRepository(session, project_id)

    def latest_execution(self, node_id: str) -> ExecutionRecord | None:
        """The most recent execution of a node, if any."""
        executions = self.executions.for_node(node_id)
        return executions[-1] if executions else None

    def decision_for(self, decision_id: str) -> DecisionRecord:
        """One decision by identifier.

        Raises:
            NotFound: This project has no such decision.
        """
        return self.decisions.get(decision_id=decision_id)
