"""a deviation can be resolved, which is the one UPDATE it permits

`DeviationRepository.resolve` writes `resolved_by_decision_ref` and
`resolved_at` onto an escalation, and the table carried the append-only
trigger — so every resolution was refused by the database, and had been since
the trigger was installed. The mistake was invisible for exactly as long as
nothing called the method, which was until A12 needed an answer to a Worker's
escalation.

`state/repositories/records.py` already described the intent: "a deviation is
the one exception in spirit: it is *resolved* by writing a decision reference
onto it, which is a single permitted UPDATE". The check constraint that makes
that write all-or-nothing (`resolution_is_all_or_nothing`) was already there.
What was missing was the table's name in `UPDATABLE_TABLES`, and the identity
trigger that keeps the permission narrow.

So this revision does not widen what a deviation *is* — it narrows the write
to the two columns the resolution actually sets. Everything describing the
escalation is fixed by `ravel_deviation_records_identity`: which node raised
it, what was asked for, why the contract refused, and when. Without that
trigger, moving the table into the updatable set would have made the whole row
rewritable, and a deviation whose `requested_action` could be edited after
Master answered it is a record that can be made to agree with any decision
taken.

Delegated to `guards.install`, as the backend-jobs revision is, so the
migration, the test schema, and `create_all` install byte-identical rules.

Revision ID: 7c1d9b40e6a2
Revises: f559c62b222e
Created: 2026-09-19 10:25:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "7c1d9b40e6a2"
down_revision: str | None = "f559c62b222e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Replace the append-only trigger with the narrow identity one."""
    _install_guards()


def downgrade() -> None:
    """Put the append-only trigger back, refusing resolutions again.

    This fails on a database where a deviation has been resolved: the
    append-only trigger is `BEFORE UPDATE` and does not look at the rows, so
    the DDL succeeds — and the rows it would have forbidden stay in the table,
    readable, with a resolution nothing may now write. That is the honest
    outcome of rolling back a permission: the records made under it are not
    undone by taking it away.
    """
    _install_guards()


def _install_guards() -> None:
    from ravel.state import guards

    guards.install(op.get_bind())
