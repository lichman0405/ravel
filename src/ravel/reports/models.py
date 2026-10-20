"""The report records, and the requests a report is assembled from.

A report is a snapshot rather than a query. `scientific_reports` holds the
typed evidence index and the deterministic counts that were true when the
report was written; `report_claims` and `report_evidence_refs` hold what the
report rests on. Later ledger rows do not reach back into any of them, which is
why a corrected report is a new report id rather than an UPDATE.

The request types are deliberately narrow. They let a Master or Review seat say
*what* to cite and *which page* supports it. They do not let it say what kind
of evidence the citation is, or whether the report is REAL: those are derived
from the ledger row's provenance by `ravel.reports.classification`, because a
caller-supplied label is exactly the sort of thing a report is supposed to be
able to prove is false.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    ClaimClass,
    EvidenceExecutionType,
    ReportDataMode,
    ReportStatus,
)
from ravel.domain.ids import new_id
from ravel.domain.roles import AgentRole


def report_uuid() -> str:
    """A report identifier in the canonical UUID spelling the schema requires."""
    return str(uuid4())


class ReportEvidenceRef(Record):
    """One ledger record a report rests on, typed from its own provenance."""

    report_id: str
    evidence_id: str = Field(min_length=1)
    position: int = Field(ge=1)
    execution_type: EvidenceExecutionType
    #: A page range or other locator. Required for a source claim, empty for a
    #: computation whose execution references are its locator instead.
    locator: str = ""
    #: The hash the ledger holds for this record, not one the caller supplied.
    #: An asserted hash that disagrees is recorded as a conflict.
    content_hash: str | None = None
    #: The ledger row's identifying provenance at the moment of the snapshot.
    original_record: dict[str, Any] = Field(default_factory=dict)


class ReportClaim(Record):
    """A statement a report makes, and the typed evidence it cites."""

    report_id: str
    claim_id: str = Field(default_factory=lambda: str(uuid4()))
    position: int = Field(ge=1)
    claim_type: ClaimClass
    statement: str = Field(min_length=1)
    evidence_ids: tuple[str, ...] = ()
    execution_refs: tuple[str, ...] = ()
    execution_node_id: str | None = None
    execution_count: int | None = Field(default=None, ge=0)


class ScientificReport(Record):
    """The immutable report row: title, mode, status, facts and conflicts."""

    report_id: str = Field(default_factory=report_uuid)
    project_id: str
    title: str = Field(min_length=1, max_length=500)
    generated_at: datetime = Field(default_factory=utcnow)
    data_mode: ReportDataMode
    status: ReportStatus
    limitations: tuple[str, ...] = ()
    #: Deterministic facts rendered before any commentary: execution counts,
    #: artifact provenance counts, review state, deviations and the evidence
    #: index. Stored, so a later ledger change cannot rewrite them.
    rendered: dict[str, Any] = Field(default_factory=dict)
    conflicts: tuple[dict[str, Any], ...] = ()

    @model_validator(mode="after")
    def _status_agrees_with_conflicts(self) -> ScientificReport:
        if self.status is ReportStatus.CERTIFIED and self.conflicts:
            raise ValueError(
                f"report {self.report_id} is CERTIFIED but records "
                f"{len(self.conflicts)} conflicts; a conflicted report is not certified"
            )
        if self.status is ReportStatus.REPORT_CONFLICT and not self.conflicts:
            raise ValueError(
                f"report {self.report_id} is REPORT_CONFLICT but names no conflict; "
                "the status must say what disagrees with the ledger"
            )
        return self


class ReportCommentary(Record):
    """Master's explanation of a report, kept apart from the report's facts.

    It is a separate append-only row rather than a field on the report for one
    reason: commentary must not be able to alter a rendered count, a claim, or
    a determinism result. Reading a report can include this, but the certified
    JSON never does.
    """

    commentary_id: str = Field(default_factory=new_id)
    report_id: str
    project_id: str
    author: str = Field(min_length=1)
    role: AgentRole = AgentRole.MASTER
    body: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _only_master_explains(self) -> ReportCommentary:
        if self.role is not AgentRole.MASTER:
            raise ValueError(
                f"commentary {self.commentary_id} is written by {self.role.value}; "
                "only Master explains a report, and Review's judgement is a Review Record"
            )
        return self


class ReportNotCertified(RuntimeError):
    """Raised when a conflicted report is asked for its certified rendering."""


@dataclass(frozen=True, slots=True)
class ReportSnapshot:
    """One report and everything it was assembled from, read back together."""

    report: ScientificReport
    claims: tuple[ReportClaim, ...]
    evidence_refs: tuple[ReportEvidenceRef, ...]
    commentary: ReportCommentary | None = None

    @property
    def certified(self) -> bool:
        """Whether this report may be exported as certified."""
        return self.report.status is ReportStatus.CERTIFIED and not self.report.conflicts

    @property
    def conflicts(self) -> tuple[dict[str, Any], ...]:
        return self.report.conflicts

    def report_json(self) -> dict[str, Any]:
        """The frozen `report.schema.json` shape for this snapshot.

        Shape-valid for a conflicted report too, so a reader can inspect what
        was accepted; `certified_json` is the method that refuses one. The
        arrays are ordered by the positions fixed at assembly, which is what
        makes two reports over the same ledger render identically.
        """
        return {
            "report_id": self.report.report_id,
            "project_id": self.report.project_id,
            "title": self.report.title,
            "generated_at": self.report.generated_at.isoformat(),
            "data_mode": self.report.data_mode.value,
            "evidence_refs": [
                {
                    "evidence_id": ref.evidence_id,
                    "execution_type": ref.execution_type.value,
                }
                for ref in self.evidence_refs
            ],
            "claims": [
                {
                    "claim_id": claim.claim_id,
                    "claim_type": claim.claim_type.value,
                    "statement": claim.statement,
                    "evidence_ids": list(claim.evidence_ids),
                }
                for claim in self.claims
            ],
            "limitations": list(self.report.limitations),
        }

    def certified_json(self) -> dict[str, Any]:
        """The rendering that may be exported, or a refusal.

        Raises:
            ReportNotCertified: The report has at least one unresolved conflict.
        """
        if not self.certified:
            codes = ", ".join(
                sorted({str(conflict.get("code", "unknown")) for conflict in self.conflicts})
            )
            raise ReportNotCertified(
                f"report {self.report.report_id} is {self.report.status.value} "
                f"({codes or 'conflicted'}); it may be read, but not exported as "
                "a certified report"
            )
        return self.report_json()


@dataclass(frozen=True, slots=True)
class EvidenceRequest:
    """A ledger record the report means to cite.

    No execution type and no data mode: both are derived. `content_hash` is an
    assertion to check, not a value to store; a disagreement is a conflict.
    """

    evidence_id: str
    locator: str = ""
    content_hash: str | None = None


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    """A statement to verify against the ledger before it becomes a claim."""

    statement: str
    claim_type: ClaimClass
    evidence_ids: tuple[str, ...] = ()
    execution_refs: tuple[str, ...] = ()
    execution_node_id: str | None = None
    execution_count: int | None = None


@dataclass(frozen=True, slots=True)
class ReportRequest:
    """Everything a report author supplies. Everything else is derived."""

    title: str
    claims: tuple[ClaimRequest, ...]
    evidence: tuple[EvidenceRequest, ...] = ()
    limitations: tuple[str, ...] = ()


__all__ = [
    "ClaimRequest",
    "EvidenceRequest",
    "ReportClaim",
    "ReportCommentary",
    "ReportEvidenceRef",
    "ReportNotCertified",
    "ReportRequest",
    "ReportSnapshot",
    "ScientificReport",
    "report_uuid",
]
