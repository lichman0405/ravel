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
    claim_access,
    claim_tier,
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


# ── What a claim's standing is derived from ─────────────────────────────────
#
# A claim does not carry a tier because somebody chose one: the tier is read
# off the sources behind it. These are the rules that read it, and the case
# that matters most is the unreadable source — a claim cannot be strengthened
# by a paper RAVEL was unable to open.


def test_a_claim_is_as_strong_as_the_best_source_it_was_read_from() -> None:
    """The strongest *read* source, not the average and not the first.

    A claim supported by a journal article and by a blog is supported by the
    journal article; the blog does not weaken it, and it does not strengthen it
    either.
    """
    strong = _source(tier=EvidenceSourceTier.A)
    weak = _source(url="https://blog.example.org/x", tier=EvidenceSourceTier.D)

    assert claim_tier([weak, strong]) is EvidenceSourceTier.A
    assert claim_tier([strong, weak]) is EvidenceSourceTier.A


def test_a_source_ravel_could_not_read_cannot_strengthen_a_claim() -> None:
    """A paywalled tier-A paper is not a tier-A reading.

    This is the derivation doing its job: a model that could cite the DOI of a
    paper it never opened would otherwise be able to file the claim at A.
    """
    read = _source(tier=EvidenceSourceTier.D)
    unread = _source(
        url="https://pubs.acs.org/doi/10.1021/example",
        tier=EvidenceSourceTier.A,
        access_status=AccessStatus.PAYWALLED,
        retrieved_at=None,
        content_hash=None,
    )

    assert claim_tier([read, unread]) is EvidenceSourceTier.D


def test_a_claim_nobody_could_support_still_has_a_tier() -> None:
    """Its sources are all unreadable, so the claim is rated on what it names.

    A hypothesis resting on a paywalled paper and one resting on a blog are
    different propositions, and the tier says which was named even though
    neither was read.
    """
    paywalled = _source(
        access_status=AccessStatus.PAYWALLED,
        retrieved_at=None,
        content_hash=None,
        tier=EvidenceSourceTier.B,
    )

    assert claim_tier([paywalled]) is EvidenceSourceTier.B


def test_a_source_with_no_recorded_tier_rates_as_the_weakest() -> None:
    """Rows written before the tier was stored are not promoted by default."""
    assert claim_tier([_source(tier=None)]) is EvidenceSourceTier.D


def test_a_claim_with_no_sources_has_no_tier_to_derive() -> None:
    """Refused rather than defaulted: the caller decides what "unsupported" means."""
    with pytest.raises(ValueError, match="names none"):
        claim_tier([])
    with pytest.raises(ValueError, match="names none"):
        claim_access([])


def test_access_is_ok_when_any_named_source_was_read() -> None:
    """A second source RAVEL could not open does not make the claim unread."""
    unread = _source(
        access_status=AccessStatus.AUTH_REQUIRED, retrieved_at=None, content_hash=None
    )

    assert claim_access([unread, _source()]) is AccessStatus.OK
    assert claim_access([_source(), unread]) is AccessStatus.OK


def test_access_reports_why_nothing_could_be_checked() -> None:
    """The first unreadable source's own reason, in the order the claim named it.

    Not a ranking of the four kinds: which restriction is "worse" is a judgement
    RAVEL would be inventing, and the useful fact is what happened.
    """
    paywalled = _source(
        access_status=AccessStatus.PAYWALLED, retrieved_at=None, content_hash=None
    )
    limited = _source(
        url="https://slow.example.org/x",
        access_status=AccessStatus.ACCESS_LIMITED,
        retrieved_at=None,
        content_hash=None,
    )

    assert claim_access([paywalled, limited]) is AccessStatus.PAYWALLED
    assert claim_access([limited, paywalled]) is AccessStatus.ACCESS_LIMITED


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
