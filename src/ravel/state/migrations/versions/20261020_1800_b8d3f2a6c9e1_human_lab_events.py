"""person-reported lab events, package binding, lead confirmation, upload receipts

A laboratory handover now records the mode it was opened in and the lead
confirmation it was opened under. Four append-only tables carry the rest:

- `lab_package_bindings` binds one prepared package to the protocol version,
  sample labels, required raw files, device identity and SOP references it was
  built from.
- `lab_lead_confirmations` records a lab lead's statement that the SOP, site
  and device suit that package. Readiness for real execution is derived from a
  covering confirmation, never from a mutable flag.
- `lab_events` holds what a person reported: acknowledged, start reported,
  deviation reported, output uploaded, stop confirmed. `person_reported` is
  constrained to TRUE; system withdrawal changes `lab_handovers` and writes no
  `STOP_CONFIRMED`.
- `lab_upload_receipts` keeps every upload attempt, including the late and
  refused ones, so a rejection is history rather than silence.

`ACKNOWLEDGED`, `START_REPORTED` and `STOP_CONFIRMED` are one per handover,
enforced by partial unique indexes; deviations and uploads may repeat. All four
tables are absent from `guards.UPDATABLE_TABLES`, so the guard installed at the
end of this revision rejects UPDATE and DELETE. `lab_handovers.execution_mode`
and `lead_confirmation_ref` are added to its identity columns, so a handover's
mode cannot be rewritten after it was opened.

Revision ID: b8d3f2a6c9e1
Revises: a4d2e6f8b1c3
Created: 2026-10-20 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ravel.state import guards

revision: str = "b8d3f2a6c9e1"
down_revision: str | None = "a4d2e6f8b1c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing handovers were opened before execution modes existed. They were
    # all software handovers, so SIMULATED is the true value rather than a
    # placeholder, and the default is dropped afterwards so the column is
    # written explicitly from here on.
    op.add_column(
        "lab_handovers",
        sa.Column(
            "execution_mode",
            sa.String(length=16),
            nullable=False,
            server_default="SIMULATED",
        ),
    )
    op.add_column(
        "lab_handovers",
        sa.Column(
            "lead_confirmation_ref",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.alter_column("lab_handovers", "execution_mode", server_default=None)
    op.alter_column("lab_handovers", "lead_confirmation_ref", server_default=None)
    op.create_check_constraint(
        op.f("ck_lab_handovers_execution_mode_is_known"),
        "lab_handovers",
        "execution_mode IN ('SIMULATED', 'REAL')",
    )

    op.create_table(
        "lab_package_bindings",
        sa.Column("binding_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("preparation_id", sa.String(length=32), nullable=False),
        sa.Column("execution_contract_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_contract_version", sa.Integer(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column(
            "sample_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "required_raw_files",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("device_identity", sa.Text(), nullable=False),
        sa.Column(
            "sop_refs",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("bound_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "execution_contract_version >= 1",
            name=op.f("ck_lab_package_bindings_contract_version_is_positive"),
        ),
        sa.CheckConstraint(
            "protocol_version >= 1",
            name=op.f("ck_lab_package_bindings_protocol_version_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_lab_package_bindings_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_lab_package_bindings_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["preparation_id"],
            ["execution_preparations.preparation_id"],
            name=op.f(
                "fk_lab_package_bindings_preparation_id_execution_preparations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("binding_id", name=op.f("pk_lab_package_bindings")),
        sa.UniqueConstraint(
            "project_id",
            "preparation_id",
            name="uq_lab_package_bindings_preparation",
        ),
    )
    op.create_index(
        "ix_lab_package_bindings_project_node",
        "lab_package_bindings",
        ["project_id", "node_id"],
    )

    op.create_table(
        "lab_lead_confirmations",
        sa.Column("confirmation_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("preparation_id", sa.String(length=32), nullable=False),
        sa.Column("execution_contract_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_contract_version", sa.Integer(), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("sop_ref", sa.String(length=256), nullable=False),
        sa.Column("site_ref", sa.String(length=256), nullable=False),
        sa.Column("device_identity", sa.String(length=256), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("confirmed_by", sa.String(length=32), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "execution_contract_version >= 1",
            name=op.f("ck_lab_lead_confirmations_contract_version_is_positive"),
        ),
        sa.CheckConstraint(
            "protocol_version >= 1",
            name=op.f("ck_lab_lead_confirmations_protocol_version_is_positive"),
        ),
        sa.CheckConstraint(
            "char_length(statement) BETWEEN 1 AND 4000",
            name=op.f("ck_lab_lead_confirmations_statement_has_length"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_lab_lead_confirmations_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_lab_lead_confirmations_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["preparation_id"],
            ["execution_preparations.preparation_id"],
            name=op.f(
                "fk_lab_lead_confirmations_preparation_id_execution_preparations"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "confirmation_id", name=op.f("pk_lab_lead_confirmations")
        ),
        sa.UniqueConstraint(
            "project_id",
            "preparation_id",
            name="uq_lab_lead_confirmations_preparation",
        ),
    )
    op.create_index(
        "ix_lab_lead_confirmations_project_node",
        "lab_lead_confirmations",
        ["project_id", "node_id"],
    )

    op.create_table(
        "lab_events",
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("handover_id", sa.String(length=32), nullable=False),
        sa.Column("actor_user_id", sa.String(length=32), nullable=False),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("reported_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("person_reported", sa.Boolean(), nullable=False),
        sa.Column("sample_id", sa.String(length=128), nullable=True),
        sa.Column("sop_ref", sa.String(length=256), nullable=True),
        sa.Column("lead_confirmation_ref", sa.String(length=256), nullable=True),
        sa.Column("raw_output_ref", sa.String(length=1024), nullable=True),
        sa.Column("raw_output_sha256", sa.String(length=64), nullable=True),
        sa.Column("deviation_note", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "event_type IN ('ACKNOWLEDGED', 'START_REPORTED', "
            "'DEVIATION_REPORTED', 'OUTPUT_UPLOADED', 'STOP_CONFIRMED')",
            name=op.f("ck_lab_events_event_type_is_known"),
        ),
        sa.CheckConstraint(
            "protocol_version >= 1",
            name=op.f("ck_lab_events_protocol_version_is_positive"),
        ),
        sa.CheckConstraint(
            "person_reported",
            name=op.f("ck_lab_events_events_are_person_reported"),
        ),
        sa.CheckConstraint(
            "raw_output_sha256 IS NULL OR raw_output_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_lab_events_raw_output_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "deviation_note IS NULL OR char_length(deviation_note) <= 4000",
            name=op.f("ck_lab_events_deviation_note_fits_the_schema"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_lab_events_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["handover_id"],
            ["lab_handovers.handover_id"],
            name=op.f("fk_lab_events_handover_id_lab_handovers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("event_id", name=op.f("pk_lab_events")),
    )
    op.create_index(
        "uq_lab_events_acknowledged",
        "lab_events",
        ["handover_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'ACKNOWLEDGED'"),
    )
    op.create_index(
        "uq_lab_events_start_reported",
        "lab_events",
        ["handover_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'START_REPORTED'"),
    )
    op.create_index(
        "uq_lab_events_stop_confirmed",
        "lab_events",
        ["handover_id"],
        unique=True,
        postgresql_where=sa.text("event_type = 'STOP_CONFIRMED'"),
    )
    op.create_index(
        "ix_lab_events_project_handover",
        "lab_events",
        ["project_id", "handover_id"],
    )

    op.create_table(
        "lab_upload_receipts",
        sa.Column("receipt_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("handover_id", sa.String(length=32), nullable=False),
        sa.Column("output", sa.String(length=200), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("media_type", sa.String(length=200), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=32), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("artifact_id", sa.String(length=32), nullable=True),
        sa.Column("artifact_version", sa.Integer(), nullable=True),
        sa.Column("protocol_version", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('ACCEPTED', 'REJECTED_LATE', 'REJECTED_UNKNOWN_OUTPUT', "
            "'REJECTED_WRONG_CONTRACT_VERSION', 'REJECTED_WRONG_SAMPLE')",
            name=op.f("ck_lab_upload_receipts_outcome_is_known"),
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name=op.f("ck_lab_upload_receipts_content_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name=op.f("ck_lab_upload_receipts_size_is_not_negative"),
        ),
        sa.CheckConstraint(
            "protocol_version >= 1",
            name=op.f("ck_lab_upload_receipts_protocol_version_is_positive"),
        ),
        sa.CheckConstraint(
            "artifact_version IS NULL OR artifact_version >= 1",
            name=op.f("ck_lab_upload_receipts_artifact_version_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_lab_upload_receipts_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_lab_upload_receipts_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["handover_id"],
            ["lab_handovers.handover_id"],
            name=op.f("fk_lab_upload_receipts_handover_id_lab_handovers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("receipt_id", name=op.f("pk_lab_upload_receipts")),
    )
    op.create_index(
        "ix_lab_upload_receipts_project_handover",
        "lab_upload_receipts",
        ["project_id", "handover_id"],
    )

    guards.install(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "ix_lab_upload_receipts_project_handover",
        table_name="lab_upload_receipts",
    )
    op.drop_table("lab_upload_receipts")
    op.drop_index("ix_lab_events_project_handover", table_name="lab_events")
    op.drop_index("uq_lab_events_stop_confirmed", table_name="lab_events")
    op.drop_index("uq_lab_events_start_reported", table_name="lab_events")
    op.drop_index("uq_lab_events_acknowledged", table_name="lab_events")
    op.drop_table("lab_events")
    op.drop_index(
        "ix_lab_lead_confirmations_project_node",
        table_name="lab_lead_confirmations",
    )
    op.drop_table("lab_lead_confirmations")
    op.drop_index(
        "ix_lab_package_bindings_project_node",
        table_name="lab_package_bindings",
    )
    op.drop_table("lab_package_bindings")
    op.drop_constraint(
        op.f("ck_lab_handovers_execution_mode_is_known"),
        "lab_handovers",
        type_="check",
    )
    op.drop_column("lab_handovers", "lead_confirmation_ref")
    op.drop_column("lab_handovers", "execution_mode")
