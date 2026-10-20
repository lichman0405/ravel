"""T12-AC3: report rows are append-only in PostgreSQL, not only in Python."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.premium.T12.support import (
    a_real_source,
    an_evidence,
    explain,
    read_report,
    report,
)

from ravel.domain.enums import ClaimClass
from ravel.reports.models import ClaimRequest, EvidenceRequest, ReportRequest
from ravel.state import guards

REPORT_TABLES = (
    "scientific_reports",
    "report_claims",
    "report_evidence_refs",
    "report_commentaries",
)


def test_report_tables_are_outside_the_updatable_set() -> None:
    append_only = guards.append_only_tables()
    for table in REPORT_TABLES:
        assert table in append_only, f"{table} must reject UPDATE and DELETE"
        assert table not in guards.UPDATABLE_TABLES, (
            f"{table} must not be updatable; a report is a snapshot, not a draft"
        )


def test_raw_update_and_delete_are_refused_for_every_report_table(
    database, project
) -> None:
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Append-only report",
            evidence=(EvidenceRequest(evidence.evidence_id, locator="pp. 1-2"),),
            claims=(
                ClaimRequest(
                    statement="The claim stays as written.",
                    claim_type=ClaimClass.HYPOTHESIS,
                    evidence_ids=(evidence.evidence_id,),
                ),
            ),
        ),
    )
    report_id = snapshot.report.report_id
    explain(
        database,
        project.project_id,
        report_id,
        "The explanation stays as written.",
    )
    original = read_report(database, project.project_id, report_id)
    assert original.claims
    assert original.evidence_refs
    assert original.commentary is not None

    assignments = {
        "scientific_reports": "title = 'rewritten'",
        "report_claims": "statement = 'rewritten'",
        "report_evidence_refs": "locator = 'rewritten'",
        "report_commentaries": "body = 'rewritten'",
    }
    for table in REPORT_TABLES:
        with (
            pytest.raises(IntegrityError, match="append-only"),
            database.engine.begin() as connection,
        ):
            connection.execute(
                text(
                    f"UPDATE {table} SET {assignments[table]} "
                    "WHERE report_id = :report_id"
                ),
                {"report_id": report_id},
            )
        with (
            pytest.raises(IntegrityError, match="append-only"),
            database.engine.begin() as connection,
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE report_id = :report_id"),
                {"report_id": report_id},
            )

    unchanged = read_report(database, project.project_id, report_id)
    assert unchanged.report.title == "Append-only report"
    assert unchanged.claims[0].statement == "The claim stays as written."
    assert unchanged.evidence_refs[0].locator == "pp. 1-2"
    assert unchanged.commentary is not None
    assert unchanged.commentary.body == "The explanation stays as written."
    assert unchanged.report_json() == original.report_json()

    with database.read_only() as session:
        counts = {
            table: session.execute(
                text(f"SELECT count(*) FROM {table} WHERE report_id = :report_id"),
                {"report_id": report_id},
            ).scalar()
            for table in REPORT_TABLES
        }
    assert counts == {
        "scientific_reports": 1,
        "report_claims": 1,
        "report_evidence_refs": 1,
        "report_commentaries": 1,
    }
