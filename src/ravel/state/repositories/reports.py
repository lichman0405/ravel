"""Reports as rows: assemble once, read back as a snapshot.

The repository is project-scoped like every other one. `create` reads the
project's ledger, asks the builder for a snapshot, and stages the report, its
claims and its evidence references in one transaction. `read` reassembles the
snapshot from those rows and from nothing else; it never recomputes a count
from the current ledger, so a report cannot change because the project did.

Two authoring rules are enforced here rather than in a prompt:

- A report is assembled by Master or Review, the two seats the schema names.
- Commentary is Master's alone, is a separate row, and cannot be attached to
  another project's report.
"""

from __future__ import annotations

from sqlalchemy import select

from ravel.domain.roles import AgentRole, require_role
from ravel.reports.builder import build_report
from ravel.reports.classification import LedgerIndex
from ravel.reports.models import (
    ReportClaim,
    ReportCommentary,
    ReportEvidenceRef,
    ReportRequest,
    ReportSnapshot,
    ScientificReport,
)
from ravel.state.mapping import build_row, from_row
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.tables import (
    ReportClaimRow,
    ReportCommentaryRow,
    ReportEvidenceRefRow,
    ScientificReportRow,
)

#: The seats the report schema names as assemblers.
REPORT_AUTHORS: frozenset[AgentRole] = frozenset(
    {AgentRole.MASTER, AgentRole.REVIEW}
)


class ReportAuthorError(PermissionError):
    """Raised when a role that does not assemble reports tries to."""


def require_report_author(role: AgentRole, action: str) -> None:
    """Refuse report assembly to every role but Master and Review."""
    if role not in REPORT_AUTHORS:
        raise ReportAuthorError(
            f"the {role.value} role may not {action}; only Master and Review "
            "assemble reports from the ledger"
        )


class ScientificReportRepository(ProjectScopedRepository[ScientificReport]):
    """Immutable reports for one project, and the rows they rest on."""

    row_type = ScientificReportRow
    record_type = ScientificReport

    def _order_by(self) -> object:
        """When the report was generated, which is its only timestamp."""
        return ScientificReportRow.generated_at

    def create(
        self,
        request: ReportRequest,
        *,
        role: AgentRole,
        actor_id: str = "",
    ) -> ReportSnapshot:
        """Assemble and stage one report snapshot.

        Raises:
            ReportAuthorError: The role is neither Master nor Review.
            ValueError: The request is ambiguous (duplicate ids, empty claim).
        """
        require_report_author(role, "assemble a scientific report")
        index = LedgerIndex.load(self.session, self.project_id)
        built = build_report(index, self.project_id, request)
        self.add(built.report)
        for claim in built.claims:
            self.session.add(build_row(ReportClaimRow, claim))
        for evidence_ref in built.evidence_refs:
            self.session.add(build_row(ReportEvidenceRefRow, evidence_ref))
        return self.read(built.report.report_id)

    def read(self, report_id: str) -> ReportSnapshot:
        """Read one report with its claims, evidence index and commentary.

        Raises:
            NotFound: This project has no such report.
        """
        report = self.get(report_id=report_id)
        claims = tuple(
            from_row(ReportClaim, row)
            for row in self.session.execute(
                select(ReportClaimRow)
                .where(ReportClaimRow.report_id == report.report_id)
                .order_by(ReportClaimRow.position)
            ).scalars()
        )
        evidence_refs = tuple(
            from_row(ReportEvidenceRef, row)
            for row in self.session.execute(
                select(ReportEvidenceRefRow)
                .where(ReportEvidenceRefRow.report_id == report.report_id)
                .order_by(ReportEvidenceRefRow.position)
            ).scalars()
        )
        commentary_row = self.session.execute(
            select(ReportCommentaryRow).where(
                ReportCommentaryRow.project_id == self.project_id,
                ReportCommentaryRow.report_id == report.report_id,
            )
        ).scalar_one_or_none()
        return ReportSnapshot(
            report=report,
            claims=claims,
            evidence_refs=evidence_refs,
            commentary=(
                from_row(ReportCommentary, commentary_row)
                if commentary_row is not None
                else None
            ),
        )

    def certified_json(self, report_id: str) -> dict[str, object]:
        """The frozen-schema export, refusing a conflicted report.

        Raises:
            ReportNotCertified: The report has at least one conflict.
        """
        return self.read(report_id).certified_json()

    def comment(
        self,
        report_id: str,
        body: str,
        *,
        role: AgentRole,
        author: str,
    ) -> ReportCommentary:
        """Attach Master's explanation to a report, without touching it.

        The report row is not updated — the guard would reject that, and the
        point is that nothing needs it. A report has one explanation; further
        explanation belongs to a new report, which is the only way to change
        what the certified export says.

        Raises:
            PermissionError: The caller is not Master.
            NotFound: This project has no such report.
            ValueError: The report already has an explanation, or the body is empty.
        """
        require_role(role, AgentRole.MASTER, "explain a scientific report")
        report = self.get(report_id=report_id)
        existing = self.session.execute(
            select(ReportCommentaryRow).where(
                ReportCommentaryRow.project_id == self.project_id,
                ReportCommentaryRow.report_id == report.report_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise ValueError(
                f"report {report.report_id} already has Master's explanation; "
                "attach further explanation to a newly assembled report so the "
                "certified version is not rewritten in place"
            )
        commentary = ReportCommentary(
            report_id=report.report_id,
            project_id=self.project_id,
            author=author,
            role=role,
            body=body,
        )
        self.session.add(build_row(ReportCommentaryRow, commentary))
        return commentary

    def commentary_for(self, report_id: str) -> ReportCommentary | None:
        """The explanation attached to a report, if Master wrote one."""
        report = self.get(report_id=report_id)
        row = self.session.execute(
            select(ReportCommentaryRow).where(
                ReportCommentaryRow.project_id == self.project_id,
                ReportCommentaryRow.report_id == report.report_id,
            )
        ).scalar_one_or_none()
        return from_row(ReportCommentary, row) if row is not None else None

    def all(self, **equalities: object) -> list[ScientificReport]:
        """Every report in this project, oldest first."""
        return super().all(**equalities)


__all__ = [
    "REPORT_AUTHORS",
    "ReportAuthorError",
    "ScientificReportRepository",
    "require_report_author",
]
