"""Decision Records and Review Records.

These two records are the separation of powers made durable. Review says what
happened; Master says what it means. Neither can write the other's record, and
both are immutable once written.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    Confidence,
    DecisionType,
    ReviewCheckpoint,
    ReviewOutcome,
)
from ravel.domain.ids import DisplayPrefix, display_id, new_id


class AuthorityCheck(Record):
    """The authority question Master answered before acting.

    Recorded rather than assumed. If a later reader asks "was Master allowed to
    do this", the answer is a field on the decision, not an inference from
    which role happened to be running.
    """

    actor_role: str
    authority_envelope_ref: str | None = None
    required_approval_ref: str | None = None
    permitted: bool
    rationale: str = ""


class AffectedNodes(Record):
    """What a decision changed. Empty is a legitimate value.

    A decision that changed nothing structural — accepting a result, resolving
    a deviation — still records what it touched, so a reader can reconstruct
    the reasoning without diffing the DAG.
    """

    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    cancelled: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        """Whether the decision touched no nodes."""
        return not (self.created or self.modified or self.cancelled)

    @property
    def all_nodes(self) -> tuple[str, ...]:
        """Every node the decision touched, without duplicates."""
        return tuple(dict.fromkeys((*self.created, *self.modified, *self.cancelled)))


class DecisionRecord(Record):
    """Why Master changed the plan.

    Required for every material mutation. The fields are the ones a reader
    needs to disagree with the decision: what triggered it, what else was
    considered, what evidence it rested on, and how confident Master was.
    """

    decision_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.DECISION))
    project_id: str
    decision_type: DecisionType
    trigger_refs: tuple[str, ...] = ()
    rationale: str = Field(min_length=1)
    alternatives_considered: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    affected_nodes: AffectedNodes = Field(default_factory=AffectedNodes)
    confidence: Confidence
    authority_check: AuthorityCheck
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _material_change_names_what_it_changed(self) -> DecisionRecord:
        """A decision that creates, cancels, or replaces nodes must name them."""
        structural = {
            DecisionType.CREATE_NODE,
            DecisionType.CANCEL_NODE,
            DecisionType.REPLACE_NODE,
            DecisionType.CHANGE_ROUTE,
        }
        if self.decision_type in structural and self.affected_nodes.is_empty:
            raise ValueError(
                f"decision {self.display_id} is a {self.decision_type.value} but names "
                "no affected nodes; the DAG change it authorizes would be unattributable"
            )
        return self


class CriterionResult(Record):
    """Review's verdict on one frozen criterion."""

    criterion_id: str
    statement: str = ""
    satisfied: bool
    observed: str = ""
    evidence_refs: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    note: str = ""


class ReviewRecord(Record):
    """What Review found, against the criteria that were frozen before the run.

    `frozen_criteria_ref` and `frozen_criteria_version` name the definition of
    done this review measured against, and they are required. Which contract
    that is depends on the node: a COMPUTATION or EXPERIMENT node has an
    Acceptance Contract because `can_enter_running` demands one, and every
    other executed node has its Execution Contract, which is also frozen before
    the run and names what it owed. Either way the review states what it read,
    so a verdict cannot be measured against criteria that changed afterwards.
    """

    review_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.REVIEW))
    project_id: str
    node_id: str
    checkpoint: ReviewCheckpoint
    frozen_criteria_ref: str
    frozen_criteria_version: int = Field(ge=1)
    outcome: ReviewOutcome
    criterion_results: tuple[CriterionResult, ...] = ()
    diagnosis: str = ""
    recommendations: tuple[str, ...] = ()
    artifact_refs: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    review_session_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _verdict_matches_the_criteria(self) -> ReviewRecord:
        """The outcome must follow from the criterion results, not sit beside them.

        A FINAL review with every criterion satisfied but an outcome of FAIL is
        a contradiction a reader would have to guess their way through.
        """
        if self.checkpoint is not ReviewCheckpoint.FINAL or not self.criterion_results:
            return self

        satisfied = sum(1 for result in self.criterion_results if result.satisfied)
        total = len(self.criterion_results)
        if satisfied == total and self.outcome is not ReviewOutcome.PASS:
            raise ValueError(
                f"review {self.display_id} satisfied all {total} criteria but reported "
                f"{self.outcome.value}"
            )
        if satisfied == 0 and self.outcome is not ReviewOutcome.FAIL:
            raise ValueError(
                f"review {self.display_id} satisfied none of {total} criteria but "
                f"reported {self.outcome.value}"
            )
        if 0 < satisfied < total and self.outcome is not ReviewOutcome.PARTIAL:
            raise ValueError(
                f"review {self.display_id} satisfied {satisfied} of {total} criteria but "
                f"reported {self.outcome.value}"
            )
        return self

    @property
    def satisfied_count(self) -> int:
        """How many frozen criteria the result met."""
        return sum(1 for result in self.criterion_results if result.satisfied)
