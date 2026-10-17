"""project leases: one driver per project, with a monotonic epoch

Phase 11 made a project's processes legible but not mutually exclusive: two
supervisors could discover the same active project and drive it through two
sets of DSH sessions. This table is the database-level answer.

`project_id` is the primary key, so the database — not a process-local map —
decides who is driving. `epoch` is the fencing token: an owner increments it
only by taking over an expired or released row, and every later write/start
checks the epoch it holds. Expiry comparisons use the database clock.

Revision ID: b7e4c2a1d9f3
Revises: d1f7a40c8e21
Created: 2026-10-17 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b7e4c2a1d9f3"
down_revision: str | None = "d1f7a40c8e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_leases",
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("owner_instance_id", sa.String(128), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("renewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("epoch >= 1", name=op.f("ck_project_leases_epoch_positive")),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_project_leases_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("project_id", name="pk_project_leases"),
    )
    op.create_index(
        "ix_project_leases_expires_at", "project_leases", ["expires_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_project_leases_expires_at", table_name="project_leases")
    op.drop_table("project_leases")
