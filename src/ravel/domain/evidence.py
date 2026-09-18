"""The Evidence Ledger.

An artifact is bytes. Evidence is a claim about the world, and the difference
matters: the same PDF can support one claim and contradict another. Evidence is
therefore a separate record that *references* artifacts and sources, and it
carries the retrieval metadata that makes the claim checkable.

A search result is a lead, not evidence. Every `EvidenceSource` records what was
actually opened, when, and what the bytes hashed to — so a reader can tell an
opened primary source from a snippet a search engine returned.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    CompletionStatus,
    Confidence,
    EvidenceSourceTier,
    Sufficiency,
)
from ravel.domain.ids import DisplayPrefix, display_id, new_id


class EvidenceSource(Record):
    """One place a claim came from, as it was actually reached.

    The fields that matter most are the ones that are easy to fake:
    `retrieved_at` (when it was read, not when it was written down),
    `content_hash` (the bytes that were read), and `access_status` (whether it
    was read at all).
    """

    source_id: str = Field(default_factory=new_id)
    url: str = Field(min_length=1)
    title: str = ""
    publisher: str = ""
    #: The identifier a reader can resolve independently, when the source has
    #: one. A DOI is recorded only when the source itself carries it; RAVEL
    #: never constructs one.
    doi: str | None = None
    access_status: AccessStatus
    retrieved_at: datetime | None = None
    content_hash: str | None = None
    media_type: str | None = None
    artifact_ref: str | None = None
    snapshot_ref: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _retrieved_means_read(self) -> EvidenceSource:
        """Only a source that was actually read may claim retrieval metadata.

        A `PAYWALLED` source with a `content_hash` would assert that RAVEL read
        bytes it could not reach. Recording the restriction is the honest
        outcome; the check makes the dishonest one unrepresentable.
        """
        if self.access_status is AccessStatus.OK:
            if self.retrieved_at is None:
                raise ValueError(
                    f"source {self.url} is marked OK but has no retrieved_at; a source "
                    "RAVEL could read must record when it read it"
                )
        elif self.content_hash is not None:
            raise ValueError(
                f"source {self.url} is {self.access_status.value} but carries a "
                "content_hash; restricted content was not read, so it has no hash"
            )
        return self

    @property
    def was_read(self) -> bool:
        """Whether RAVEL actually obtained this source's content."""
        return self.access_status is AccessStatus.OK and self.retrieved_at is not None

    @property
    def reference(self) -> str:
        """A citable form: the DOI when the source carries one, else the URL."""
        return f"doi:{self.doi}" if self.doi else self.url


class EvidenceConflict(Record):
    """Two evidence rows that disagree, recorded rather than resolved away.

    Conflicting evidence is a finding. Averaging it, or keeping only the
    convenient half, would hide the uncertainty a decision has to account for.
    """

    conflict_id: str = Field(default_factory=new_id)
    project_id: str
    evidence_refs: tuple[str, ...] = Field(min_length=2)
    description: str = Field(min_length=1)
    resolution: str | None = None
    resolved_by_decision_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)


class Evidence(Record):
    """One claim, its class, and what supports it.

    `claim_class` keeps observation and speculation apart. A `FACT` must cite a
    source that was read; an `INFERENCE` must say what it was inferred from; a
    `HYPOTHESIS` is allowed to rest on nothing yet, which is exactly what makes
    it a hypothesis rather than a finding.
    """

    evidence_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.EVIDENCE))
    project_id: str
    statement: str = Field(min_length=1)
    claim_class: ClaimClass
    source_tier: EvidenceSourceTier
    source_refs: tuple[str, ...] = ()
    conditions: dict[str, str] = Field(default_factory=dict)
    retrieved_at: datetime | None = None
    content_hash: str | None = None
    #: The strongest access status among this row's sources. A claim resting on
    #: a source RAVEL could not open is weaker than the same claim resting on
    #: one it read, and that must be visible where the claim is.
    access_status: AccessStatus = AccessStatus.OK
    conflicts_with: tuple[str, ...] = ()
    confidence: Confidence = Confidence.MEDIUM
    research_task_ref: str | None = None
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _evidence_needs_support(self) -> Evidence:
        if self.claim_class is ClaimClass.FACT and not self.source_refs:
            raise ValueError(
                f"evidence {self.display_id} is a FACT with no source_refs; an "
                "unsupported assertion is a hypothesis, not a fact"
            )
        if self.access_status is not AccessStatus.OK and self.claim_class is ClaimClass.FACT:
            raise ValueError(
                f"evidence {self.display_id} is a FACT resting on a source that was "
                f"{self.access_status.value}; it was not read, so it cannot be a fact"
            )
        if self.evidence_id in self.conflicts_with:
            raise ValueError(f"evidence {self.display_id} lists itself as a conflict")
        return self


class SufficiencyAssessment(Record):
    """Whether the gathered evidence can carry the decision it is for."""

    sufficiency: Sufficiency
    rationale: str = Field(min_length=1)
    gaps: tuple[str, ...] = ()
    would_change_with: tuple[str, ...] = ()


class ResearchRecord(Record):
    """A Research Agent's structured output for one task.

    Completion is a property of the contract, not of the agent's confidence:
    `completion_status` is COMPLETE only when the task's required outputs are
    present. An agent that reports INCOMPLETE with a clear list of unknowns has
    done its job correctly.
    """

    research_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.RESEARCH_TASK))
    project_id: str
    task_id: str
    node_id: str | None = None
    question: str = ""
    facts: tuple[str, ...] = ()
    inferences: tuple[str, ...] = ()
    hypotheses: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()
    sufficiency: SufficiencyAssessment
    suggested_acceptance_criteria: tuple[str, ...] = ()
    recommended_followups: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    completion_status: CompletionStatus
    report: str = ""
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _complete_means_supported(self) -> ResearchRecord:
        if self.completion_status is CompletionStatus.COMPLETE and not self.evidence_refs:
            raise ValueError(
                f"research {self.display_id} is COMPLETE with no evidence; a completed "
                "research task has something to show for it"
            )
        return self
