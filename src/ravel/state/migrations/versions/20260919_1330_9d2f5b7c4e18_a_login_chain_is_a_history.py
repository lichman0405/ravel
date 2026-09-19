"""a login chain is a history, not a row that moves

`docs/09` §2 asks V0 for Argon2id passwords, a short-lived access token, and
refresh-token rotation. The first two need no storage. Rotation does, and how
it is stored decides whether a replay can be *detected* or only *hoped
against*.

The obvious design gives a token a `used` column and sets it on the way out.
That makes detection depend on a write having happened: a process that dies
between accepting a token and recording the acceptance leaves a token that
looks unused and is still valid, which is precisely the state a replay wants.
It also needs `UPDATE`, which in this schema means joining `UPDATABLE_TABLES`
and carrying a column-level guard so that only `used` can move.

So rotation writes a **second row** naming the first as its `parent_id`. "Was
this token already presented?" becomes `EXISTS (SELECT 1 FROM refresh_tokens
WHERE parent_id = :token_id)`, which is answered from what was written rather
than from what was remembered. Both tables are therefore append-only, and no
guard code changed to make them so: a table absent from `UPDATABLE_TABLES`
gets the append-only trigger automatically, which is the default this schema
was built around.

A detected replay revokes the whole `family_id` — the chain from one login.
Both the thief and the victim lose it, which is the only safe answer once a
token is known to have been copied, and it is a row in
`revoked_token_families` rather than a column, so the record of the revocation
cannot be edited away.

Revision ID: 9d2f5b7c4e18
Revises: 4a7c1e93d5b8
Created: 2026-09-19 13:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9d2f5b7c4e18"
down_revision: str | None = "4a7c1e93d5b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The digest is hex-encoded SHA-256, so its width is fixed by the algorithm
#: rather than by taste. Stated as a length rather than as `Text` so that a
#: future change to a different digest is a migration rather than a surprise.
HASH_LENGTH = 64


def upgrade() -> None:
    """Create the two tables a rotating refresh token needs."""
    op.create_table(
        "refresh_tokens",
        sa.Column("token_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("family_id", sa.String(length=32), nullable=False),
        sa.Column("parent_id", sa.String(length=32), nullable=True),
        sa.Column("token_hash", sa.String(length=HASH_LENGTH), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "expires_at > issued_at", name=op.f("ck_refresh_tokens_a_grant_outlives_its_issue")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name=op.f("fk_refresh_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("token_id", name=op.f("pk_refresh_tokens")),
    )
    op.create_index("ix_refresh_tokens_family", "refresh_tokens", ["family_id"], unique=False)
    op.create_index("ix_refresh_tokens_hash", "refresh_tokens", ["token_hash"], unique=True)

    op.create_table(
        "revoked_token_families",
        sa.Column("family_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.user_id"],
            name=op.f("fk_revoked_token_families_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("family_id", name=op.f("pk_revoked_token_families")),
    )

    _install_guards()


def _install_guards() -> None:
    """Attach the append-only trigger to both new tables.

    Delegated rather than written out, for the reason every revision delegates
    it: the migration, the test schema, and `create_all` must install
    byte-identical rules, and a hand-written list here would be a second place
    for them to differ. Neither table is in `UPDATABLE_TABLES`, so what this
    installs is the trigger that refuses `UPDATE` and `DELETE` — which is the
    point of the revision rather than a side effect of it.
    """
    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Drop both tables and the triggers attached to them.

    The triggers go with their tables. No guard *function* is dropped, because
    both tables use the shared append-only function that every other table in
    the schema still depends on.
    """
    op.execute("DROP TRIGGER IF EXISTS ravel_append_only ON revoked_token_families")
    op.execute("DROP TRIGGER IF EXISTS ravel_no_delete ON revoked_token_families")
    op.execute("DROP TRIGGER IF EXISTS ravel_contract_freeze ON revoked_token_families")
    op.drop_table("revoked_token_families")
    op.execute("DROP TRIGGER IF EXISTS ravel_append_only ON refresh_tokens")
    op.execute("DROP TRIGGER IF EXISTS ravel_no_delete ON refresh_tokens")
    op.execute("DROP TRIGGER IF EXISTS ravel_contract_freeze ON refresh_tokens")
    op.drop_index("ix_refresh_tokens_hash", table_name="refresh_tokens")
    op.drop_index("ix_refresh_tokens_family", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
