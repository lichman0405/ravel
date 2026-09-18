"""evidence source snapshots are object keys, not identifiers

A source's `snapshot_ref` is the object-store key of its stored copy, and a key
is a path: `projects/<project>/<artifact>/<version>/<filename>`. That is longer
than the 64 characters the column allowed, and it grows with the filename, so
the first real registration through the research gateway failed with a
truncation error. The column is now `Text`, matching
`artifact_versions.storage_key`, which holds the same string for the same
object.

Nothing is lost by widening it and nothing is gained by narrowing it on the way
back down: a key that fits in 64 characters still fits in 64 characters, so the
downgrade is safe for any database this ran against. It would fail only for a
row written while the column was `Text` with a longer key, and failing loudly is
better than truncating a reference to stored bytes.

Revision ID: d3e067ead13a
Revises: 5c2355f96e2a
Created: 2026-09-19 06:57:40.654223
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3e067ead13a"
down_revision: str | None = "5c2355f96e2a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column(
        "evidence_sources",
        "snapshot_ref",
        existing_type=sa.VARCHAR(length=64),
        type_=sa.Text(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "evidence_sources",
        "snapshot_ref",
        existing_type=sa.Text(),
        type_=sa.VARCHAR(length=64),
        existing_nullable=True,
    )
