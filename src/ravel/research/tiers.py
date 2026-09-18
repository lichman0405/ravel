"""How strong a source is, and why.

`docs/05_RESEARCH_AND_EVIDENCE.md` defines four tiers. This module turns that
definition into rules and, more importantly, records *which rule fired*. A tier
with no reason is a number someone has to take on faith; a tier that names the
rule can be argued with, and the argument is what a later reader needs when a
decision rests on it.

The rules are deliberately conservative and domain-based, because domain is the
only signal available before the source has been read. Two consequences are
worth stating plainly:

- **A tier is not a judgement about a specific paper.** A `doi.org` link can
  resolve to a retraction notice; `arxiv.org` hosts work that later wins a
  Nobel. The tier says what kind of venue it is, which is what can be known
  without reading it, and the reading is what the Evidence Ledger records
  separately.
- **An unclassified domain is Tier D, not Tier A.** The default is the weakest
  defensible claim. A rule that promoted the unknown would make it profitable
  to be unknown.

A connector that knows more — Crossref knows a work's type, a journal's ISSN,
whether it is a preprint — can override the domain rule and say so. That is
why `assign` takes hints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urlsplit

from ravel.domain.enums import EvidenceSourceTier


@dataclass(frozen=True, slots=True)
class TierAssignment:
    """A tier together with the reason it was chosen."""

    tier: EvidenceSourceTier
    rule: str
    detail: str = ""

    def as_dict(self) -> dict[str, str]:
        return {"tier": self.tier.value, "rule": self.rule, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class TierRule:
    """One domain-based rule. First match wins, so order encodes priority."""

    name: str
    tier: EvidenceSourceTier
    suffixes: tuple[str, ...]
    detail: str = ""


def _rule(
    name: str, tier: EvidenceSourceTier, detail: str, *suffixes: str
) -> TierRule:
    return TierRule(name=name, tier=tier, suffixes=suffixes, detail=detail)


#: Ordered strongest first. A domain appears once: `pubs.acs.org` is a
#: publisher, and the publisher rule is stated before the generic ones so it is
#: the one that fires.
RULES: tuple[TierRule, ...] = (
    _rule(
        "registry",
        EvidenceSourceTier.A,
        "a persistent identifier registry; the record is the registration itself",
        "doi.org",
        "dx.doi.org",
        "ncbi.nlm.nih.gov",
        "pubchem.ncbi.nlm.nih.gov",
        "ebi.ac.uk",
        "rcsb.org",
        "nist.gov",
        "nlm.nih.gov",
    ),
    _rule(
        "standards-body",
        EvidenceSourceTier.A,
        "an official standards body publishing the standard itself",
        "iso.org",
        "iec.ch",
        "astm.org",
        "ieee.org",
        "ietf.org",
        "w3.org",
        "cen.eu",
        "bsigroup.com",
    ),
    _rule(
        "publisher",
        EvidenceSourceTier.A,
        "a scholarly publisher's own site",
        "nature.com",
        "science.org",
        "sciencedirect.com",
        "springer.com",
        "link.springer.com",
        "wiley.com",
        "onlinelibrary.wiley.com",
        "acs.org",
        "pubs.acs.org",
        "rsc.org",
        "pubs.rsc.org",
        "iopscience.iop.org",
        "aps.org",
        "journals.aps.org",
        "aip.org",
        "pubs.aip.org",
        "cell.com",
        "thelancet.com",
        "bmj.com",
        "jamanetwork.com",
        "plos.org",
        "journals.plos.org",
        "mdpi.com",
        "frontiersin.org",
        "tandfonline.com",
        "sagepub.com",
        "cambridge.org",
        "academic.oup.com",
        "royalsocietypublishing.org",
    ),
    _rule(
        "preprint",
        EvidenceSourceTier.B,
        "a preprint server: not yet peer reviewed",
        "arxiv.org",
        "biorxiv.org",
        "medrxiv.org",
        "chemrxiv.org",
        "ssrn.com",
        "preprints.org",
        "osf.io",
    ),
    _rule(
        "patent-office",
        EvidenceSourceTier.B,
        "a patent office's own register",
        "patents.google.com",
        "uspto.gov",
        "epo.org",
        "wipo.int",
        "lens.org",
    ),
    _rule(
        "government",
        EvidenceSourceTier.B,
        "a government body publishing its own material",
        # `gov` covers every United States federal host, including the ones a
        # reader might expect to see listed — `nasa.gov`, `energy.gov`,
        # `osti.gov`. Listing them again here would be entries that never fire,
        # because the registry rule and this suffix are both checked first.
        "gov",
        "europa.eu",
        "un.org",
        "who.int",
    ),
    _rule(
        "academic-institution",
        EvidenceSourceTier.B,
        "a university or research institute",
        "edu",
        "ac.uk",
        "ac.jp",
        "ac.cn",
        "edu.cn",
        "mpg.de",
        "cnrs.fr",
        "zenodo.org",
        "figshare.com",
    ),
    _rule(
        "vendor",
        EvidenceSourceTier.C,
        "a vendor or industry body describing its own product",
        "sigmaaldrich.com",
        "thermofisher.com",
        "merckmillipore.com",
        "matweb.com",
        "asminternational.org",
        "tms.org",
        "aiche.org",
    ),
)

#: The tier of a domain no rule claims. Deliberately the weakest.
UNCLASSIFIED = TierAssignment(
    tier=EvidenceSourceTier.D,
    rule="unclassified",
    detail="no rule recognises this domain; it is recorded as a general web source",
)

#: Hints a connector can supply when it knows something the domain does not.
#: Keyed by the work's *declared type* — what the depositor told Crossref it
#: was — because that is the one classification richer than the domain, and it
#: comes from the record rather than from RAVEL's opinion.
_TYPE_HINTS: dict[str, TierAssignment] = {
    "journal-article": TierAssignment(
        tier=EvidenceSourceTier.A, rule="crossref:journal-article", detail="peer-reviewed article"
    ),
    "proceedings-article": TierAssignment(
        tier=EvidenceSourceTier.A,
        rule="crossref:proceedings-article",
        detail="peer-reviewed conference paper",
    ),
    "book-chapter": TierAssignment(
        tier=EvidenceSourceTier.A, rule="crossref:book-chapter", detail="edited volume chapter"
    ),
    "posted-content": TierAssignment(
        tier=EvidenceSourceTier.B, rule="crossref:posted-content", detail="a preprint deposit"
    ),
    "report": TierAssignment(
        tier=EvidenceSourceTier.B, rule="crossref:report", detail="a technical report"
    ),
    "dataset": TierAssignment(
        tier=EvidenceSourceTier.B, rule="crossref:dataset", detail="a deposited dataset"
    ),
    "standard": TierAssignment(
        tier=EvidenceSourceTier.A, rule="crossref:standard", detail="a published standard"
    ),
    "peer-review": TierAssignment(
        tier=EvidenceSourceTier.A, rule="crossref:peer-review", detail="a published review report"
    ),
    # Supporting information — the `.s001` deposits ACS, Wiley and others
    # register beside an article. It accompanies peer-reviewed work and is not
    # itself peer reviewed, so it sits below the article it belongs to rather
    # than inheriting its tier. Without this rule the domain rule would rate it
    # Tier A purely for resolving through doi.org, which is exactly the
    # overstatement these hints exist to prevent.
    "component": TierAssignment(
        tier=EvidenceSourceTier.B,
        rule="crossref:component",
        detail="supporting information deposited beside an article; not separately peer reviewed",
    ),
    "dissertation": TierAssignment(
        tier=EvidenceSourceTier.B, rule="crossref:dissertation", detail="a doctoral thesis"
    ),
    "book-section": TierAssignment(
        tier=EvidenceSourceTier.B, rule="crossref:book-section", detail="a section of a book"
    ),
    "book-part": TierAssignment(
        tier=EvidenceSourceTier.B, rule="crossref:book-part", detail="a part of a book"
    ),
    # Container records: a venue, a volume, an issue. These are DOI deposits for
    # the container rather than for a work, so a lead pointing at one points at
    # a table of contents. Recorded as usable-but-low rather than promoted,
    # because a citation to a journal is not a citation to a finding.
    "journal": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:container", detail="a journal, not a work"
    ),
    "journal-issue": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:container", detail="an issue, not a work"
    ),
    "journal-volume": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:container", detail="a volume, not a work"
    ),
    "proceedings": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:container", detail="a proceedings, not a work"
    ),
    "book": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:container", detail="a book record, not a chapter"
    ),
    "monograph": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:container", detail="a monograph, not a chapter"
    ),
    "reference-entry": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:reference-entry", detail="an encyclopedia entry"
    ),
    "database": TierAssignment(
        tier=EvidenceSourceTier.C,
        rule="crossref:database",
        detail="a registered database; the registration is not an assessment of its contents",
    ),
    # A funding record establishes that work was funded. It is not a finding,
    # and a research task that cites one as evidence of a result has confused
    # the grant with the work.
    "grant": TierAssignment(
        tier=EvidenceSourceTier.C, rule="crossref:grant", detail="a funding record, not a finding"
    ),
    "other": TierAssignment(
        tier=EvidenceSourceTier.D, rule="crossref:other", detail="the depositor declared no type"
    ),
}

#: What a declared type RAVEL has no rule for is rated. Tier C rather than the
#: domain rule's answer, because the type is the better evidence about the work
#: and RAVEL does not know what it means: a record a registry classified as
#: something unrecognised has not earned a claim to be peer-reviewed
#: literature, whatever domain it resolves through.
UNRECOGNIZED_TYPE = TierAssignment(
    tier=EvidenceSourceTier.C,
    rule="unrecognized-declared-type",
    detail="the record declares a type RAVEL has no rule for",
)


def host_of(url: str) -> str:
    """The registrable-ish host of a URL, lowercased, without `www.`.

    Not a public-suffix parse: RAVEL needs the host as written so that a rule
    can match `pubs.acs.org` and `acs.org` differently, and a full suffix list
    would be a large dependency for a classification that is already a
    heuristic and is recorded as one.
    """
    host = urlsplit(url).netloc.lower().split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host


def _matches(host: str, suffix: str) -> bool:
    """Whether a host is, or is under, a rule's suffix.

    One expression covers both shapes. `"gov"` matches `nist.gov` because the
    host ends with `.gov`, and does not match `gov.example.com` because that
    host ends with `.com` — the anchoring is what distinguishes a top-level
    suffix from a domain, so no separate case is needed.
    """
    return host == suffix or host.endswith(f".{suffix}")


def assign(url: str, *, declared_type: str | None = None) -> TierAssignment:
    """Classify a URL, preferring what the record says to what the host says.

    A declared type wins because it is evidence about the *work* — Crossref's
    depositor said `journal-article` — while the domain is evidence about the
    *site*. When a preprint server hosts a published article's record, the
    record is the better answer, and the assignment names which source of truth
    it used so the choice can be reviewed.

    A declared type that no rule recognizes also wins, and returns
    `UNRECOGNIZED_TYPE`. Falling through to the domain there would let a record
    the registry declined to call an article be rated by the reputation of the
    site it resolves through, which is the wrong way round.
    """
    if declared_type:
        key = declared_type.strip().lower()
        hint = _TYPE_HINTS.get(key)
        if hint is not None:
            return hint
        return TierAssignment(
            tier=UNRECOGNIZED_TYPE.tier,
            rule=UNRECOGNIZED_TYPE.rule,
            detail=f"{UNRECOGNIZED_TYPE.detail} ({key})",
        )

    host = host_of(url)
    for rule in RULES:
        if any(_matches(host, suffix) for suffix in rule.suffixes):
            return TierAssignment(tier=rule.tier, rule=rule.name, detail=rule.detail)

    return TierAssignment(
        tier=UNCLASSIFIED.tier,
        rule=UNCLASSIFIED.rule,
        detail=f"{UNCLASSIFIED.detail} ({host})" if host else UNCLASSIFIED.detail,
    )


@dataclass(frozen=True, slots=True)
class TierCounts:
    """How a body of evidence is distributed across tiers."""

    counts: dict[EvidenceSourceTier, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def strongest(self) -> EvidenceSourceTier | None:
        for tier in (EvidenceSourceTier.A, EvidenceSourceTier.B, EvidenceSourceTier.C):
            if self.counts.get(tier):
                return tier
        return EvidenceSourceTier.D if self.counts.get(EvidenceSourceTier.D) else None
