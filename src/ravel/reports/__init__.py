"""Scientific reports over the Evidence Ledger.

A report is assembled from rows, typed from provenance, and frozen. The public
surface is the request types, the snapshot, and the repository that persists
it; classification and rendering are implementation detail a caller should
read but not be able to override.
"""

from ravel.reports.builder import BuiltReport, build_report, render_facts
from ravel.reports.classification import (
    Classification,
    LedgerIndex,
    classify,
    is_fixture_provenance,
    is_fixture_url,
    is_simulated_artifact,
    is_simulated_backend,
)
from ravel.reports.models import (
    ClaimRequest,
    EvidenceRequest,
    ReportClaim,
    ReportCommentary,
    ReportEvidenceRef,
    ReportNotCertified,
    ReportRequest,
    ReportSnapshot,
    ScientificReport,
)

__all__ = [
    "BuiltReport",
    "ClaimRequest",
    "Classification",
    "EvidenceRequest",
    "LedgerIndex",
    "ReportClaim",
    "ReportCommentary",
    "ReportEvidenceRef",
    "ReportNotCertified",
    "ReportRequest",
    "ReportSnapshot",
    "ScientificReport",
    "build_report",
    "classify",
    "is_fixture_provenance",
    "is_fixture_url",
    "is_simulated_artifact",
    "is_simulated_backend",
    "render_facts",
]
