"""account deactivations, recorded rather than overwritten

An account is stopped by writing a row here, not by updating `users`. That
table is append-only — an account's existence is a fact about the past — and a
security decision to refuse an account has to say who made it and when. The two
facts are therefore kept apart: `users.is_active` can be false when an account
was created that way, and this table records every later stop.

The table is append-only like every other table the guards do not name as
updatable. `guards.install` is called here so that a fresh `alembic upgrade
head` carries the same triggers as the test schema; the migration parity test
compares them.

Revision ID: 7e4c1b2a9d3f
Revises: d1b8e4f6a2c9
Created: 2026-10-19 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ravel.state import guards

revision: str = "7e4c1b2a9d3f"
down_revision: str | None = "d1b8e4f6a2c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "account_deactivations",
        sa.Column("user_id", sa.String(32), nullable=False),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_by", sa.String(64), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name="fk_account_deactivations_user_id_users",
        ),
        sa.PrimaryKeyConstraint("user_id", name="pk_account_deactivations"),
    )
    guards.install(op.get_bind())


def downgrade() -> None:
    op.drop_table("account_deactivations")
