"""a contract names the environment it runs in

Phase 11 puts a step between a frozen Execution Contract and a Worker starting
work: something has to turn the terms into the files a machine or a bench
needs. That step has to be told *whether* to build anything, and the answer
belongs to the contract rather than to the node type — a COMPUTATION node that
runs a calculation needs a workspace with inputs and a job script, and a
COMPUTATION node that analyses numbers already on disk needs neither. Master
plans the node and knows which it is planning, so `execution_requirements` is
Master's to write, keyed by kind: `{"software": "raspa"}` or
`{"lab": "bench-chemistry"}`.

JSONB because the value is a small map and nothing queries inside it: what
reads it is the preparation layer, which asks whether the contract requires
anything and for which package.

**The column is added `NOT NULL` with a server default, and the default is
then dropped.** `execution_contracts` is not empty in a deployed database —
every node a Worker may start has a frozen contract — so an `ALTER` without a
default would fail on the rows that exist. `'{}'` is the honest value for
them and not merely a filler: a contract written before this revision names no
environment, which is exactly what an empty map means, and the execution loop
reads it as "nothing to prepare". The `server_default` is removed afterwards
so that the schema a migrated database ends with is the schema `create_all`
builds — a lingering default would be a rule about new rows that lives only in
the deployed database.

No guard changes. `execution_contracts` is already in `UPDATABLE_TABLES` for
its `frozen_at` column with the accompanying freeze trigger, and a contract's
terms are written before it is frozen; adding a term to the list does not
change which columns may move afterwards.

Revision ID: c8f2a5d10e47
Revises: d4e7a1b90c26
Created: 2026-09-23 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c8f2a5d10e47"
down_revision: str | None = "d4e7a1b90c26"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "execution_contracts"
COLUMN = "execution_requirements"


def upgrade() -> None:
    """Add the column, fill the rows that predate it, and drop the filler."""
    op.add_column(
        TABLE,
        sa.Column(
            COLUMN,
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column(TABLE, COLUMN, server_default=None)


def downgrade() -> None:
    """Drop the column.

    No guard changes, for the reason the upgrade makes: this table's triggers
    are about `frozen_at` and about a contract's identity, and neither the
    presence nor the absence of a term changes them.
    """
    op.drop_column(TABLE, COLUMN)
