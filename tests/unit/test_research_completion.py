"""The research completion contract, and the difference it draws.

`docs/05_RESEARCH_AND_EVIDENCE.md` §11 lists nine things that must hold before a
RESEARCH node can be COMPLETE, and then says what to do when they do not:
"continue research, or return INCOMPLETE with explicit gaps". These tests are
about that second branch being usable — the gaps have to be specific enough for
a Master to act on.

The distinction the file keeps coming back to is that **COMPLETE is a statement
about the record, not about the evidence**. A task that gathered weak evidence
and said so is complete; a task that gathered the same evidence and called it
strong is not. So several tests here assert a COMPLETE record whose sufficiency
is WEAK, and several assert an INCOMPLETE record whose evidence is fine — the
two judgments answer different questions and must not collapse into one.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    CompletionStatus,
    EvidenceSourceTier,
    Sufficiency,
)
from ravel.domain.evidence import Evidence, EvidenceConflict, EvidenceSource, ResearchRecord
from ravel.research.completion import REQUIREMENTS, Draft, build, judge, unmeasured
from ravel.research.sufficiency import Consideration, Finding, assess

PROJECT = "proj"
DIGEST = "sha256:" + "a" * 64

QUESTION = "Does the catalyst retain its activity after 500 hours under load?"


def _source(
    url: str = "https://journal.example.org/a",
    *,
    source_id: str = "src-1",
    restricted: bool = False,
) -> EvidenceSource:
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
        content_hash=DIGEST,
        snapshot_ref=f"snapshots/{source_id}",
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
    restricted: bool = False,
) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        project_id=PROJECT,
        statement=statement,
        claim_class=claim_class,
        source_tier=tier,
        source_refs=sources,
        conditions={"temperature": "300 K"} if conditions is None else conditions,
        conflicts_with=conflicts_with,
        access_status=AccessStatus.PAYWALLED if restricted else AccessStatus.OK,
    )


def _conflict(*, resolved: bool = False) -> EvidenceConflict:
    return EvidenceConflict(
        project_id=PROJECT,
        evidence_refs=("ev-1", "ev-2"),
        description="one run reports 91 percent retention, the other reports 40",
        resolution="the second ran at 600 K" if resolved else None,
    )


def _complete() -> Draft:
    """A draft that satisfies all nine requirements.

    Two publishers, so that no axis of the sufficiency assessment is held down
    by anything other than what a test does to it. Every test below starts here
    and breaks one thing, so what it asserts is attributable to that one thing.
    """
    return Draft(
        question=QUESTION,
        claims=[
            _claim(evidence_id="ev-1", sources=("src-1",)),
            _claim(
                evidence_id="ev-2",
                statement="retention was 89 percent in the second run",
                tier=EvidenceSourceTier.B,
                sources=("src-2",),
            ),
        ],
        sources=[
            _source("https://journal.example.org/a", source_id="src-1"),
            _source("https://publisher.example.org/b", source_id="src-2"),
        ],
        unknowns=("nothing is known about the 5000-hour regime",),
        recommended_followups=("extend the run to 5000 hours",),
        suggested_acceptance_criteria=("retention >= 80 percent at 500 hours",),
        report="The catalyst retained most of its activity; the long-run regime is untested.",
        task_id="task-1",
        node_id="node-1",
    )


def _assessment(draft: Draft):
    return assess(claims=draft.claims, sources=draft.sources, conflicts=draft.conflicts)


def _judge(draft: Draft):
    return judge(draft, _assessment(draft))


# ── The contract, met ───────────────────────────────────────────────────────


def test_a_draft_that_meets_every_requirement_is_complete() -> None:
    """The baseline the rest of the file breaks one requirement at a time."""
    draft = _complete()

    completion = _judge(draft)

    assert completion.status is CompletionStatus.COMPLETE
    assert completion.unmet == ()
    assert completion.detail == ()
    assert completion.is_complete
    assert _assessment(draft).sufficiency is Sufficiency.STRONG


def test_the_requirements_are_named_rather_than_numbered() -> None:
    """A gap is reported against the requirement it failed.

    "Incomplete" on its own tells a Master nothing about whether to keep
    researching, narrow the question, or proceed and record the risk.
    """
    completion = _judge(Draft(question=""))

    assert set(completion.unmet) <= set(REQUIREMENTS)
    assert len(REQUIREMENTS) == len(set(REQUIREMENTS)) == 9


# ── The requirements, each broken on its own ────────────────────────────────


def test_a_draft_with_no_question_is_not_complete() -> None:
    completion = _judge(Draft(question="   ", recommended_followups=("look again",)))

    assert REQUIREMENTS[0] in completion.unmet
    assert "states no question" in " ".join(completion.detail)


def test_a_fact_citing_a_source_nobody_supplied_is_not_complete() -> None:
    """The hole the domain validators cannot close.

    `Evidence` refuses a FACT with no `source_refs`, so a fact always names
    something. Whether that something is in the ledger is a question about the
    draft, and it is the difference between provenance and a plausible-looking
    identifier.
    """
    draft = _complete()
    draft.claims.append(_claim(evidence_id="ev-3", sources=("src-ghost",)))

    completion = _judge(draft)

    assert REQUIREMENTS[1] in completion.unmet
    assert "do not rest on a source RAVEL read" in " ".join(completion.detail)
    # The sufficiency assessment caught the same thing from its own side; the
    # two are separate judgments of the same ledger.
    assert _assessment(draft).sufficiency is Sufficiency.WEAK


def test_a_fact_resting_on_a_source_that_was_not_read_is_not_a_fact() -> None:
    """Refused at construction, so the contract never has to check it.

    Recorded as a test because it is the reason requirement 2 checks the
    direction it does: the domain makes the dishonest row unrepresentable, and
    what is left for the contract is whether the identifiers resolve.
    """
    with pytest.raises(ValueError, match="was not read"):
        _claim(
            claim_class=ClaimClass.FACT,
            sources=("src-paid",),
            restricted=True,
        )


def test_insufficient_evidence_is_not_complete() -> None:
    """Nothing readable means the question was not answered, whatever was written.

    The record may hold a long and careful account of sources that could not be
    opened. It cannot be COMPLETE, because the things §11 asks it to establish
    are not established by reading titles.
    """
    draft = _complete()
    draft.sources = [_source("https://publisher.example.org/paid", restricted=True)]
    draft.claims = [
        _claim(
            evidence_id="ev-1",
            claim_class=ClaimClass.INFERENCE,
            sources=("src-1",),
            restricted=True,
        )
    ]

    completion = _judge(draft)

    assert REQUIREMENTS[2] in completion.unmet
    assert "cannot carry a decision" in " ".join(completion.detail)
    assert _assessment(draft).sufficiency is Sufficiency.INSUFFICIENT


def test_a_disagreement_named_by_a_claim_but_not_written_down_is_not_complete() -> None:
    """`conflicts_with` is a pointer; the conflict is the record.

    An evidence row that says it disagrees with another, with no conflict row
    saying what about, is a disagreement nobody can review.
    """
    draft = _complete()
    draft.claims[0] = _claim(evidence_id="ev-1", sources=("src-1",), conflicts_with=("ev-2",))

    completion = _judge(draft)

    assert REQUIREMENTS[3] in completion.unmet
    assert "no conflict record" in " ".join(completion.detail)


def test_a_disagreement_that_was_written_down_satisfies_the_requirement() -> None:
    """Writing it down is what §11 asks for; resolving it is not.

    The record is COMPLETE and its sufficiency is WEAK. Both are true, and
    collapsing them would either hide the disagreement or block the task that
    found it.
    """
    draft = _complete()
    draft.claims[0] = _claim(evidence_id="ev-1", sources=("src-1",), conflicts_with=("ev-2",))
    draft.conflicts = [_conflict()]

    completion = _judge(draft)

    assert REQUIREMENTS[3] not in completion.unmet
    assert completion.status is CompletionStatus.COMPLETE
    assert draft and _assessment(draft).sufficiency is Sufficiency.WEAK


def test_the_same_statement_in_two_claim_classes_is_not_complete() -> None:
    """The classes are the separation §8 asks for, so a statement belongs to one.

    Filed twice, the weaker copy becomes the record's position and the stronger
    becomes its evidence, which is a way of asserting something while denying
    having asserted it.
    """
    draft = _complete()
    draft.claims.append(
        _claim(evidence_id="ev-3", claim_class=ClaimClass.INFERENCE, sources=("src-1",))
    )

    completion = _judge(draft)

    assert REQUIREMENTS[4] in completion.unmet
    assert "more than one class" in " ".join(completion.detail)


def test_inferences_that_record_no_conditions_are_not_complete() -> None:
    """A conclusion with no stated scope is a conclusion nobody can apply.

    Condition match is one of the six considerations in §7, and it can only be
    measured if the conditions are there to measure.
    """
    draft = _complete()
    draft.claims = [
        _claim(evidence_id="ev-1", claim_class=ClaimClass.INFERENCE, conditions={}),
    ]

    completion = _judge(draft)

    assert REQUIREMENTS[4] in completion.unmet
    assert "conditions it holds under" in " ".join(completion.detail)


def test_one_inference_that_records_its_conditions_is_enough() -> None:
    """The check is on the reasoning, not on every line of it.

    A single inference carrying its conditions tells a reader where the
    reasoning stops applying, which is what the requirement is for.
    """
    draft = _complete()
    draft.claims.append(
        _claim(
            evidence_id="ev-3",
            statement="activity loss slows after the first 200 hours",
            claim_class=ClaimClass.INFERENCE,
            sources=("src-1",),
        )
    )

    assert REQUIREMENTS[4] not in _judge(draft).unmet


def test_a_task_that_found_nothing_outstanding_is_not_complete() -> None:
    """ "No unknowns and no gaps" is a claim about the researcher, not the world.

    The check is deliberately one-sided. Declaring unknowns is always safe;
    declaring none while the assessment also found none is the shape of a
    record that stopped looking, and it is the one §11's "unknowns explicitly
    declared" exists to catch.
    """
    draft = _complete()
    draft.unknowns = ()

    completion = _judge(draft)

    assert REQUIREMENTS[5] in completion.unmet
    assert _assessment(draft).gaps == (), "the setup only works if the axes are all met"


def test_acceptance_criteria_with_nothing_observed_behind_them_are_not_complete() -> None:
    """A criterion is only a benchmark if something was measured.

    With nothing observed, a suggested threshold is an expectation — it can
    still be proposed, but it cannot reach a contract looking like a standard
    the result will be held to.
    """
    draft = _complete()
    draft.claims = [
        _claim(
            evidence_id="ev-1",
            claim_class=ClaimClass.HYPOTHESIS,
            sources=(),
            conditions={},
        )
    ]

    completion = _judge(draft)

    assert REQUIREMENTS[6] in completion.unmet
    assert "expectations rather than" in " ".join(completion.detail)


def test_a_task_that_recommends_no_followups_is_not_complete() -> None:
    """Complete is not the same as finished, and §11 asks for both.

    A record that established what it set out to establish still owes the
    Master the next question, or the DAG has nowhere to go.
    """
    draft = _complete()
    draft.recommended_followups = ()

    assert REQUIREMENTS[7] in _judge(draft).unmet


def test_a_draft_with_no_evidence_at_all_is_not_complete() -> None:
    draft = _complete()
    draft.claims = []
    draft.sources = []
    draft.unknowns = ()

    completion = _judge(draft)

    assert REQUIREMENTS[8] in completion.unmet
    assert REQUIREMENTS[2] in completion.unmet
    assert completion.status is CompletionStatus.INCOMPLETE


# ── What `build` writes ─────────────────────────────────────────────────────


def test_the_claims_are_recorded_under_their_own_classes() -> None:
    """The record's three tuples are a projection of the ledger, not a retelling.

    §12 asks for a structured record and a readable report as two views of one
    thing. Deriving the tuples from the evidence rows is what keeps them from
    drifting apart.
    """
    draft = _complete()
    draft.claims.append(
        _claim(
            evidence_id="ev-3",
            statement="the loss is caused by surface reconstruction",
            claim_class=ClaimClass.HYPOTHESIS,
            sources=(),
            conditions={},
        )
    )
    draft.claims.append(
        _claim(
            evidence_id="ev-4",
            statement="activity loss slows after the first 200 hours",
            claim_class=ClaimClass.INFERENCE,
            sources=("src-1",),
        )
    )

    record = build(draft, _assessment(draft), project_id=PROJECT)

    assert record.facts == (
        "the catalyst retained 91 percent of its activity after 500 hours",
        "retention was 89 percent in the second run",
    )
    assert record.inferences == ("activity loss slows after the first 200 hours",)
    assert record.hypotheses == ("the loss is caused by surface reconstruction",)


def test_the_record_cites_every_evidence_row_it_was_built_from() -> None:
    """Provenance runs from the record back to the rows, and the rows to sources."""
    draft = _complete()

    record = build(draft, _assessment(draft), project_id=PROJECT)

    assert record.evidence_refs == ("ev-1", "ev-2")
    assert record.question == QUESTION
    assert record.task_id == "task-1"
    assert record.node_id == "node-1"
    assert record.project_id == PROJECT
    assert record.report == draft.report


def test_a_conflict_is_referenced_by_id_on_the_record() -> None:
    draft = _complete()
    draft.conflicts = [_conflict()]

    record = build(draft, _assessment(draft), project_id=PROJECT)

    assert record.conflicts == (draft.conflicts[0].conflict_id,)


def test_the_contracts_findings_become_the_records_unknowns() -> None:
    """An INCOMPLETE record has to hand the Master the list of what is missing.

    A status of INCOMPLETE with no gaps recorded is a refusal to explain, and
    the Master's next move becomes a guess.
    """
    draft = _complete()
    draft.unknowns = ()
    draft.recommended_followups = ()

    record = build(draft, _assessment(draft), project_id=PROJECT)

    assert record.completion_status is CompletionStatus.INCOMPLETE
    assert any("followups" in unknown for unknown in record.unknowns)
    assert any("unknowns" in unknown for unknown in record.unknowns)


def test_what_the_task_declared_comes_before_what_the_contract_found() -> None:
    """The researcher's own account is kept, and the review is appended to it.

    A reader meets what the task knew first and what was found wrong with it
    second, which is the order the two were established in.
    """
    draft = _complete()
    draft.recommended_followups = ()

    record = build(draft, _assessment(draft), project_id=PROJECT)

    assert record.unknowns[0] == "nothing is known about the 5000-hour regime"
    assert len(record.unknowns) == 2


def test_a_complete_record_keeps_the_assessment_as_it_was_measured() -> None:
    draft = _complete()

    assessment = _assessment(draft)
    record = build(draft, assessment, project_id=PROJECT)

    assert record.sufficiency == assessment.as_assessment()


def test_an_incomplete_record_folds_the_findings_into_the_stored_assessment() -> None:
    """Strong evidence for the wrong question is a real outcome.

    The assessment said STRONG; the record is INCOMPLETE. Writing only the
    assessment would lose the second, and writing only the findings would lose
    the axes.
    """
    draft = _complete()
    draft.recommended_followups = ()

    assessment = _assessment(draft)
    record = build(draft, assessment, project_id=PROJECT)

    assert assessment.sufficiency is Sufficiency.STRONG
    assert record.sufficiency.sufficiency is Sufficiency.STRONG
    assert record.sufficiency.gaps
    assert set(assessment.gaps) < set(record.sufficiency.gaps)


def test_building_an_empty_draft_produces_a_record_rather_than_an_error() -> None:
    """The check runs before the domain validator, and that ordering is the point.

    `ResearchRecord` refuses a COMPLETE record with no evidence. `build` never
    produces one — it judges first — so an empty task yields an INCOMPLETE
    record with its reasons rather than a validation error the caller has to
    interpret.
    """
    draft = Draft(question="what is known about X?")

    record = build(draft, _assessment(draft), project_id=PROJECT)

    assert isinstance(record, ResearchRecord)
    assert record.completion_status is CompletionStatus.INCOMPLETE
    assert record.evidence_refs == ()
    assert record.facts == record.inferences == record.hypotheses == ()


def test_a_complete_record_is_one_the_domain_accepts() -> None:
    """The two layers agree: what `build` calls complete, the domain accepts."""
    draft = _complete()

    record = build(draft, _assessment(draft), project_id=PROJECT)

    assert record.completion_status is CompletionStatus.COMPLETE
    assert record.evidence_refs, "a COMPLETE record with no evidence is refused by the domain"


# ── Naming the axes behind the category ─────────────────────────────────────


def test_unmeasured_names_the_axes_holding_the_category_down() -> None:
    """The question a Master asks next: not "how good is this" but "what would help"."""
    draft = _complete()
    draft.claims.append(
        _claim(
            evidence_id="ev-3",
            statement="the coating reduces wear by half",
            sources=("src-1",),
            tier=EvidenceSourceTier.D,
        )
    )

    axes = unmeasured(_assessment(draft))

    assert axes == (Consideration.AUTHORITY,)


def test_unmeasured_is_empty_when_nothing_is_holding_the_category_down() -> None:
    draft = _complete()

    assert unmeasured(_assessment(draft)) == ()


def test_a_holding_axis_is_named_whatever_kind_of_gap_it_is() -> None:
    """`UNMET` and `UNKNOWN` are both gaps, and both are reported as one."""
    assessment = assess(claims=[_claim(conditions={})], sources=[_source()])

    assert unmeasured(assessment) == (Consideration.CONDITION_MATCH,)
    assert assessment.measurement(Consideration.CONDITION_MATCH).finding is Finding.UNKNOWN
