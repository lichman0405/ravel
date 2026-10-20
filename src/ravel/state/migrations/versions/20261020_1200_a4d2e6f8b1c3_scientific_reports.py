"""scientific reports, typed evidence references, and Master commentary

A report is a snapshot of what the ledger supported at a moment. Its evidence
references carry the type derived from each cited row's provenance, and its
claims are the ones that survived verification; rejected assertions live in
the report's `conflicts` beside the original ledger values rather than being
silently corrected.

All four tables are append-only. They are deliberately absent from
`guards.UPDATABLE_TABLES`, so the guard installed at the end of this revision
rejects UPDATE and DELETE. A corrected report is a new report id.

Revision ID: a4d2e6f8b1c3
Revises: 7e4c1b2a9d3f
Created: 2026-10-20 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ravel.state import guards

revision: str = "a4d2e6f8b1c3"
down_revision: str | None = "7e4c1b2a9d3f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "scientific_reports",
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("data_mode", sa.String(16), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column(
            "limitations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "rendered",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "conflicts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "data_mode IN ('REAL', 'SIMULATED', 'MIXED')",
            name=op.f("ck_scientific_reports_data_mode_is_known"),
        ),
        sa.CheckConstraint(
            "status IN ('CERTIFIED', 'REPORT_CONFLICT')",
            name=op.f("ck_scientific_reports_status_is_known"),
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 500",
            name=op.f("ck_scientific_reports_title_has_length"),
        ),
        sa.CheckConstraint(
            "status <> 'CERTIFIED' OR conflicts = '[]'::jsonb",
            name=op.f("ck_scientific_reports_certified_has_no_conflicts"),
        ),
        sa.CheckConstraint(
            "status <> 'REPORT_CONFLICT' OR jsonb_array_length(conflicts) > 0",
            name=op.f("ck_scientific_reports_conflict_names_a_conflict"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_scientific_reports_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "report_id", name=op.f("pk_scientific_reports")
        ),
    )
    op.create_index(
        "ix_scientific_reports_project_generated",
        "scientific_reports",
        ["project_id", "generated_at"],
    )

    op.create_table(
        "report_claims",
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("claim_id", sa.String(36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("claim_type", sa.String(16), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column(
            "evidence_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "execution_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("execution_node_id", sa.String(32), nullable=True),
        sa.Column("execution_count", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "claim_type IN ('FACT', 'INFERENCE', 'HYPOTHESIS')",
            name=op.f("ck_report_claims_claim_type_is_known"),
        ),
        sa.CheckConstraint(
            "position >= 1", name=op.f("ck_report_claims_position_is_positive")
        ),
        sa.CheckConstraint(
            "execution_count IS NULL OR execution_count >= 0",
            name=op.f("ck_report_claims_execution_count_is_not_negative"),
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["scientific_reports.report_id"],
            name=op.f("fk_report_claims_report_id_scientific_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "report_id", "claim_id", name=op.f("pk_report_claims")
        ),
        sa.UniqueConstraint(
            "report_id",
            "position",
            name=op.f("one_claim_per_report_position"),
        ),
    )
    op.create_index(
        "ix_report_claims_report_position",
        "report_claims",
        ["report_id", "position"],
    )

    op.create_table(
        "report_evidence_refs",
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("evidence_id", sa.String(64), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("execution_type", sa.String(24), nullable=False),
        sa.Column("locator", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.String(128), nullable=True),
        sa.Column(
            "original_record",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.CheckConstraint(
            "execution_type IN ('RESEARCH_SOURCE', 'COMPUTATION', 'HUMAN_LAB', "
            "'SIMULATED')",
            name=op.f("ck_report_evidence_refs_execution_type_is_known"),
        ),
        sa.CheckConstraint(
            "position >= 1",
            name=op.f("ck_report_evidence_refs_position_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["scientific_reports.report_id"],
            name=op.f("fk_report_evidence_refs_report_id_scientific_reports"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "report_id", "evidence_id", name=op.f("pk_report_evidence_refs")
        ),
        sa.UniqueConstraint(
            "report_id",
            "position",
            name=op.f("one_evidence_ref_per_report_position"),
        ),
    )
    op.create_index(
        "ix_report_evidence_refs_report_position",
        "report_evidence_refs",
        ["report_id", "position"],
    )

    op.create_table(
        "report_commentaries",
        sa.Column("commentary_id", sa.String(32), nullable=False),
        sa.Column("report_id", sa.String(36), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('master', 'research', 'review', 'compute-worker', "
            "'experimental-worker')",
            name=op.f("ck_report_commentaries_role_is_known"),
        ),
        sa.CheckConstraint(
            "char_length(body) > 0",
            name=op.f("ck_report_commentaries_commentary_is_not_empty"),
        ),
        sa.ForeignKeyConstraint(
            ["report_id"],
            ["scientific_reports.report_id"],
            name=op.f("fk_report_commentaries_report_id_scientific_reports"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_report_commentaries_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "commentary_id", name=op.f("pk_report_commentaries")
        ),
        sa.UniqueConstraint(
            "report_id", name=op.f("one_commentary_per_report")
        ),
    )
    op.create_index(
        "ix_report_commentaries_project_report",
        "report_commentaries",
        ["project_id", "report_id"],
    )

    guards.install(op.get_bind())


def downgrade() -> None:
    op.drop_table("report_commentaries")
    op.drop_table("report_evidence_refs")
    op.drop_table("report_claims")
    op.drop_table("scientific_reports")
