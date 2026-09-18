"""The research completion contract, as a function rather than a hope.

`docs/05_RESEARCH_AND_EVIDENCE.md` §11 lists what must be true before a RESEARCH
node can be COMPLETE: core questions covered, every key factual claim carrying
provenance, sufficiency assessed, conflicts recorded, the three claim classes
separated, unknowns declared, acceptance criteria carrying provenance,
followups provided, and the record itself valid. This module takes what a
research task produced and decides which of those hold.

Two properties make it worth having as code:

- **INCOMPLETE is a correct answer.** A task that comes back with a clear list of
  what it could not establish has done its job; a task that comes back COMPLETE
  with a story has not. So the checks are not a formality to pass — the gaps they
  find are the record's `unknowns`, and they are the reason the Master can
  decide to keep researching instead of proceeding.
- **A claim's class is not the researcher's to choose freely.** `Evidence` and
  `ResearchRecord` already refuse a FACT with no source and a record that is
  COMPLETE with nothing to show. What is added here is the check that the same
  statement is not filed under two classes at once, and that an inference says
  what it was inferred from.

The record is assembled from the ledger rather than from prose: facts,
inferences and hypotheses are the `statement` of the `Evidence` rows in each
class. That way the human-readable report is a projection of the structured
record, which is what §12 asks for, rather than a second account that can drift
from it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ravel.domain.enums import ClaimClass, CompletionStatus, Sufficiency
from ravel.domain.evidence import (
    Evidence,
    EvidenceConflict,
    EvidenceSource,
    ResearchRecord,
    SufficiencyAssessment,
)
from ravel.research.sufficiency import Assessment, Consideration, Finding

#: What a research task must be able to say it did. Named so that a gap in the
#: record can be reported against the requirement it failed, rather than as a
#: sentence somebody has to interpret.
REQUIREMENTS = (
    "core questions covered",
    "key factual claims have provenance",
    "evidence sufficiency assessed",
    "conflicts recorded",
    "fact/inference/hypothesis separated",
    "unknowns explicitly declared",
    "suggested acceptance criteria have provenance",
    "recommended followups provided",
    "structured research record validated",
)


@dataclass
class Draft:
    """What a research task produced, before it is judged against the contract."""

    question: str
    claims: list[Evidence] = field(default_factory=list)
    sources: list[EvidenceSource] = field(default_factory=list)
    conflicts: list[EvidenceConflict] = field(default_factory=list)
    suggested_acceptance_criteria: tuple[str, ...] = ()
    recommended_followups: tuple[str, ...] = ()
    unknowns: tuple[str, ...] = ()
    report: str = ""
    task_id: str = ""
    node_id: str | None = None

    @property
    def evidence_refs(self) -> tuple[str, ...]:
        return tuple(claim.evidence_id for claim in self.claims)


@dataclass(frozen=True, slots=True)
class Completion:
    """Whether the contract holds, and precisely where it does not."""

    status: CompletionStatus
    unmet: tuple[str, ...]
    detail: tuple[str, ...]

    @property
    def is_complete(self) -> bool:
        return self.status is CompletionStatus.COMPLETE


def judge(draft: Draft, assessment: Assessment) -> Completion:
    """Check a draft against the nine requirements.

    Every unmet requirement is reported with what was actually found, because
    "incomplete" on its own tells a Master nothing about whether to keep
    researching, narrow the question, or proceed and record the risk.
    """
    unmet: list[str] = []
    detail: list[str] = []

    facts = [claim for claim in draft.claims if claim.claim_class is ClaimClass.FACT]
    inferences = [claim for claim in draft.claims if claim.claim_class is ClaimClass.INFERENCE]

    if not draft.question.strip():
        unmet.append(REQUIREMENTS[0])
        detail.append("the task states no question, so nothing can be said to be covered")

    # A FACT cannot exist without a source — the domain type refuses one — so
    # what is checked here is the other direction: that a claim resting on a
    # source RAVEL read was not filed as something weaker than it is.
    unbacked = [claim for claim in facts if not _rests_on_a_read_source(claim, draft.sources)]
    if facts and unbacked:
        unmet.append(REQUIREMENTS[1])
        detail.append(
            f"{len(unbacked)} claims are filed as facts but do not rest on a source RAVEL read"
        )

    if assessment.sufficiency is Sufficiency.INSUFFICIENT:
        unmet.append(REQUIREMENTS[2])
        detail.append(
            "the evidence gathered cannot carry a decision: " + "; ".join(assessment.gaps)
        )

    # A conflict is only "recorded" if it is in the draft. Evidence rows may
    # carry conflicts_with without the disagreement having been written down as
    # its own record, and a disagreement nobody wrote down cannot be reviewed.
    referenced = {ref for claim in draft.claims for ref in claim.conflicts_with}
    described = {ref for conflict in draft.conflicts for ref in conflict.evidence_refs}
    if referenced - described:
        unmet.append(REQUIREMENTS[3])
        detail.append(
            f"{len(referenced - described)} evidence rows name a conflict that has no "
            "conflict record, so what they disagree about is not written down"
        )

    duplicated = _statements_in_two_classes(draft.claims)
    if duplicated:
        unmet.append(REQUIREMENTS[4])
        detail.append(
            "the same statement is filed under more than one class: "
            + "; ".join(sorted(duplicated)[:3])
        )

    with_conditions = [claim for claim in inferences if not claim.conditions]
    if inferences and len(with_conditions) == len(inferences):
        unmet.append(REQUIREMENTS[4])
        detail.append(
            "no inference records the conditions it holds under, so a reader cannot tell "
            "where the reasoning stops applying"
        )

    if not draft.unknowns and not assessment.gaps:
        unmet.append(REQUIREMENTS[5])
        detail.append(
            "the task declared no unknowns and the assessment found no gaps; a task that "
            "found everything settled has almost certainly not looked"
        )

    # §11 asks for a criterion's "provenance/status". `ResearchRecord` carries
    # suggested criteria as bare strings, so the status is the one the field
    # name gives them — and what is checkable here is that they came from
    # somewhere. A criterion with nothing observed behind it came from the
    # researcher's expectation, which is a hypothesis about what to measure and
    # must not reach a contract looking like a benchmark.
    observed = [claim for claim in draft.claims if claim.claim_class is not ClaimClass.HYPOTHESIS]
    if draft.suggested_acceptance_criteria and not observed:
        unmet.append(REQUIREMENTS[6])
        detail.append(
            f"{len(draft.suggested_acceptance_criteria)} acceptance criteria are suggested "
            "with nothing observed behind them, so they are expectations rather than "
            "measurements a result could be held to"
        )

    if not draft.recommended_followups:
        unmet.append(REQUIREMENTS[7])
        detail.append("no followups are recommended, including when the task is complete")

    if not draft.claims:
        unmet.append(REQUIREMENTS[8])
        detail.append("the record holds no evidence at all")

    return Completion(
        status=CompletionStatus.INCOMPLETE if unmet else CompletionStatus.COMPLETE,
        unmet=tuple(unmet),
        detail=tuple(detail),
    )


def build(draft: Draft, assessment: Assessment, *, project_id: str) -> ResearchRecord:
    """Assemble the structured record, and say honestly whether it is complete.

    The gaps the contract found are appended to the record's `unknowns` rather
    than being kept only in the completion status. A task that returns
    INCOMPLETE has to hand the Master the list of what is missing, or the
    Master's next move is a guess.
    """
    completion = judge(draft, assessment)
    unknowns = tuple(dict.fromkeys((*draft.unknowns, *completion.detail)))

    return ResearchRecord(
        project_id=project_id,
        task_id=draft.task_id,
        node_id=draft.node_id,
        question=draft.question,
        facts=_statements(draft.claims, ClaimClass.FACT),
        inferences=_statements(draft.claims, ClaimClass.INFERENCE),
        hypotheses=_statements(draft.claims, ClaimClass.HYPOTHESIS),
        evidence_refs=draft.evidence_refs,
        conflicts=tuple(conflict.conflict_id for conflict in draft.conflicts),
        sufficiency=_sufficiency(assessment, completion),
        suggested_acceptance_criteria=draft.suggested_acceptance_criteria,
        recommended_followups=draft.recommended_followups,
        unknowns=unknowns,
        completion_status=completion.status,
        report=draft.report,
    )


def _sufficiency(assessment: Assessment, completion: Completion) -> SufficiencyAssessment:
    """The storable assessment, with the contract's findings folded in.

    An assessment can say STRONG while the record is INCOMPLETE — strong
    evidence for the wrong question is a real outcome — so the gaps from both
    are merged rather than one overwriting the other.
    """
    stored = assessment.as_assessment()
    if completion.is_complete:
        return stored
    return stored.model_copy(
        update={"gaps": tuple(dict.fromkeys((*stored.gaps, *completion.detail)))}
    )


def _statements(claims: Sequence[Evidence], claim_class: ClaimClass) -> tuple[str, ...]:
    return tuple(claim.statement for claim in claims if claim.claim_class is claim_class)


def _rests_on_a_read_source(claim: Evidence, sources: Sequence[EvidenceSource]) -> bool:
    by_id = {source.source_id: source for source in sources}
    return any(
        (source := by_id.get(ref)) is not None and source.was_read
        for ref in claim.source_refs
    )


def _statements_in_two_classes(claims: Sequence[Evidence]) -> set[str]:
    """Statements filed under more than one claim class.

    The same words as a fact and as an inference would let a reader take the
    weaker one as the record's position and the stronger as its evidence. The
    classes are the separation §8 asks for, so a statement belongs to one.
    """
    seen: dict[str, set[ClaimClass]] = {}
    for claim in claims:
        seen.setdefault(claim.statement.strip().lower(), set()).add(claim.claim_class)
    return {statement for statement, classes in seen.items() if len(classes) > 1}


def unmeasured(assessment: Assessment) -> tuple[Consideration, ...]:
    """The axes that are holding the category down.

    Exposed because it is the question a Master asks next: not "how good is
    this" but "what would make it better".
    """
    return tuple(
        measurement.consideration
        for measurement in assessment.measurements
        if measurement.finding in (Finding.UNMET, Finding.UNKNOWN)
    )
