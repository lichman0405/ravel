"""a review measures against the criteria the node was given

`review_records` was written before anything could write one, and the name it
gave its central reference assumed every reviewed node would have an Acceptance
Contract. Only COMPUTATION and EXPERIMENT nodes do: `can_enter_running` requires
frozen acceptance criteria of those two and of no others, so a RESEARCH or
HYPOTHESIS node runs with an Execution Contract and nothing else. A review of
one of those has to name what it measured against, and the old name would have
made it either lie about which contract it read or leave the column null on a
`NOT NULL` field.

`frozen_criteria_ref` is what the column always meant. The review reads the
definition of done that was frozen before the run — the Acceptance Contract for
the two node types that have one, the Execution Contract for the rest — and
records its identifier and version so that a later change to what the node was
asked for cannot be read back into the verdict.

The rename is safe in a way most are not: nothing writes this table yet. It is
in the initial schema and no code path had reached it, so there are no rows to
migrate and no reader to update.

Revision ID: 3d9a6f01b7c2
Revises: 7b1f4c2a90de
Created: 2026-09-19 09:24:18.660417
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "3d9a6f01b7c2"
down_revision: str | None = "7b1f4c2a90de"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RENAMES = (
    ("frozen_acceptance_contract_ref", "frozen_criteria_ref"),
    ("frozen_acceptance_version", "frozen_criteria_version"),
)


def upgrade() -> None:
    """Rename both columns, in the direction the new name means.

    No guard changes: `review_records` is append-only, was already, and a
    rename does not alter which trigger a table warrants.
    """
    for before, after in RENAMES:
        op.alter_column("review_records", before, new_column_name=after)


def downgrade() -> None:
    """Put the old names back.

    Nothing is refused here, unlike the vocabulary narrowing two revisions
    back: a name carries no information a row would lose, so the reverse is a
    rename and nothing else.
    """
    for before, after in RENAMES:
        op.alter_column("review_records", after, new_column_name=before)
