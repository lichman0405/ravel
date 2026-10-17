"""a process can say it is going

`runtime_services` made a process's liveness legible. It did not make its
*shutdown* legible, and the two are the thing an operator most needs to keep
apart: a supervisor that was restarted by a deploy and one that segfaulted both
end in silence, and only one of them is an incident. Silence is the same
whether it was intended.

So the row gains `stopped_at`: null while the process is running, the moment it
recorded its own shutdown once it is not. A beat clears it, because a beat is a
process saying it is running and a row that kept the previous shutdown's time
through a restart would report a service as stopped while it was answering
requests.

**The row is not deleted on shutdown, and could not be.** The table is in
`UPDATABLE_TABLES`, whose trigger rejects every `DELETE` — a service able to
erase its own row would be able to erase one belonging to a service that never
said anything, and "the row is gone" is a much weaker thing to read than "this
process stopped at 14:02". The absence of a row means nothing has ever run
under that name, which is a fact worth being able to state.

Revision ID: d1f7a40c8e21
Revises: c5a9e2f01b73
Created: 2026-09-23 18:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d1f7a40c8e21"
down_revision: str | None = "c5a9e2f01b73"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the column, nullable, with no default.

    Nullable because every row that exists describes a process that is running
    now, and a default would have to invent a shutdown for each of them. There
    is no `server_default` and no backfill for the same reason: `NULL` already
    means the right thing about a service that is up.
    """
    op.add_column(
        "runtime_services",
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    """Drop the column.

    The rows survive it. Downgrading loses the record of which services were
    shut down and which died, which is what a downgrade of this revision means:
    the fact it added is the fact it takes away.
    """
    op.drop_column("runtime_services", "stopped_at")
