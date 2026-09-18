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
    TerminationStatus,
    WorkerMessageKind,
)
from ravel.domain.ids import DisplayPrefix, display_id, new_id


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
