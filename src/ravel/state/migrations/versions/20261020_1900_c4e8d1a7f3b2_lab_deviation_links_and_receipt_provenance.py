"""explicit deviation/event join and structured upload provenance

A `DEVIATION_REPORTED` lab event and the `deviation_records` row Master reads
were two records about one occurrence with no identifier between them. Node,
description and time could suggest a pairing, but a suggestion is not a
provenance chain. `lab_deviation_links` makes the pairing a row with foreign
keys to both sides and unique keys that keep it one-to-one.

`lab_upload_receipts` gains `sample_id` and `declared_protocol_version`: the
sample and contract version the uploader supplied, as columns rather than only
inside the free-text refusal `detail`. A refused upload is then queryable by
what it claimed to be for. The handover's own `protocol_version` remains the
version the file had to answer, so a wrong declaration is visible as a
difference between the two columns rather than overwriting the truth.

Both tables are append-only: `lab_deviation_links` is absent from
`guards.UPDATABLE_TABLES`, and the existing trigger on `lab_upload_receipts`
already covers its new columns.

Revision ID: c4e8d1a7f3b2
Revises: b8d3f2a6c9e1
Created: 2026-10-20 19:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ravel.state import guards

revision: str = "c4e8d1a7f3b2"
down_revision: str | None = "b8d3f2a6c9e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "lab_upload_receipts",
        sa.Column("sample_id", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "lab_upload_receipts",
        sa.Column("declared_protocol_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_lab_upload_receipts_declared_protocol_version_is_positive"),
        "lab_upload_receipts",
        "declared_protocol_version IS NULL OR declared_protocol_version >= 1",
    )

    op.create_table(
        "lab_deviation_links",
        sa.Column("link_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("handover_id", sa.String(length=32), nullable=False),
        sa.Column("deviation_id", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=32), nullable=False),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_lab_deviation_links_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_lab_deviation_links_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["handover_id"],
            ["lab_handovers.handover_id"],
            name=op.f("fk_lab_deviation_links_handover_id_lab_handovers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["deviation_id"],
            ["deviation_records.deviation_id"],
            name=op.f("fk_lab_deviation_links_deviation_id_deviation_records"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["lab_events.event_id"],
            name=op.f("fk_lab_deviation_links_event_id_lab_events"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("link_id", name=op.f("pk_lab_deviation_links")),
        sa.UniqueConstraint(
            "project_id",
            "deviation_id",
            name="uq_lab_deviation_links_deviation",
        ),
        sa.UniqueConstraint(
            "project_id",
            "event_id",
            name="uq_lab_deviation_links_event",
        ),
    )
    op.create_index(
        "ix_lab_deviation_links_project_handover",
        "lab_deviation_links",
        ["project_id", "handover_id"],
    )

    guards.install(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "ix_lab_deviation_links_project_handover",
        table_name="lab_deviation_links",
    )
    op.drop_table("lab_deviation_links")
    op.drop_constraint(
        op.f("ck_lab_upload_receipts_declared_protocol_version_is_positive"),
        "lab_upload_receipts",
        type_="check",
    )
    op.drop_column("lab_upload_receipts", "declared_protocol_version")
    op.drop_column("lab_upload_receipts", "sample_id")
    guards.install(op.get_bind())
