"""Whether what was gathered can carry the decision it was gathered for.

`docs/05_RESEARCH_AND_EVIDENCE.md` §7 is explicit that this is "an assessment,
not a mechanical paper count", and then lists six things to consider. This
module is the mechanical part of that: it measures the six, states what it
measured for each, and only then gives a category. The judgement stays with the
Master and the Review agent, who can read the measurements and disagree with the
conclusion — which is the point of recording them. A category with no working
underneath it is a number someone has to take on faith.

Three decisions shape everything here:

- **The weakest consideration sets the category.** Not an average. Weak
  reproducibility does not become acceptable because authority is high, and a
  body of evidence is not better than its worst-supported claim, which is why
  the axes are combined by taking the floor rather than by scoring.
- **Nothing readable is INSUFFICIENT, whatever else is true.** A paywall that
  answered is not a source RAVEL read, and ten of them are not five sources.
  This is the one category that follows from a single fact, because it is the
  one that no amount of other evidence can compensate for.
- **Every axis is total.** These run on whatever a research task gathered,
  including nothing at all, and an assessment that raised on an empty ledger
  would fail exactly when it is most needed.

The output is `SufficiencyAssessment` — a domain record that is stored on the
`ResearchRecord` — so it is written down and can be reviewed later. The
per-axis findings are folded into its `rationale` and `gaps`.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urlsplit

from ravel.domain.enums import ClaimClass, EvidenceSourceTier, Sufficiency
from ravel.domain.evidence import (
    Evidence,
    EvidenceConflict,
    EvidenceSource,
    SufficiencyAssessment,
)


class Consideration(StrEnum):
    """The six axes, named as `docs/05` names them."""

    INDEPENDENCE = "independence"
    AUTHORITY = "authority"
    DIRECTNESS = "directness"
    CONDITION_MATCH = "condition match"
    REPRODUCIBILITY = "reproducibility"
    CONFLICT = "conflict"


class Finding(StrEnum):
    """What one axis measured.

    `UNKNOWN` is separate from `UNMET` on purpose. "Nobody recorded the
    conditions this claim holds under" and "the conditions recorded do not match
    the decision's" are different problems with different fixes, and collapsing
    them would send a researcher to do the wrong work.
    """

    MET = "met"
    PARTIAL = "partial"
    UNMET = "unmet"
    UNKNOWN = "unknown"
    #: Nothing to assess on this axis because there is nothing to assess at all.
    NOT_APPLICABLE = "not applicable"


@dataclass(frozen=True, slots=True)
class Measurement:
    """One axis, what it found, and the working that produced it."""

    consideration: Consideration
    finding: Finding
    detail: str

    def as_line(self) -> str:
        return f"{self.consideration.value}: {self.finding.value} ({self.detail})"


@dataclass(frozen=True, slots=True)
class Assessment:
    """A sufficiency category together with the measurements behind it."""

    sufficiency: Sufficiency
    measurements: tuple[Measurement, ...]

    def measurement(self, consideration: Consideration) -> Measurement:
        for measurement in self.measurements:
            if measurement.consideration is consideration:
                return measurement
        raise KeyError(consideration)

    @property
    def gaps(self) -> tuple[str, ...]:
        """The axes that held the category down, in `docs/05`'s own words."""
        return tuple(
            f"{m.consideration.value}: {m.detail}"
            for m in self.measurements
            if m.finding in (Finding.UNMET, Finding.UNKNOWN)
        )

    @property
    def would_change_with(self) -> tuple[str, ...]:
        """What would move each unmet axis, stated as an action.

        Deliberately a list of things to do rather than of things that are
        missing: a gap a researcher cannot act on is a complaint.
        """
        return tuple(_REMEDIES[m.consideration] for m in self.measurements if _held_down(m))

    def as_assessment(self, *, extra_rationale: str = "") -> SufficiencyAssessment:
        """The storable domain record for this assessment."""
        rationale = "; ".join(m.as_line() for m in self.measurements)
        if extra_rationale:
            rationale = f"{rationale}; {extra_rationale}"
        return SufficiencyAssessment(
            sufficiency=self.sufficiency,
            rationale=rationale,
            gaps=self.gaps,
            would_change_with=self.would_change_with,
        )


#: What would improve each axis. Written once so that the advice cannot drift
#: from the measurement that produced it.
_REMEDIES: dict[Consideration, str] = {
    Consideration.INDEPENDENCE: (
        "a second source from a different publisher reporting the same result"
    ),
    Consideration.AUTHORITY: (
        "a peer-reviewed or standards-body source for the claim it is load-bearing for"
    ),
    Consideration.DIRECTNESS: "the primary source itself, rather than a record describing it",
    Consideration.CONDITION_MATCH: "the conditions each claim holds under, recorded alongside it",
    Consideration.REPRODUCIBILITY: "a stored snapshot of every source the claim rests on",
    Consideration.CONFLICT: "a decision resolving the recorded conflict, or evidence settling it",
}


def _held_down(measurement: Measurement) -> bool:
    return measurement.finding in (Finding.UNMET, Finding.UNKNOWN, Finding.PARTIAL)


def assess(
    *,
    claims: Sequence[Evidence] = (),
    sources: Sequence[EvidenceSource] = (),
    conflicts: Sequence[EvidenceConflict] = (),
) -> Assessment:
    """Measure a body of evidence against the six considerations.

    Args:
        claims: The evidence rows gathered, whatever their claim class.
        sources: Every source they rest on, read or not. Passing the restricted
            ones matters: a paywall that answered is part of the picture.
        conflicts: Recorded disagreements, including resolved ones. Only
            unresolved ones hold the category down.

    Returns:
        The measurements and the category they support.
    """
    readable = [source for source in sources if source.was_read]
    measurements = (
        _independence(readable),
        _authority(claims),
        _directness(claims, sources),
        _condition_match(claims),
        _reproducibility(readable),
        _conflict(conflicts),
    )
    return Assessment(
        sufficiency=_category(measurements, readable=readable),
        measurements=measurements,
    )


def _category(
    measurements: tuple[Measurement, ...], *, readable: Sequence[EvidenceSource]
) -> Sufficiency:
    """The category the measurements support.

    `INSUFFICIENT` first and on its own terms: nothing readable means there is
    no evidence, and no combination of the other axes changes that. After that
    the weakest finding decides, with `UNKNOWN` treated as a gap rather than as
    a pass — an axis nobody measured is not an axis that came out well.
    """
    if not readable:
        return Sufficiency.INSUFFICIENT

    findings = {m.finding for m in measurements}
    if Finding.UNMET in findings:
        return Sufficiency.WEAK
    if Finding.UNKNOWN in findings:
        return Sufficiency.WEAK
    if Finding.PARTIAL in findings:
        return Sufficiency.MODERATE
    return Sufficiency.STRONG


def _independence(readable: Sequence[EvidenceSource]) -> Measurement:
    """Whether the sources are independent of one another.

    Measured by publisher host, which is a proxy and is recorded as one: two
    papers from one group on one publisher's site count as one source here even
    though they are two documents. The proxy errs in the safe direction — it
    understates independence rather than overstating it — and the hosts are
    named in the detail so that a reader can see what was counted.
    """
    if not readable:
        return Measurement(
            Consideration.INDEPENDENCE,
            Finding.NOT_APPLICABLE,
            "no source was read, so there is nothing to be independent of",
        )
    hosts = sorted({_host(source.url) for source in readable})
    if len(hosts) == 1:
        return Measurement(
            Consideration.INDEPENDENCE,
            Finding.PARTIAL,
            f"every source comes from {hosts[0]}; one publisher is one account of a result",
        )
    return Measurement(
        Consideration.INDEPENDENCE,
        Finding.MET,
        f"{len(hosts)} publishers: {', '.join(hosts)}",
    )


def _authority(claims: Sequence[Evidence]) -> Measurement:
    """The strongest tier any claim rests on, and the weakest.

    The weakest is reported because it is the one that decides whether a
    *decision* is safe: a conclusion supported by three Tier A papers and one
    blog post is a conclusion that has not been established for the part that
    rests on the blog post.
    """
    load_bearing = [claim for claim in claims if claim.claim_class is not ClaimClass.HYPOTHESIS]
    if not load_bearing:
        return Measurement(
            Consideration.AUTHORITY,
            Finding.UNKNOWN,
            "no fact or inference was recorded, so no claim rests on a source",
        )
    tiers = sorted({claim.source_tier for claim in load_bearing}, key=lambda tier: tier.value)
    weakest = tiers[-1]
    if weakest in (EvidenceSourceTier.A, EvidenceSourceTier.B):
        strongest = tiers[0]
        scope = (
            f"tier {strongest.value} to {weakest.value}"
            if len(tiers) > 1
            else f"tier {strongest.value}"
        )
        return Measurement(
            Consideration.AUTHORITY,
            Finding.MET,
            f"every claim rests on {scope} sources",
        )
    return Measurement(
        Consideration.AUTHORITY,
        Finding.UNMET,
        f"a claim rests on a tier {weakest.value} source, which is not a peer-reviewed or "
        "standards-body account",
    )


def _directness(claims: Sequence[Evidence], sources: Sequence[EvidenceSource]) -> Measurement:
    """Whether claims rest on sources that were actually read.

    This is the axis the whole project is arranged around. A claim resting on a
    source RAVEL could not open is a claim resting on a title, and the ledger
    records it as such — but it must not count towards sufficiency as though the
    source had been read.

    A claim resting on a *container* record — a table of contents, a funding
    record — is a separate weakness, and it is already measured: such records
    are tier C, and the authority axis holds those down.
    """
    if not claims:
        return Measurement(
            Consideration.DIRECTNESS,
            Finding.UNKNOWN,
            "no claim was recorded, so there is nothing whose directness could be measured",
        )
    by_id = {source.source_id: source for source in sources}
    unsupported = [
        claim
        for claim in claims
        if not any(
            (source := by_id.get(ref)) is not None and source.was_read
            for ref in claim.source_refs
        )
    ]
    if unsupported:
        return Measurement(
            Consideration.DIRECTNESS,
            Finding.UNMET,
            f"{len(unsupported)} of {len(claims)} claims rest on a source that was not read "
            "or is no longer in the ledger",
        )
    return Measurement(
        Consideration.DIRECTNESS,
        Finding.MET,
        f"every one of {len(claims)} claims rests on a source RAVEL read",
    )


def _condition_match(claims: Sequence[Evidence]) -> Measurement:
    """Whether the conditions a claim holds under were recorded.

    RAVEL cannot check that a claim's conditions match a decision's — it does
    not know the decision's conditions here — so this measures the thing it can:
    whether the conditions are present at all, which is what makes the check
    possible for whoever does know.
    """
    if not claims:
        return Measurement(
            Consideration.CONDITION_MATCH,
            Finding.NOT_APPLICABLE,
            "no claim was recorded",
        )
    with_conditions = [claim for claim in claims if claim.conditions]
    if not with_conditions:
        return Measurement(
            Consideration.CONDITION_MATCH,
            Finding.UNKNOWN,
            f"none of {len(claims)} claims records the conditions it holds under, so a "
            "decision cannot check that they match its own",
        )
    if len(with_conditions) < len(claims):
        return Measurement(
            Consideration.CONDITION_MATCH,
            Finding.PARTIAL,
            f"{len(with_conditions)} of {len(claims)} claims record their conditions",
        )
    return Measurement(
        Consideration.CONDITION_MATCH,
        Finding.MET,
        f"every one of {len(claims)} claims records the conditions it holds under",
    )


def _reproducibility(readable: Sequence[EvidenceSource]) -> Measurement:
    """Whether what was read can be checked again.

    A hash alone proves the bytes have not changed since; a stored snapshot
    proves what they were. Both are needed for a claim someone can re-examine
    without the original URL still answering, so a hash with no snapshot is
    partial rather than met.
    """
    if not readable:
        return Measurement(
            Consideration.REPRODUCIBILITY,
            Finding.NOT_APPLICABLE,
            "no source was read, so there is nothing to reproduce",
        )
    hashed = [source for source in readable if source.content_hash]
    stored = [source for source in readable if source.snapshot_ref or source.artifact_ref]
    if len(stored) == len(readable):
        return Measurement(
            Consideration.REPRODUCIBILITY,
            Finding.MET,
            f"all {len(readable)} read sources are hashed and stored",
        )
    if hashed:
        return Measurement(
            Consideration.REPRODUCIBILITY,
            Finding.PARTIAL,
            f"{len(stored)} of {len(readable)} read sources have a stored snapshot; the rest "
            "rest on the original URL still answering",
        )
    return Measurement(
        Consideration.REPRODUCIBILITY,
        Finding.UNMET,
        f"none of {len(readable)} read sources carries a content hash",
    )


def _conflict(conflicts: Sequence[EvidenceConflict]) -> Measurement:
    """Whether recorded disagreements have been resolved.

    Unresolved is not the same as absent. A body of evidence with a known
    disagreement in it is weaker than one without — and pretending otherwise,
    by dropping the conflict or averaging it away, is the failure `docs/05` §9
    exists to prevent.
    """
    if not conflicts:
        return Measurement(
            Consideration.CONFLICT,
            Finding.MET,
            "no conflict is recorded among the sources consulted",
        )
    unresolved = [conflict for conflict in conflicts if conflict.resolution is None]
    if unresolved:
        return Measurement(
            Consideration.CONFLICT,
            Finding.UNMET,
            f"{len(unresolved)} of {len(conflicts)} recorded conflicts are unresolved",
        )
    return Measurement(
        Consideration.CONFLICT,
        Finding.MET,
        f"all {len(conflicts)} recorded conflicts have a resolution",
    )


def _host(url: str) -> str:
    """The publisher host of a URL, without `www.`."""
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host
