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

`BackendJobRepository` sits here for the same reason the rest do: it is the
record of work in flight, and the durable layer needs it to be a database fact
rather than something a workflow remembers.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ravel.domain.clock import utcnow
from ravel.domain.decisions import DecisionRecord, ReviewRecord
from ravel.domain.enums import Confidence, DecisionType, FailureClass, JobState
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.execution import (
    BackendJob,
    DeviationRecord,
    ExecutionRecord,
    WorkerMessage,
)
from ravel.domain.roles import AgentRole, require_role
from ravel.state.mapping import to_row_data
from ravel.state.outbox import emit
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.repositories.reconciliation import RunReconciliationRepository
from ravel.state.tables import (
    BackendJobRow,
    DecisionRecordRow,
    DeviationRecordRow,
    ExecutionRecordRow,
    ReviewRecordRow,
    WorkerMessageRow,
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
        require_role(role, AgentRole.MASTER, "write a Decision Record")
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

    def submit(
        self, review: ReviewRecord, *, role: AgentRole, actor_id: str | None = None
    ) -> ReviewRecord:
        """Write a Review Record and announce it.

        Review may report, diagnose, and recommend. It has no method here to
        change a node's status, a contract, or the DAG.

        **Who is asking is part of the record.** A Review Record is the
        measurement the three powers are separated to protect: it is what a
        verdict about someone else's work is made of, and a Worker that could
        write one could pass its own work. So the role is required and checked
        at the write, which is the boundary a caller has to cross, rather than
        trusted from a caller that has already decided to be Review.

        Raises:
            PermissionError: The caller is not Review.
        """
        require_role(role, AgentRole.REVIEW, "submit a Review Record")
        self.add(review)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.REVIEW_SUBMITTED,
            actor_type=ActorType.AGENT,
            actor_id=actor_id or role.value,
            payload={
                "review_id": review.review_id,
                "display_id": review.display_id,
                "node_id": review.node_id,
                "checkpoint": review.checkpoint.value,
                "outcome": review.outcome.value,
                "frozen_criteria_version": review.frozen_criteria_version,
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


class BackendJobRepository(ProjectScopedRepository[BackendJob]):
    """The job a backend is running for one attempt at one node.

    `start` is the method the durable layer depends on, and it is the only
    reason this repository exists in the shape it does. A worker that dies
    between handing work to a backend and recording the result is retried, and
    the retry must not hand the work over a second time. So starting is an
    insert that conflicts rather than a check followed by an insert: the
    database decides, and two activities racing produce one job rather than
    two.
    """

    row_type = BackendJobRow
    record_type = BackendJob

    def _order_by(self) -> Any:
        """Journey order, which for a job is attempt order.

        The base repository sorts by `created_at`, and a job has none: a job's
        clock starts when it is submitted, and its `updated_at` moves every
        time a backend reports — so ordering by either would put the second
        attempt before the first as soon as the first was polled.
        """
        return self.row_type.attempt

    def for_attempt(
        self, node_id: str, attempt: int, execution_contract_version: int
    ) -> BackendJob | None:
        """The job already recorded for one attempt at one version, if there is one.

        All three parts are needed to name an attempt. A node whose contract was
        revised runs again under the new version, and that run's first attempt is
        attempt one — not the next number after the run that raised the question.
        """
        return self._one(
            node_id=node_id,
            attempt=attempt,
            execution_contract_version=execution_contract_version,
        )

    def for_node(self, node_id: str) -> list[BackendJob]:
        """Every job this node has had, oldest attempt first."""
        return sorted(self.all(node_id=node_id), key=lambda job: job.attempt)

    def latest_for_node(self, node_id: str) -> BackendJob | None:
        """The most recent attempt's job, if the node has one.

        Ordered by attempt, which is the only ordering `for_node` has — so for
        a node that has run under more than one version this is the
        highest-numbered attempt of *any* version, and not necessarily the
        newest run. A caller that means "the run in flight now" wants
        `latest_for_version` with the version it is asking about.
        """
        jobs = self.for_node(node_id)
        return jobs[-1] if jobs else None

    def latest_for_version(
        self, node_id: str, execution_contract_version: int
    ) -> BackendJob | None:
        """The newest attempt recorded under one version of a node's contract.

        The version is part of the question for the reason it is part of the
        job's key: a node whose terms Master revised starts again at attempt
        one under the new version, so "the latest job" across versions would
        answer with an attempt of a run that has already ended.

        Attempts under one version are numbered from one and `for_node` sorts
        by attempt, so the last of the filtered list is the newest — not the
        one whose `updated_at` is largest, which polling moves.
        """
        matching = [
            job
            for job in self.for_node(node_id)
            if job.execution_contract_version == execution_contract_version
        ]
        return matching[-1] if matching else None

    def start(self, job: BackendJob) -> BackendJob:
        """Record that a backend is taking on this attempt.

        Idempotent by primary key of the work rather than by anything the
        caller does: the row is keyed by
        `(project_id, node_id, execution_contract_version, attempt)`, so a
        retried call finds the first one and returns it.

        Raises:
            ProjectScopeError: The job belongs to a different project. The
                check is explicit because this method writes its own INSERT
                rather than going through `add`, so the guard that every other
                write passes is not on this path by default — and a row written
                from a caller-supplied record is exactly where a supplied
                `project_id` would otherwise be taken at its word.
            ValueError: A job is already recorded for this attempt and it is
                not the same job. That is not a retry; it means two different
                pieces of work were proposed for one attempt, and returning
                either one would hide it.
        """
        self._authorize(job)
        statement = (
            pg_insert(BackendJobRow)
            .values(**to_row_data(job, BackendJobRow))
            .on_conflict_do_nothing(
                index_elements=[
                    "project_id",
                    "node_id",
                    "execution_contract_version",
                    "attempt",
                ]
            )
            .returning(BackendJobRow.job_id)
        )
        inserted = self.session.execute(statement).scalar_one_or_none()
        stored = self.for_attempt(
            job.node_id, job.attempt, job.execution_contract_version
        )
        if stored is None:  # pragma: no cover - the insert cannot vanish
            raise NotFound(
                f"job for attempt {job.attempt} of node {job.node_id} under contract "
                f"version {job.execution_contract_version} was neither inserted nor "
                "found, which means the row was removed"
            )
        if stored.job_id != job.job_id and not stored.same_work_as(job):
            raise ValueError(
                f"attempt {job.attempt} of node {job.node_id} under contract version "
                f"{job.execution_contract_version} is already recorded as job "
                f"{stored.job_id} on backend {stored.backend!r}, which is not the job "
                "now being started"
            )
        if inserted is not None:
            self._emit(stored, change="STARTED")
        return stored

    def record_state(
        self,
        job_id: str,
        state: JobState,
        *,
        backend_state: str = "",
        backend_job_ref: str | None = None,
        failure_class: FailureClass | None = None,
        detail: str = "",
        actor_id: str | None = None,
        actor_type: ActorType = ActorType.BACKEND,
    ) -> BackendJob:
        """Move a job to a new state and record the change.

        Re-asserting the state a job is already in writes nothing and emits
        nothing, so a poll that reports the same thing twice does not fill the
        event stream with it.

        **Who is credited with the change defaults to the backend, and is not
        always the backend.** Almost every call here is a report: the backend
        said the job is running, and RAVEL wrote down what it was told. One
        caller is not — the reconciler ends a job belonging to a run that no
        longer exists, which is RAVEL's own act on its own machinery — and a
        stream that credited that to the backend would say the machine
        reported an ending it never reported. So the actor travels with the
        call rather than being assumed, and the default keeps every existing
        caller's event exactly as it was.

        Raises:
            TransitionError: The move is not legal, or the job has ended.
        """
        job = self.get(job_id=job_id)
        reference = backend_job_ref or job.backend_job_ref
        if job.state is state and reference == job.backend_job_ref:
            return job
        moved = job.moved_to(
            state,
            backend_state=backend_state,
            failure_class=failure_class,
            detail=detail,
        )
        moved = moved.model_validate({**moved.model_dump(), "backend_job_ref": reference})
        row = self.session.get(BackendJobRow, job_id)
        assert row is not None
        row.state = moved.state.value
        row.backend_state = moved.backend_state
        row.failure_class = (
            moved.failure_class.value if moved.failure_class is not None else None
        )
        row.detail = moved.detail
        row.backend_job_ref = moved.backend_job_ref
        row.updated_at = moved.updated_at
        row.ended_at = moved.ended_at
        self._emit(moved, change="STATE", actor_id=actor_id, actor_type=actor_type)
        return moved

    def _emit(
        self,
        job: BackendJob,
        *,
        change: str,
        actor_id: str | None = None,
        actor_type: ActorType = ActorType.BACKEND,
    ) -> None:
        """Record the change in the project's stream.

        `BACKEND_STATUS_CHANGED` is the event the vocabulary already had for
        this, and it is emitted with the backend as the actor by default:
        RAVEL did not decide the job was running, it was told.

        The backend's own state word travels in the payload beside RAVEL's, so
        a reader can see the two disagree without opening the table — and
        `backend_job_ref` travels with it, because the moment RAVEL learns
        which job on the backend this is, is a moment worth being able to find
        again.
        """
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.BACKEND_STATUS_CHANGED,
            actor_type=actor_type,
            actor_id=actor_id or job.backend,
            payload={
                "change": change,
                "job_id": job.job_id,
                "node_id": job.node_id,
                "attempt": job.attempt,
                "backend_job_ref": job.backend_job_ref,
                "backend_state": job.backend_state,
                "state": job.state.value,
                "failure_class": (
                    job.failure_class.value if job.failure_class is not None else None
                ),
            },
        )


class DeviationRepository(ProjectScopedRepository[DeviationRecord]):
    """Requests a contract did not permit, waiting on Master."""

    row_type = DeviationRecordRow
    record_type = DeviationRecord

    def _order_by(self) -> Any:
        """`raised_at`, because a deviation has no `created_at`.

        The base class sorts by `created_at`, and this is the one record whose
        timestamp is named for what it is. Without this, `all()` — and therefore
        `open()`, and therefore every reader that asks what is waiting on Master
        — raised `AttributeError` rather than returning anything.
        """
        return self.row_type.raised_at

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


class WorkerMessageRepository(ProjectScopedRepository[WorkerMessage]):
    """What a Worker said, inside the four kinds it is allowed to say it in."""

    row_type = WorkerMessageRow
    record_type = WorkerMessage

    def _order_by(self) -> Any:
        """When it was said.

        The base class sorts by `created_at`, which a message has not got: a
        message is sent at a moment, and `sent_at` is that moment.
        """
        return self.row_type.sent_at

    def record(self, message: WorkerMessage) -> WorkerMessage:
        """Stage one message.

        No event is emitted. The project event vocabulary has none for a
        Worker's message, and inventing one here would put a type in the stream
        that `schemas/project_events.yaml` does not define — which is a worse
        problem than a message being found by reading the table it is in.
        """
        return self.add(message)

    def for_node(self, node_id: str) -> list[WorkerMessage]:
        """Everything said about one node, in the order it was said."""
        return self.all(node_id=node_id)


class RecordRepositories:
    """The record repositories for one project, sharing one session.

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
        self.jobs = BackendJobRepository(session, project_id)
        self.messages = WorkerMessageRepository(session, project_id)
        self.reconciliations = RunReconciliationRepository(session, project_id)

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
