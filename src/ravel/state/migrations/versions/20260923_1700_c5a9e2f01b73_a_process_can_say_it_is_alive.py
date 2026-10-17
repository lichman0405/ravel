"""a process can say it is alive

Everything on the administrator's screen is answerable from a record except one
question: **is the process that drives projects running**. A supervisor that
died leaves a project that has stopped moving, and a project with nothing left
to do looks exactly the same — so no amount of reading the DAG, the events or
the jobs distinguishes them, and a screen that guessed would be guessing.

`runtime_services` is that answer, and it is deliberately not a record. One row
per service name, overwritten on every beat: `instance` and `started_at` move
when a process restarts, so the row always describes the process that is
beating now rather than the first one that ever did. A table of restarts would
answer a question nobody asks here and would have to be pruned.

That shapes the guard. The table joins `UPDATABLE_TABLES` — `UPDATE` is its
only write, since every beat rewrites it — and it takes the *shortest* identity
list in the schema, protecting `service` alone. Everything else on the row is
the reporting process's account of itself and is meant to be replaced; `service`
is the row's name, and protecting it is what stops one process from beating on
another's row and making a dead service look alive.

Revision ID: c5a9e2f01b73
Revises: b2f8d1c73a55
Created: 2026-09-23 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c5a9e2f01b73"
down_revision: str | None = "b2f8d1c73a55"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the liveness table and attach its guard."""
    op.create_table(
        "runtime_services",
        sa.Column("service", sa.String(length=64), primary_key=True),
        sa.Column("instance", sa.String(length=255), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        # No server default, deliberately: the metadata declares this column
        # with a Python-side one, and a migration that added a second default
        # at the database would make the migrated schema and the one
        # `create_all` builds disagree in exactly the way the shared metadata
        # exists to prevent.
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
    )

    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Drop the table and its trigger.

    The trigger goes first and by name: `DROP TRIGGER` on a table that is about
    to be dropped is redundant, and `DROP TABLE` would take it anyway — but
    leaving it to be taken implicitly would mean the downgrade of a *later*
    revision could not drop this one's trigger the way it drops every other.
    """
    op.execute("DROP TRIGGER IF EXISTS ravel_runtime_services_identity ON runtime_services")
    op.drop_table("runtime_services")
