"""The Evidence Ledger's rules are what keep fabricated claims out of it."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    CompletionStatus,
    Confidence,
    EvidenceSourceTier,
    Sufficiency,
)
from ravel.domain.evidence import (
    Evidence,
    EvidenceConflict,
    EvidenceSource,
    ResearchRecord,
    SufficiencyAssessment,
)


def _source(**overrides: object) -> EvidenceSource:
    defaults: dict[str, object] = {
        "project_id": "proj-a",
        "url": "https://example.org/paper",
        "access_status": AccessStatus.OK,
        "retrieved_at": utcnow(),
        "content_hash": "sha256:abc123",
    }
    defaults.update(overrides)
    return EvidenceSource(**defaults)  # type: ignore[arg-type]


def _evidence(**overrides: object) -> Evidence:
    defaults: dict[str, object] = {
        "project_id": "proj-a",
        "statement": "The enzyme is inhibited at 37C.",
        "claim_class": ClaimClass.FACT,
        "source_tier": EvidenceSourceTier.A,
        "source_refs": ("src-1",),
    }
    defaults.update(overrides)
    return Evidence(**defaults)  # type: ignore[arg-type]


# ── What a source may claim ─────────────────────────────────────────────────


def test_a_read_source_records_when_it_was_read() -> None:
    source = _source()
    assert source.was_read
    assert source.reference == "https://example.org/paper"


def test_a_read_source_without_a_timestamp_is_refused() -> None:
    with pytest.raises(ValidationError, match="no retrieved_at"):
        _source(retrieved_at=None)


@pytest.mark.parametrize(
    "status",
    [
        AccessStatus.PAYWALLED,
        AccessStatus.AUTH_REQUIRED,
        AccessStatus.ACCESS_LIMITED,
        AccessStatus.POLICY_BLOCKED,
    ],
)
def test_a_restricted_source_cannot_carry_a_content_hash(status: AccessStatus) -> None:
    """A hash would assert RAVEL read bytes it could not reach."""
    with pytest.raises(ValidationError, match="carries a content_hash"):
        _source(access_status=status)


@pytest.mark.parametrize(
    "status",
    [AccessStatus.PAYWALLED, AccessStatus.AUTH_REQUIRED, AccessStatus.ACCESS_LIMITED],
)
def test_a_restricted_source_is_recorded_honestly(status: AccessStatus) -> None:
    source = _source(access_status=status, retrieved_at=None, content_hash=None)
    assert not source.was_read
    assert source.access_status is status


def test_a_doi_is_cited_as_a_doi_when_the_source_carries_one() -> None:
    source = _source(doi="10.1000/xyz123")
    assert source.reference == "doi:10.1000/xyz123"


# ── What evidence may claim ─────────────────────────────────────────────────


def test_a_fact_must_cite_a_source() -> None:
    with pytest.raises(ValidationError, match="FACT with no source_refs"):
        _evidence(source_refs=())


def test_a_fact_cannot_rest_on_a_source_that_was_not_read() -> None:
    with pytest.raises(ValidationError, match="resting on a source that was"):
        _evidence(access_status=AccessStatus.PAYWALLED)


def test_an_inference_may_rest_on_nothing_yet() -> None:
    """An inference is derived, so its support is the reasoning, not a source."""
    evidence = _evidence(claim_class=ClaimClass.INFERENCE, source_refs=())
    assert evidence.claim_class is ClaimClass.INFERENCE


def test_a_hypothesis_may_rest_on_nothing_yet() -> None:
    evidence = _evidence(claim_class=ClaimClass.HYPOTHESIS, source_refs=())
    assert evidence.claim_class is ClaimClass.HYPOTHESIS


def test_evidence_cannot_conflict_with_itself() -> None:
    evidence = _evidence()
    with pytest.raises(ValidationError, match="lists itself"):
        _evidence(evidence_id=evidence.evidence_id, conflicts_with=(evidence.evidence_id,))


def test_evidence_gets_a_readable_display_id() -> None:
    assert _evidence().display_id.startswith("EVD-")


def test_a_conflict_names_at_least_two_rows() -> None:
    with pytest.raises(ValidationError):
        EvidenceConflict(project_id="proj-a", evidence_refs=("e1",), description="disagree")


def test_a_conflict_is_open_until_a_decision_resolves_it() -> None:
    conflict = EvidenceConflict(
        project_id="proj-a", evidence_refs=("e1", "e2"), description="Different Ki values."
    )
    assert conflict.resolved_by_decision_ref is None


# ── Research records ────────────────────────────────────────────────────────


def _assessment(**overrides: object) -> SufficiencyAssessment:
    defaults: dict[str, object] = {"sufficiency": Sufficiency.MODERATE, "rationale": "One source."}
    defaults.update(overrides)
    return SufficiencyAssessment(**defaults)  # type: ignore[arg-type]


def test_a_sufficiency_assessment_must_say_why() -> None:
    with pytest.raises(ValidationError):
        SufficiencyAssessment(sufficiency=Sufficiency.WEAK, rationale="")


def _record(**overrides: object) -> ResearchRecord:
    defaults: dict[str, object] = {
        "project_id": "proj-a",
        "task_id": "task-1",
        "sufficiency": _assessment(),
        "completion_status": CompletionStatus.COMPLETE,
        "evidence_refs": ("e1",),
    }
    defaults.update(overrides)
    return ResearchRecord(**defaults)  # type: ignore[arg-type]


def test_a_complete_research_record_shows_its_work() -> None:
    with pytest.raises(ValidationError, match="COMPLETE with no evidence"):
        _record(evidence_refs=())


def test_an_incomplete_record_may_admit_it_found_nothing() -> None:
    """Reporting INCOMPLETE with clear unknowns is doing the job correctly."""
    record = _record(
        completion_status=CompletionStatus.INCOMPLETE,
        evidence_refs=(),
        unknowns=("No primary source located.",),
    )
    assert record.completion_status is CompletionStatus.INCOMPLETE
    assert record.unknowns


def test_confidence_is_a_band_not_a_number() -> None:
    """A spurious 0.83 would read as precision the evidence does not have."""
    assert _evidence(confidence=Confidence.HIGH).confidence is Confidence.HIGH
    with pytest.raises(ValidationError):
        _evidence(confidence=0.83)
