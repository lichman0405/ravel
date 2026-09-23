"""a bench keeps the terms it was given

Phase 11 gave RAVEL a real compute backend, and the shape of that backend's
durable record is a file: `SlurmComputeBackend` uploads `submission.json` next
to the work, and reads it back to answer "which project does this job belong
to" after a restart. A laboratory has nowhere to upload a note to. The work is
done by a person, the port hands the backend a reference and nothing else, and
the four tables that between them describe the run — the job row, the frozen
contract, the prepared package, the uploaded artifacts — are all free to move
while the bench works: a contract revised mid-experiment would silently change
what the run owed.

This revision creates the table that holds the handover. One row per piece of
work handed to a bench: the node and attempt it is for, the contract version it
was handed under, the prepared package the bench was given, and the outputs
that package owed. `required_outputs` is a copy of the contract's list and the
copy is the point — it is what a delivery is checked against, and a check whose
checklist can be edited afterwards is not a check.

**Mutable, and guarded, like `backend_jobs`.** A handover moves from
`WAITING_EXTERNAL` to one of four endings while the bench works, so the table
is in `UPDATABLE_TABLES` and takes a `ravel_protect_identity` trigger rather
than the append-only one. Everything describing what was handed over —
including `required_outputs` and the package — is in the identity list and
cannot be rewritten in place.

**`uq_lab_handovers_attempt` is what makes handing work over idempotent**, which
is the port's requirement of every backend: a worker killed between the backend
accepting work and the job reference being recorded is retried, and the retry
must find the handover it already made rather than hand the same experiment to
the same bench twice.

The state constraint is narrower than `JobState`, and derived from the domain's
own `HANDOVER_STATES` rather than written out here: a handover is never
`SUBMITTED` and never `RUNNING`, because a person working at a bench is not
something RAVEL can see, and a `RUNNING` that meant "somebody is probably at
the bench" is the long-running lie the state exists to avoid.

Revision ID: a7c3e5b1f284
Revises: e1b7c3f4a920
Created: 2026-09-23 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ravel.domain.lab import HANDOVER_STATES

revision: str = "a7c3e5b1f284"
down_revision: str | None = "e1b7c3f4a920"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The states a handover may be in, as SQL literals. Read from the domain so
#: the constraint this revision writes and the one the models declare are the
#: same list rather than two lists that happen to agree today.
_STATES = ", ".join(f"'{state.value}'" for state in HANDOVER_STATES)


def upgrade() -> None:
    """Create the table a laboratory handover is recorded in."""
    op.create_table(
        "lab_handovers",
        sa.Column("handover_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("backend", sa.String(length=64), nullable=False),
        sa.Column("execution_contract_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_contract_version", sa.Integer(), nullable=False),
        sa.Column("preparation_id", sa.String(length=64), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("protocol", sa.Text(), nullable=False),
        sa.Column("required_outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("handed_over_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            f"state IN ({_STATES})",
            name=op.f("ck_lab_handovers_state_is_a_handover_state"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_lab_handovers_attempt_is_positive")),
        sa.CheckConstraint(
            "execution_contract_version >= 1",
            name=op.f("ck_lab_handovers_contract_version_is_positive"),
        ),
        sa.CheckConstraint(
            "(state = 'WAITING_EXTERNAL') = (closed_at IS NULL)",
            name=op.f("ck_lab_handovers_closing_is_all_or_nothing"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_lab_handovers_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_lab_handovers_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("handover_id", name=op.f("pk_lab_handovers")),
        sa.UniqueConstraint(
            "project_id",
            "node_id",
            "execution_contract_version",
            "attempt",
            name="uq_lab_handovers_attempt",
        ),
    )
    op.create_index(
        "ix_lab_handovers_project_node",
        "lab_handovers",
        ["project_id", "node_id"],
        unique=False,
    )

    _install_guards()


def _install_guards() -> None:
    """Attach the guards the new table warrants.

    Delegated rather than written out, for the reason every revision delegates
    it: the migration, the test schema and `create_all` must install
    byte-identical rules. `lab_handovers` is in `UPDATABLE_TABLES`, so it gets
    the no-delete trigger and the identity trigger, and the two together are
    what make "the bench's terms are fixed but its state moves" a property of
    the database rather than of the code that happens to write it today.
    """
    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Drop the table and the triggers that were attached to it."""
    op.execute("DROP TRIGGER IF EXISTS ravel_no_delete ON lab_handovers")
    op.execute("DROP TRIGGER IF EXISTS ravel_lab_handovers_identity ON lab_handovers")
    op.drop_index("ix_lab_handovers_project_node", table_name="lab_handovers")
    op.drop_table("lab_handovers")
