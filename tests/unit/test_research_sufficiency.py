"""How a body of evidence is measured, and what the measurement decides.

`docs/05_RESEARCH_AND_EVIDENCE.md` §7 asks for an *assessment* rather than a
paper count, and then lists six things to consider. These tests pin down what
"consider" was turned into: each axis's finding at the boundary where it
changes, and the rule that combines them.

Two properties get the most attention, because they are the ones an
implementation gets quietly wrong:

- **The floor, not the average.** Six good axes and one bad one is not five
  sixths of a good answer. Every test that asserts a category also asserts the
  axes that were fine, so a change to an averaging scheme would show up as a
  category that improved while nothing was fixed.
- **`UNKNOWN` is a gap.** An axis nobody measured is not an axis that came out
  well, and an assessment that treated "no conditions were recorded" as a pass
  would rate a body of unexamined evidence as STRONG.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ravel.domain.enums import AccessStatus, ClaimClass, EvidenceSourceTier, Sufficiency
from ravel.domain.evidence import Evidence, EvidenceConflict, EvidenceSource
from ravel.research.sufficiency import Consideration, Finding, assess

PROJECT = "proj"
DIGEST = "sha256:" + "a" * 64


def _source(
    url: str = "https://journal.example.org/a",
    *,
    source_id: str = "src-1",
    hashed: bool = True,
    stored: bool = True,
    restricted: bool = False,
) -> EvidenceSource:
    """A source as the gateway would have recorded it.

    `hashed` and `stored` are separate because they answer different questions:
    a hash proves the bytes have not changed since they were read, a snapshot
    proves what they were. A source can have the first without the second.
    """
    if restricted:
        return EvidenceSource(
            source_id=source_id,
            project_id=PROJECT,
            url=url,
            access_status=AccessStatus.PAYWALLED,
            retrieved_at=datetime.now(UTC),
        )
    return EvidenceSource(
        source_id=source_id,
        project_id=PROJECT,
        url=url,
        access_status=AccessStatus.OK,
        retrieved_at=datetime.now(UTC),
        content_hash=DIGEST if hashed else None,
        snapshot_ref=f"snapshots/{source_id}" if stored else None,
    )


def _claim(
    *,
    evidence_id: str = "ev-1",
    statement: str = "the catalyst retained 91 percent of its activity after 500 hours",
    claim_class: ClaimClass = ClaimClass.FACT,
    tier: EvidenceSourceTier = EvidenceSourceTier.A,
    sources: tuple[str, ...] = ("src-1",),
    conditions: dict[str, str] | None = None,
    conflicts_with: tuple[str, ...] = (),
) -> Evidence:
    """A claim, with conditions recorded unless a test passes `{}`."""
    return Evidence(
        evidence_id=evidence_id,
        project_id=PROJECT,
        statement=statement,
        claim_class=claim_class,
        source_tier=tier,
        source_refs=sources,
        conditions={"temperature": "300 K"} if conditions is None else conditions,
        conflicts_with=conflicts_with,
        access_status=(
            AccessStatus.PAYWALLED
            if claim_class is ClaimClass.INFERENCE and not sources
            else AccessStatus.OK
        ),
    )


def _conflict(*, resolved: bool = False) -> EvidenceConflict:
    return EvidenceConflict(
        project_id=PROJECT,
        evidence_refs=("ev-1", "ev-2"),
        description="one run reports 91 percent retention, the other reports 40",
        resolution="the second ran at 600 K" if resolved else None,
    )


def _everything_good() -> tuple[list[Evidence], list[EvidenceSource]]:
    """A ledger with no weakness on any axis, used as the baseline to break."""
    sources = [
        _source("https://journal.example.org/a", source_id="src-1"),
        _source("https://publisher.example.org/b", source_id="src-2"),
    ]
    claims = [
        _claim(evidence_id="ev-1", sources=("src-1",)),
        _claim(
            evidence_id="ev-2",
            statement="retention was 89 percent in the second run",
            sources=("src-2",),
            tier=EvidenceSourceTier.B,
        ),
    ]
    return claims, sources


def _findings(assessment) -> dict[Consideration, Finding]:
    return {m.consideration: m.finding for m in assessment.measurements}


# ── The category, and the rule that produces it ─────────────────────────────


def test_an_empty_ledger_is_insufficient_and_says_so_on_every_axis() -> None:
    """Nothing to assess is still an assessment.

    A research task that found nothing is a normal outcome, and the assessment
    has to survive it rather than raise — it is needed most exactly then.
    """
    assessment = assess()

    assert assessment.sufficiency is Sufficiency.INSUFFICIENT
    assert len(assessment.measurements) == len(Consideration)


def test_every_axis_is_measured_exactly_once() -> None:
    """Totality, so that no axis can be dropped from the category by accident.

    The category is computed from the set of findings, so an axis that stopped
    being measured would silently stop constraining anything.
    """
    assessment = assess(claims=[_claim()], sources=[_source()])

    measured = [m.consideration for m in assessment.measurements]
    assert sorted(measured) == sorted(Consideration)
    assert len(measured) == len(set(measured))


def test_a_ledger_with_no_weakness_on_any_axis_is_strong() -> None:
    claims, sources = _everything_good()

    assessment = assess(claims=claims, sources=sources)

    assert _findings(assessment) == dict.fromkeys(Consideration, Finding.MET)
    assert assessment.sufficiency is Sufficiency.STRONG


def test_one_unmet_axis_is_not_averaged_away_by_five_good_ones() -> None:
    """The floor, stated as a test.

    Five axes come out MET and one UNMET, and the category is WEAK. An average
    would call this MODERATE or better; a body of evidence is not better than
    its worst-supported claim.
    """
    claims, sources = _everything_good()
    claims.append(
        _claim(
            evidence_id="ev-3",
            statement="the coating reduces wear by half",
            sources=("src-1",),
            tier=EvidenceSourceTier.D,
        )
    )

    assessment = assess(claims=claims, sources=sources)
    findings = _findings(assessment)

    assert findings[Consideration.AUTHORITY] is Finding.UNMET
    assert [axis for axis, finding in findings.items() if finding is Finding.MET] == [
        Consideration.INDEPENDENCE,
        Consideration.DIRECTNESS,
        Consideration.CONDITION_MATCH,
        Consideration.REPRODUCIBILITY,
        Consideration.CONFLICT,
    ]
    assert assessment.sufficiency is Sufficiency.WEAK


def test_an_unmeasured_axis_holds_the_category_down_rather_than_passing_it() -> None:
    """`UNKNOWN` is a gap, and the difference from `UNMET` is only the fix.

    Nothing here is wrong; something here is unexamined. The category has to
    reflect that, or a ledger nobody recorded conditions for would rate STRONG.
    """
    claims = [_claim(conditions={})]
    sources = [_source()]

    assessment = assess(claims=claims, sources=sources)

    assert _findings(assessment)[Consideration.CONDITION_MATCH] is Finding.UNKNOWN
    assert assessment.sufficiency is Sufficiency.WEAK


def test_a_partial_axis_is_moderate_rather_than_weak() -> None:
    """The middle category exists, and is reached by a fixable shortfall.

    One publisher is not a mistake and is not fatal — it is a body of evidence
    that would be stronger with a second account, which is what MODERATE says.
    """
    claims, _ = _everything_good()
    only_one = [_source("https://journal.example.org/a", source_id="src-1")]
    claims = [claim for claim in claims if claim.source_refs == ("src-1",)]

    assessment = assess(claims=claims, sources=only_one)

    assert _findings(assessment)[Consideration.INDEPENDENCE] is Finding.PARTIAL
    assert assessment.sufficiency is Sufficiency.MODERATE


def test_nothing_readable_is_insufficient_however_much_of_it_there_is() -> None:
    """A paywall that answered is not a source RAVEL read.

    Ten of them are not five sources. This is the one rule that does not go
    through the floor: `_directness` is UNMET here too, and INSUFFICIENT is
    returned ahead of it because no amount of the remaining evidence
    compensates for having read nothing.
    """
    sources = [
        _source(f"https://publisher.example.org/{n}", source_id=f"src-{n}", restricted=True)
        for n in range(10)
    ]
    claims = [
        _claim(
            evidence_id=f"ev-{n}",
            statement=f"claim number {n}",
            claim_class=ClaimClass.INFERENCE,
            sources=(f"src-{n}",),
        )
        for n in range(10)
    ]

    assessment = assess(claims=claims, sources=sources)
    findings = _findings(assessment)

    assert all(not source.was_read for source in sources)
    assert findings[Consideration.DIRECTNESS] is Finding.UNMET
    assert findings[Consideration.INDEPENDENCE] is Finding.NOT_APPLICABLE
    assert findings[Consideration.REPRODUCIBILITY] is Finding.NOT_APPLICABLE
    assert assessment.sufficiency is Sufficiency.INSUFFICIENT


# ── Independence ────────────────────────────────────────────────────────────


def test_two_documents_from_one_publisher_count_as_one_source() -> None:
    """The proxy errs towards understating independence, and says which hosts.

    Publisher host is not the same as research group, and the measurement is
    recorded as the approximation it is — with the hosts named, so a reader can
    see what was counted rather than being asked to trust a number.
    """
    sources = [
        _source("https://journal.example.org/a", source_id="src-1"),
        _source("https://journal.example.org/b", source_id="src-2"),
    ]
    claims = [
        _claim(evidence_id="ev-1", sources=("src-1",)),
        _claim(evidence_id="ev-2", statement="a second result", sources=("src-2",)),
    ]

    assessment = assess(claims=claims, sources=sources)
    measurement = assessment.measurement(Consideration.INDEPENDENCE)

    assert measurement.finding is Finding.PARTIAL
    assert "journal.example.org" in measurement.detail


def test_the_same_host_spelled_with_www_is_the_same_publisher() -> None:
    """Normalized, so that a redirect cannot manufacture a second source."""
    sources = [
        _source("https://journal.example.org/a", source_id="src-1"),
        _source("https://www.journal.example.org/a", source_id="src-2"),
    ]

    assessment = assess(sources=sources)

    assert "2 publishers" not in assessment.measurement(Consideration.INDEPENDENCE).detail


def test_a_source_that_could_not_be_read_does_not_count_as_a_publisher() -> None:
    """Independence is measured over what was read, not over what was listed.

    Counting a paywalled source as an independent account would let a list of
    refusals stand in for corroboration.
    """
    sources = [
        _source("https://journal.example.org/a", source_id="src-1"),
        _source("https://elsewhere.example.org/b", source_id="src-2", restricted=True),
    ]

    assessment = assess(sources=sources)

    assert assessment.measurement(Consideration.INDEPENDENCE).finding is Finding.PARTIAL


# ── Authority ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("tier", [EvidenceSourceTier.A, EvidenceSourceTier.B])
def test_a_peer_reviewed_or_standards_source_is_enough(tier: EvidenceSourceTier) -> None:
    assessment = assess(claims=[_claim(tier=tier)], sources=[_source()])

    assert assessment.measurement(Consideration.AUTHORITY).finding is Finding.MET


@pytest.mark.parametrize("tier", [EvidenceSourceTier.C, EvidenceSourceTier.D])
def test_a_claim_resting_on_a_weak_source_holds_authority_down(
    tier: EvidenceSourceTier,
) -> None:
    """The weakest load-bearing claim decides, not the strongest.

    A conclusion supported by three Tier A papers and one blog post has not
    been established for the part that rests on the blog post, and the detail
    names the tier so the fix is obvious.
    """
    claims = [
        _claim(evidence_id="ev-1", tier=EvidenceSourceTier.A),
        _claim(evidence_id="ev-2", statement="a weaker claim", tier=tier),
    ]

    assessment = assess(claims=claims, sources=[_source()])
    measurement = assessment.measurement(Consideration.AUTHORITY)

    assert measurement.finding is Finding.UNMET
    assert f"tier {tier.value}" in measurement.detail


def test_a_hypothesis_does_not_hold_authority_down() -> None:
    """A hypothesis is allowed to rest on nothing, which is what makes it one.

    Were hypotheses counted, every record that proposed a testable explanation
    would be rated WEAK for having proposed it.
    """
    claims = [
        _claim(evidence_id="ev-1", tier=EvidenceSourceTier.A),
        _claim(
            evidence_id="ev-2",
            statement="the mechanism is surface reconstruction",
            claim_class=ClaimClass.HYPOTHESIS,
            tier=EvidenceSourceTier.D,
            sources=(),
        ),
    ]

    assessment = assess(claims=claims, sources=[_source()])

    assert assessment.measurement(Consideration.AUTHORITY).finding is Finding.MET


def test_authority_is_unknown_when_no_claim_was_recorded() -> None:
    """Sources without claims measure nothing about authority.

    There is no claim, so there is no tier anything rests on — and calling that
    MET would report an assessment that was never made.
    """
    assessment = assess(sources=[_source()])

    assert assessment.measurement(Consideration.AUTHORITY).finding is Finding.UNKNOWN


# ── Directness ──────────────────────────────────────────────────────────────


def test_a_claim_resting_on_a_source_that_was_not_read_is_not_direct() -> None:
    """The axis the project is arranged around.

    A claim resting on a source RAVEL could not open is a claim resting on a
    title, and it must not count towards sufficiency as though the source had
    been read.
    """
    claims = [_claim(claim_class=ClaimClass.INFERENCE, sources=("src-paid",))]
    sources = [_source("https://publisher.example.org/paid", source_id="src-paid", restricted=True)]

    assessment = assess(claims=claims, sources=sources)
    measurement = assessment.measurement(Consideration.DIRECTNESS)

    assert measurement.finding is Finding.UNMET
    assert "1 of 1" in measurement.detail


def test_a_claim_whose_source_is_no_longer_in_the_ledger_is_not_direct() -> None:
    """The dangling reference is reported, not skipped.

    Refs are resolved against the ledger that was passed, so a claim citing a
    source nobody supplied counts as unsupported rather than as vacuously fine.
    """
    assessment = assess(claims=[_claim(sources=("src-missing",))], sources=[_source()])

    assert assessment.measurement(Consideration.DIRECTNESS).finding is Finding.UNMET


def test_a_source_that_was_read_makes_its_claims_direct() -> None:
    claims, sources = _everything_good()

    assessment = assess(claims=claims, sources=sources)

    assert assessment.measurement(Consideration.DIRECTNESS).finding is Finding.MET


# ── Condition match ─────────────────────────────────────────────────────────


def test_conditions_recorded_on_every_claim_is_met() -> None:
    claims, sources = _everything_good()

    assessment = assess(claims=claims, sources=sources)

    assert assessment.measurement(Consideration.CONDITION_MATCH).finding is Finding.MET


def test_conditions_recorded_on_some_claims_is_partial() -> None:
    """Partial rather than UNKNOWN: some of the check is possible, not none."""
    claims = [
        _claim(evidence_id="ev-1"),
        _claim(evidence_id="ev-2", statement="another claim", conditions={}),
    ]

    assessment = assess(claims=claims, sources=[_source()])
    measurement = assessment.measurement(Consideration.CONDITION_MATCH)

    assert measurement.finding is Finding.PARTIAL
    assert "1 of 2" in measurement.detail


def test_the_conditions_axis_says_what_it_could_not_check() -> None:
    """RAVEL cannot compare a claim's conditions with a decision's, and says so.

    The measurement is about whether the conditions are present, which is what
    makes that comparison possible for whoever knows the decision.
    """
    assessment = assess(claims=[_claim(conditions={})], sources=[_source()])
    measurement = assessment.measurement(Consideration.CONDITION_MATCH)

    assert measurement.finding is Finding.UNKNOWN
    assert "cannot check that they match its own" in measurement.detail


# ── Reproducibility ─────────────────────────────────────────────────────────


def test_a_hash_without_a_stored_snapshot_is_partial() -> None:
    """A hash proves the bytes have not changed; it does not prove what they were.

    Without a snapshot the claim rests on the original URL still answering, and
    the measurement says how many are in that position.
    """
    sources = [
        _source("https://journal.example.org/a", source_id="src-1"),
        _source("https://publisher.example.org/b", source_id="src-2", stored=False),
    ]

    assessment = assess(sources=sources)
    measurement = assessment.measurement(Consideration.REPRODUCIBILITY)

    assert measurement.finding is Finding.PARTIAL
    assert "1 of 2" in measurement.detail
    assert "still answering" in measurement.detail


def test_a_source_with_no_hash_at_all_is_not_reproducible() -> None:
    assessment = assess(sources=[_source(hashed=False, stored=False)])

    assert assessment.measurement(Consideration.REPRODUCIBILITY).finding is Finding.UNMET


def test_a_hash_is_not_required_of_a_source_that_was_never_read() -> None:
    """A restricted source has no hash by construction, and that is not a fault.

    `EvidenceSource` refuses a content hash on a source that was not read, so
    counting them here would make every ledger containing a paywall fail this
    axis — and fail it for the wrong reason.
    """
    assessment = assess(sources=[_source(restricted=True)])

    assert assessment.measurement(Consideration.REPRODUCIBILITY).finding is Finding.NOT_APPLICABLE


# ── Conflict ────────────────────────────────────────────────────────────────


def test_no_recorded_conflict_is_met_rather_than_unknown() -> None:
    """ "Nothing disagrees" is a finding, and a different one from "unchecked".

    Reading an empty ledger's silence as UNKNOWN would make every assessment
    WEAK, including the ones where the sources genuinely agree.
    """
    assessment = assess(claims=[_claim()], sources=[_source()])

    assert assessment.measurement(Consideration.CONFLICT).finding is Finding.MET


def test_an_unresolved_conflict_holds_the_category_down() -> None:
    """Recorded and unresolved is weaker than either alone.

    `docs/05` §9 exists because averaging a disagreement away is the tempting
    failure; this is where the cost of not doing so is paid.
    """
    claims, sources = _everything_good()

    assessment = assess(claims=claims, sources=sources, conflicts=[_conflict()])
    measurement = assessment.measurement(Consideration.CONFLICT)

    assert measurement.finding is Finding.UNMET
    assert "1 of 1" in measurement.detail
    assert assessment.sufficiency is Sufficiency.WEAK


def test_a_resolved_conflict_is_met() -> None:
    """Resolving it is what the record is for, so it stops counting against."""
    claims, sources = _everything_good()

    assessment = assess(claims=claims, sources=sources, conflicts=[_conflict(resolved=True)])

    assert assessment.measurement(Consideration.CONFLICT).finding is Finding.MET
    assert assessment.sufficiency is Sufficiency.STRONG


# ── What the assessment hands on ────────────────────────────────────────────


def test_the_gaps_name_the_axes_the_category_rests_on() -> None:
    """A category with no working underneath it is a number taken on faith."""
    claims = [_claim(tier=EvidenceSourceTier.D)]

    assessment = assess(claims=claims, sources=[_source()])

    assert assessment.sufficiency is Sufficiency.WEAK
    assert len(assessment.gaps) == 1
    assert assessment.gaps[0].startswith(f"{Consideration.AUTHORITY.value}: ")


def test_the_remedies_are_things_a_researcher_can_do() -> None:
    """Stated as actions rather than as absences: a gap nobody can act on is a complaint.

    Every axis is set up to pass except authority, so the one remedy returned is
    the one the one gap asks for — which is what makes this a check that the
    two are derived from the same measurement rather than kept in step by hand.
    """
    claims, sources = _everything_good()
    claims.append(
        _claim(
            evidence_id="ev-3",
            statement="the coating reduces wear by half",
            sources=("src-1",),
            tier=EvidenceSourceTier.D,
        )
    )

    assessment = assess(claims=claims, sources=sources)

    assert len(assessment.gaps) == 1
    assert assessment.would_change_with == (
        "a peer-reviewed or standards-body source for the claim it is load-bearing for",
    )


def test_a_partial_axis_also_says_what_would_improve_it() -> None:
    """Held down is not the same as failed, and both are worth acting on."""
    claims, _ = _everything_good()
    assessment = assess(
        claims=[claim for claim in claims if claim.source_refs == ("src-1",)],
        sources=[_source("https://journal.example.org/a", source_id="src-1")],
    )

    assert Consideration.INDEPENDENCE in [
        axis
        for axis in Consideration
        if assessment.measurement(axis).finding is Finding.PARTIAL
    ]
    assert any("second source" in remedy for remedy in assessment.would_change_with)


def test_each_axis_may_be_looked_up_by_name() -> None:
    assessment = assess()

    assert assessment.measurement(Consideration.CONFLICT).consideration is Consideration.CONFLICT
    with pytest.raises(KeyError):
        assessment.measurement("not an axis")  # type: ignore[arg-type]


def test_the_storable_assessment_carries_every_measurement() -> None:
    """The stored record is where a later reader meets the working.

    `SufficiencyAssessment.rationale` is the only free-text field on it, so the
    per-axis lines have to be there or the reasoning is lost when the
    `Assessment` object goes out of scope.
    """
    claims, sources = _everything_good()

    stored = assess(claims=claims, sources=sources).as_assessment()

    assert stored.sufficiency is Sufficiency.STRONG
    assert stored.gaps == ()
    for consideration in Consideration:
        assert consideration.value in stored.rationale
    assert "met" in stored.rationale


def test_the_storable_assessment_keeps_its_gaps_and_remedies() -> None:
    assessment = assess(claims=[_claim(tier=EvidenceSourceTier.C)], sources=[_source()])

    stored = assessment.as_assessment()

    assert stored.sufficiency is Sufficiency.WEAK
    assert stored.gaps == assessment.gaps
    assert stored.would_change_with == assessment.would_change_with


def test_extra_context_is_appended_to_the_rationale() -> None:
    """A caller with something to add does not have to overwrite the working."""
    assessment = assess(claims=[_claim()], sources=[_source()])

    stored = assessment.as_assessment(extra_rationale="the second run is still in progress")

    assert stored.rationale.endswith("the second run is still in progress")
    assert Consideration.INDEPENDENCE.value in stored.rationale
