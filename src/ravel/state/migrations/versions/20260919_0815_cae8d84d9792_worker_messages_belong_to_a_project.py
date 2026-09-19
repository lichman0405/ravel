"""worker messages belong to a project

`worker_messages` was the one project-scoped table in the schema without a
`project_id`. Every other row reachable from a project carries one, which is
what lets a repository scope its statements without a join, and the omission
only became visible when something finally wrote to the table: a Worker saying
`ESCALATE` is a fact about a project, and a record of a Worker's communication
that no project owns is a record nothing can authorize.

The column is added `NOT NULL` with no default. That is safe here and only
here: nothing wrote to this table before this revision, so it is empty in every
deployment, and a backfill default would be a way of inventing a project for
rows that do not exist. On a database that somehow had rows, the `ALTER` fails
rather than guessing — which is the outcome worth having.

The index is `(node_id, sent_at)` because a Worker's messages are read as the
conversation about one node, in the order it happened.

Revision ID: cae8d84d9792
Revises: 603e26d7e0c4
Created: 2026-09-19 08:15:11.573933
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cae8d84d9792"
down_revision: str | None = "603e26d7e0c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "worker_messages",
        sa.Column("project_id", sa.String(length=32), nullable=False),
    )
    op.create_foreign_key(
        op.f("fk_worker_messages_project_id_projects"),
        "worker_messages",
        "projects",
        ["project_id"],
        ["project_id"],
        ondelete="CASCADE",
    )
    op.create_index(
        "ix_worker_messages_node_sent",
        "worker_messages",
        ["node_id", "sent_at"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the column and what depends on it.

    No guard changes: `worker_messages` is append-only and was already, because
    it was never in `UPDATABLE_TABLES`. Adding a column does not change which
    trigger a table warrants.
    """
    op.drop_index("ix_worker_messages_node_sent", table_name="worker_messages")
    op.drop_constraint(
        op.f("fk_worker_messages_project_id_projects"),
        "worker_messages",
        type_="foreignkey",
    )
    op.drop_column("worker_messages", "project_id")
