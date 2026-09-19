"""an inconclusive ending is a decision, so it needs a type

`acceptance/V0_ACCEPTANCE.md` A20 requires a project to reach one of four
endings and to have a final audit trail. Three of them already had a decision
type: `ACCEPT_RESULT`, `REJECT_RESULT`, `TERMINATE_PROJECT`. The fourth,
`INCONCLUSIVE`, had none, and a project that stopped without succeeding and
without failing would have had to record its ending as one of the other three
— which is exactly the conflation `ProjectOutcome` exists to prevent.

The gap was invisible while `ProjectStatus.INCONCLUSIVE` existed and nothing
could write it. It becomes visible the moment Master can conclude a project:
`DecisionType` is the field a reader uses to find *why* a project ended, and
a vocabulary that cannot express one of the four endings turns that reader's
question into a guess.

Widening a closed vocabulary is safe here in the direction that matters: every
existing row still satisfies the constraint, so the migration cannot fail on
data, and `downgrade` refuses only if a row already uses the new value — in
which case the rollback is genuinely blocked and saying so is better than
deleting a decision.

Revision ID: f559c62b222e
Revises: 9e4c7b12af03
Created: 2026-09-19 10:15:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f559c62b222e"
down_revision: str | None = "9e4c7b12af03"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def _constraint() -> str:
    """The constraint as the database knows it.

    Composed in full and marked with `op.f`, as the initial migration
    does: `NAMING_CONVENTION` would otherwise prefix the already-composed
    name a second time. A function rather than a module constant because
    `op` is a proxy — calling it while the revisions are being loaded,
    before any migration is running, raises instead of returning a name.
    """
    return op.f("ck_decision_records_decision_type_is_known")

#: The vocabulary before this revision, without `CONCLUDE_INCONCLUSIVE`.
BEFORE = (
    "'CREATE_NODE', 'CANCEL_NODE', 'REPLACE_NODE', "
    "'REVISE_EXECUTION_CONTRACT', 'REVISE_ACCEPTANCE_CRITERIA', 'CHANGE_ROUTE', "
    "'RESOLVE_DEVIATION', 'ACCEPT_RESULT', 'REJECT_RESULT', "
    "'TERMINATE_PROJECT', 'RESOLVE_APPROVAL'"
)

#: The same list with it, in the position `DecisionType` declares it.
AFTER = (
    "'CREATE_NODE', 'CANCEL_NODE', 'REPLACE_NODE', "
    "'REVISE_EXECUTION_CONTRACT', 'REVISE_ACCEPTANCE_CRITERIA', 'CHANGE_ROUTE', "
    "'RESOLVE_DEVIATION', 'ACCEPT_RESULT', 'REJECT_RESULT', "
    "'CONCLUDE_INCONCLUSIVE', 'TERMINATE_PROJECT', 'RESOLVE_APPROVAL'"
)


def upgrade() -> None:
    op.drop_constraint(_constraint(), "decision_records", type_="check")
    op.create_check_constraint(
        _constraint(), "decision_records", f"decision_type IN ({AFTER})"
    )


def downgrade() -> None:
    op.drop_constraint(_constraint(), "decision_records", type_="check")
    # Narrowing over the existing rows. A project concluded as inconclusive has
    # a decision this constraint would not admit, and the migration fails on it
    # rather than dropping the record of why the project ended. The choice
    # between the two belongs to whoever is rolling back.
    op.create_check_constraint(
        _constraint(), "decision_records", f"decision_type IN ({BEFORE})"
    )
