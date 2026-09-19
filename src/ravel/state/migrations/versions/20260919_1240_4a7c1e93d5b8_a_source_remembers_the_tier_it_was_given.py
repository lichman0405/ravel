"""a source remembers the tier it was given

`docs/05` §10 lists the source tier among what is stored for an important
source, and until now RAVEL stored it only inside `notes`, as the first clause
of a sentence written for a reader: `tier A (peer-reviewed publisher): ...`.

That was enough while nothing read it back. It stopped being enough when a
claim's tier started being *derived* from its sources rather than chosen by
whoever wrote the claim — which is the rule that makes an inflated tier
impossible rather than merely discouraged. Deriving from prose means parsing
prose, and a value that is recovered by reading a sentence is a value whose
meaning depends on how the sentence is worded; the next person to reword
`_notes` would change the evidence ledger's tiers without touching the ledger.

So the tier gets a column, and `declared_type` gets one beside it for the same
reason: it is the input that outranks the domain when a tier is assigned, and a
tier that cannot be re-derived from what is stored is a tier nobody can check.

Both are nullable. Rows written before this revision have no tier, and
assigning them one now would mean classifying sources that were registered
under a rule that did not record its answer — a guess wearing a column name.

Revision ID: 4a7c1e93d5b8
Revises: 2b8e4f7c1a35
Created: 2026-09-19 12:40:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ravel.domain.enums import EvidenceSourceTier
from ravel.state.tables import enum_check

revision: str = "4a7c1e93d5b8"
down_revision: str | None = "2b8e4f7c1a35"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "evidence_sources"
CONSTRAINT = "tier_is_known"


def upgrade() -> None:
    """Store the tier a source was given, and the type it was given for."""
    op.add_column(TABLE, sa.Column("tier", sa.String(4), nullable=True))
    op.add_column(TABLE, sa.Column("declared_type", sa.String(128), nullable=True))
    op.create_check_constraint(CONSTRAINT, TABLE, enum_check("tier", EvidenceSourceTier))


def downgrade() -> None:
    """Drop both columns.

    Losing them loses every assigned tier, which is why the upgrade is written
    to be re-runnable over a schema that has them: an operator who rolls back
    and forward again gets the columns back empty rather than getting an error
    that leaves the migration half applied.
    """
    op.drop_constraint(CONSTRAINT, TABLE, type_="check")
    op.drop_column(TABLE, "declared_type")
    op.drop_column(TABLE, "tier")
