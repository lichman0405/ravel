"""the conversation is project state

Phase 8 puts a person in front of Master through the TUI, and where that
exchange is stored decides whether it survives anything.

The harness has a session log, and reading the transcript back out of it would
be the shortest path — and the wrong one. A DSH session dies with its process
and cannot be resumed across one (`vendor/DSH_PIN.json` records that
limitation, reproduced in `tests/dsh`), so a conversation kept only there
would be lost exactly when somebody wants to read what was agreed. `docs/04`
states the rule in the other direction too: DSH session is not Project.

So the transcript is a table. Append-only, because a message is a thing that
was said: nothing edits one and nothing unsays one, which is what lets a later
reader treat the transcript as a record of what happened rather than of what
somebody last thought should have happened. Absent from `UPDATABLE_TABLES`,
so the append-only trigger applies and no guard code changed.

`turn_id` groups a person's question with everything Master said back. It is
stored rather than inferred from timestamps because Master may spend several
turns on tool calls before it says anything, and "which reply answers which
question" is not recoverable from a clock.

Revision ID: b1c4a7e2f903
Revises: 9d2f5b7c4e18
Created: 2026-09-19 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b1c4a7e2f903"
down_revision: str | None = "9d2f5b7c4e18"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the transcript table."""
    op.create_table(
        "master_messages",
        sa.Column("message_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("turn_id", sa.String(length=32), nullable=False),
        sa.Column("author_id", sa.String(length=32), nullable=False),
        sa.Column("author_type", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "author_type IN ('USER', 'AGENT', 'SYSTEM', 'BACKEND')",
            name=op.f("ck_master_messages_author_type_is_known"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_master_messages_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("message_id", name=op.f("pk_master_messages")),
    )
    op.create_index(
        "ix_master_messages_turn", "master_messages", ["project_id", "turn_id"], unique=False
    )
    op.create_index(
        "ix_master_messages_order",
        "master_messages",
        ["project_id", "created_at"],
        unique=False,
    )

    _install_guards()


def _install_guards() -> None:
    """Attach the append-only trigger to the new table.

    Delegated rather than written out, for the reason every revision delegates
    it: the migration, the test schema, and `create_all` must install
    byte-identical rules, and a hand-written list here would be a second place
    for them to differ. `master_messages` is not in `UPDATABLE_TABLES`, so what
    this installs is the trigger that refuses `UPDATE` and `DELETE` — which is
    the property the revision is for rather than a side effect of it.
    """
    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Drop the table and the trigger that was attached to it."""
    op.execute("DROP TRIGGER IF EXISTS ravel_append_only ON master_messages")
    op.drop_index("ix_master_messages_order", table_name="master_messages")
    op.drop_index("ix_master_messages_turn", table_name="master_messages")
    op.drop_table("master_messages")
