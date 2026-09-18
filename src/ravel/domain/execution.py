"""Execution records, deviations, and delivery completeness.

Delivery completeness is a separate question from scientific acceptance, and
keeping them separate is the whole point of this module. The Worker answers
"did we receive everything the contract required?"; Review answers "does the
complete result pass the frozen criteria?". Conflating them would let a missing
artifact be reported as a scientific failure, which would send Master looking
for a flaw in a result that was never delivered.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    CompletenessVerdict,
    FailureClass,
    JobState,
    TerminationStatus,
    WorkerMessageKind,
)
from ravel.domain.ids import DisplayPrefix, display_id, new_id
from ravel.domain.state_machines import can_transition_job


class ExecutionAttempt(Record):
    """One try at the work.

    Retries are recorded as separate attempts rather than as a counter, because
    the interesting question after a failure is usually what differed between
    the attempts.
    """

    attempt: int = Field(ge=1)
    started_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None
    backend_job_ref: str | None = None
    outcome: TerminationStatus | None = None
    note: str = ""


class DeviationRecord(Record):
    """Something asked for that the Execution Contract did not permit.

    A deviation is not a judgement that the request was scientifically wrong.
    The Worker is not asked to make that judgement — only whether the contract
    named the action. If it did not, the Worker pauses and this record is the
    reason.
    """

    deviation_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    execution_contract_ref: str
    requested_action: str = Field(min_length=1)
    description: str = Field(min_length=1)
    permitted: bool = False
    raised_by: str
    raised_at: datetime = Field(default_factory=utcnow)
    resolved_by_decision_ref: str | None = None
    resolved_at: datetime | None = None

    @property
    def is_open(self) -> bool:
        """Whether the deviation is still waiting on Master."""
        return self.resolved_by_decision_ref is None


class CompletenessCheck(Record):
    """The Worker's answer to "did we receive everything required?"."""

    verdict: CompletenessVerdict
    required_outputs: tuple[str, ...] = ()
    delivered_outputs: tuple[str, ...] = ()
    missing_outputs: tuple[str, ...] = ()
    checked_by: str = ""
    checked_at: datetime = Field(default_factory=utcnow)
    note: str = ""

    @model_validator(mode="after")
    def _verdict_follows_from_the_lists(self) -> CompletenessCheck:
        if self.verdict is CompletenessVerdict.INCOMPLETE_DELIVERY and not self.missing_outputs:
            raise ValueError(
                "an INCOMPLETE_DELIVERY verdict must name what is missing; without "
                "that, the report cannot be acted on"
            )
        if self.verdict is CompletenessVerdict.COMPLETE and self.missing_outputs:
            raise ValueError(
                f"a COMPLETE verdict cannot list missing outputs: {self.missing_outputs}"
            )
        return self


class WorkerMessage(Record):
    """Something a Worker said to a lab, inside the four permitted kinds.

    The list is closed because a Worker that can say anything can negotiate
    outside its contract. `ESCALATE` is how it asks for more authority.
    """

    message_id: str = Field(default_factory=new_id)
    node_id: str
    kind: WorkerMessageKind
    body: str = Field(min_length=1)
    approved_by_contract: bool = True
    sent_at: datetime = Field(default_factory=utcnow)


class BackendJob(Record):
    """One job handed to a backend, while it is still in flight.

    This is the row that makes a retried activity safe, and it exists because
    of the one thing Temporal cannot be asked to remember. A worker that dies
    between "the backend accepted the job" and "the result was recorded" is
    retried by Temporal, and the retry must not submit the work a second time.
    Temporal's history does hold the activity's result — but
    `docs/04_STATE_AND_DATA.md` forbids business-critical state living only
    there, and "which job is running for this attempt" is exactly that: it is
    the difference between resuming a computation and paying for a second one.

    So the job is written to PostgreSQL before the backend is asked to do
    anything, keyed by `(project_id, node_id, attempt)`. A retried activity
    finds it and returns it rather than starting again.

    `state` is RAVEL's word for where the job is and `backend_state` is the
    backend's own; both are kept because they answer different questions. The
    first drives the durable layer, the second is what a human reads when the
    two disagree.

    The record is mutable, unlike almost everything else in RAVEL: a job's
    state changes while it runs. What is not mutable is its identity — which
    attempt it is, which contract it runs under, which backend holds it — and
    that is enforced in the database, not here.
    """

    job_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    attempt: int = Field(ge=1)
    execution_contract_ref: str
    execution_contract_version: int = Field(ge=1)
    backend: str = Field(min_length=1)
    backend_job_ref: str | None = None
    state: JobState = JobState.SUBMITTED
    backend_state: str = ""
    failure_class: FailureClass | None = None
    detail: str = ""
    submitted_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    ended_at: datetime | None = None

    @model_validator(mode="after")
    def _an_ending_is_all_or_nothing(self) -> BackendJob:
        """A terminal state and an end time are the same statement.

        Allowing one without the other produces a job that is over with no
        record of when, or one that is still running with a time it finished.
        Both would be read as fact by whatever comes next.
        """
        if self.state.is_terminal and self.ended_at is None:
            raise ValueError(
                f"job {self.job_id} is {self.state.value} but records no end time; a "
                "job that has ended has ended at some point"
            )
        if not self.state.is_terminal and self.ended_at is not None:
            raise ValueError(
                f"job {self.job_id} is {self.state.value} but records an end time; "
                "only a terminal state ends a job"
            )
        return self

    @model_validator(mode="after")
    def _only_a_failure_has_a_failure_class(self) -> BackendJob:
        """A job that did not fail has no reason for failing.

        `failure_class` is what decides whether the work is retried, so a
        value left on a job that succeeded would be read by the retry policy
        as a reason to run it again.
        """
        if self.failure_class is not None and self.state not in (
            JobState.FAILED,
            JobState.TIMED_OUT,
        ):
            raise ValueError(
                f"job {self.job_id} is {self.state.value} and carries failure class "
                f"{self.failure_class.value}; only a job that failed has a reason"
            )
        return self

    @property
    def is_terminal(self) -> bool:
        """Whether the job has ended."""
        return self.state.is_terminal

    @property
    def is_waiting(self) -> bool:
        """Whether the job is waiting on something outside RAVEL."""
        return self.state is JobState.WAITING_EXTERNAL

    def same_work_as(self, other: BackendJob) -> bool:
        """Whether two records are two views of the same piece of work.

        `job_id` cannot answer this: it is generated when a caller builds the
        record, so a retried activity proposes the same work under a new
        identifier. What the work *is* — which contract version, on which
        backend — is what has to match, and it is what a repository compares
        before deciding that an attempt already has a job.
        """
        return (
            self.node_id == other.node_id
            and self.attempt == other.attempt
            and self.backend == other.backend
            and self.execution_contract_ref == other.execution_contract_ref
            and self.execution_contract_version == other.execution_contract_version
        )

    def moved_to(
        self,
        state: JobState,
        *,
        at: datetime | None = None,
        backend_state: str = "",
        failure_class: FailureClass | None = None,
        detail: str = "",
    ) -> BackendJob:
        """Return this job in a new state, validated.

        `backend_state` and `detail` describe the state being moved *to* and
        replace whatever the previous one said. That is deliberate: the record
        holds where the job is now, and the sequence of where it has been is
        the `BACKEND_STATUS_CHANGED` events, which are append-only. Keeping a
        history inside a mutable row would be a second, worse copy of it.

        Raises:
            TransitionError: The move is not legal, or the job has already
                ended.
        """
        can_transition_job(self.state, state).raise_if_denied()
        if state is self.state:
            return self
        moment = at or utcnow()
        return BackendJob.model_validate(
            {
                **self.model_dump(),
                "state": state,
                "backend_state": backend_state,
                "failure_class": failure_class,
                "detail": detail,
                "updated_at": moment,
                "ended_at": moment if state.is_terminal else None,
            }
        )


class ExecutionRecord(Record):
    """The immutable record of one execution.

    Written once, when the work ends — by delivery, by failure, by cancellation,
    or by an open deviation. It references the contract version it ran under,
    so a later change to what workers may do cannot rewrite what this one did.
    """

    execution_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.EXECUTION))
    project_id: str
    node_id: str
    execution_contract_ref: str
    execution_contract_version: int = Field(ge=1)
    backend: str = Field(min_length=1)
    backend_job_ref: str | None = None
    attempts: tuple[ExecutionAttempt, ...] = ()
    output_refs: tuple[str, ...] = ()
    log_refs: tuple[str, ...] = ()
    completeness: CompletenessCheck
    deviations: tuple[str, ...] = ()
    termination_status: TerminationStatus
    executed_by: str = ""
    started_at: datetime | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _attempts_are_numbered_from_one(self) -> ExecutionRecord:
        for index, attempt in enumerate(self.attempts, start=1):
            if attempt.attempt != index:
                raise ValueError(
                    f"execution {self.display_id} has attempt {attempt.attempt} in "
                    f"position {index}; attempts must be numbered consecutively from 1"
                )
        return self

    @property
    def attempt_count(self) -> int:
        """How many times the work was tried."""
        return len(self.attempts)

    @property
    def delivery_is_complete(self) -> bool:
        """Whether everything the contract required was delivered.

        Not a statement about whether the result is any good.
        """
        return self.completeness.verdict is CompletenessVerdict.COMPLETE
