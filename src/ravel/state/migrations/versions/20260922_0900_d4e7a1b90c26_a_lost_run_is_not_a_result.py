"""a lost run is not a result

Phase 11 opens with L-24, and L-24's shape is that a run can die without
reporting: the workflow exhausts the retries of its last activity, Temporal
marks it FAILED, and PostgreSQL is left holding a node in RUNNING. Nothing ends
it, the loop reads `in_flight`, and the project cannot reach an ending while it
holds one.

Recovery needs a record of the recovery. The reconciler ends the stranded job,
moves the node to WAITING_DECISION so that Master — the one role that may
change the plan — is asked, and writes what it saw here, in the same
transaction as both. Without a row, Master would be handed a node waiting on a
decision with nothing to decide about, and the stream would show a status
change with no reason beside it.

Append-only, because a reconciliation is a statement about what RAVEL observed
at a moment and a statement that can be edited afterwards is not evidence.
Absent from `UPDATABLE_TABLES`, so the append-only trigger applies and no guard
code changed.

`one_reconciliation_per_run` is what makes a second scan safe. The reconciler
runs on the supervisor's tick, so it *will* look at the same node again, and
the constraint means the second look finds the first record rather than writing
a second one. It is keyed by the contract version because a run is: a node
whose terms Master revised runs again under a new version, and losing that run
too is a second loss with its own row rather than a duplicate of the first.

Revision ID: d4e7a1b90c26
Revises: b1c4a7e2f903
Created: 2026-09-22 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e7a1b90c26"
down_revision: str | None = "b1c4a7e2f903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table RAVEL's run recoveries are written to."""
    op.create_table(
        "run_reconciliations",
        sa.Column("reconciliation_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("execution_contract_version", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(length=64), nullable=False),
        sa.Column("observed", sa.String(length=32), nullable=False),
        sa.Column("failure_class", sa.String(length=32), nullable=False),
        sa.Column("node_status_before", sa.String(length=32), nullable=False),
        sa.Column("node_status_after", sa.String(length=32), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("job_state_before", sa.String(length=32), nullable=True),
        sa.Column("job_state_after", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("detected_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "observed IN ('RUNNING', 'COMPLETED', 'FAILED', 'CANCELLED', "
            "'TERMINATED', 'TIMED_OUT', 'NOT_FOUND', 'UNKNOWN')",
            name=op.f("ck_run_reconciliations_observed_is_known"),
        ),
        sa.CheckConstraint(
            "failure_class IN ('WORKFLOW_LOST', 'INFRASTRUCTURE', "
            "'BACKEND_FAILURE', 'CANCELLED', 'SCIENTIFIC')",
            name=op.f("ck_run_reconciliations_failure_class_is_known"),
        ),
        sa.CheckConstraint(
            "node_status_before IN ('PLANNED', 'READY', 'RUNNING', "
            "'WAITING_EXTERNAL', 'WAITING_DECISION', 'REVIEWING', 'PASSED', "
            "'FAILED', 'PARTIAL', 'BLOCKED', 'CANCELLED')",
            name=op.f("ck_run_reconciliations_node_status_before_is_known"),
        ),
        sa.CheckConstraint(
            "node_status_after IN ('PLANNED', 'READY', 'RUNNING', "
            "'WAITING_EXTERNAL', 'WAITING_DECISION', 'REVIEWING', 'PASSED', "
            "'FAILED', 'PARTIAL', 'BLOCKED', 'CANCELLED')",
            name=op.f("ck_run_reconciliations_node_status_after_is_known"),
        ),
        sa.CheckConstraint(
            "job_state_before IN ('SUBMITTED', 'RUNNING', 'WAITING_EXTERNAL', "
            "'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')",
            name=op.f("ck_run_reconciliations_job_state_before_is_known"),
        ),
        sa.CheckConstraint(
            "job_state_after IN ('SUBMITTED', 'RUNNING', 'WAITING_EXTERNAL', "
            "'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')",
            name=op.f("ck_run_reconciliations_job_state_after_is_known"),
        ),
        sa.CheckConstraint(
            "execution_contract_version >= 1",
            name=op.f("ck_run_reconciliations_contract_version_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_run_reconciliations_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_run_reconciliations_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("reconciliation_id", name=op.f("pk_run_reconciliations")),
        sa.UniqueConstraint(
            "project_id",
            "node_id",
            "execution_contract_version",
            name=op.f("uq_run_reconciliations_one_reconciliation_per_run"),
        ),
    )
    op.create_index(
        "ix_run_reconciliations_node_created",
        "run_reconciliations",
        ["node_id", "created_at"],
        unique=False,
    )

    _install_guards()


def _install_guards() -> None:
    """Attach the append-only trigger to the new table.

    Delegated rather than written out, for the reason every revision delegates
    it: the migration, the test schema, and `create_all` must install
    byte-identical rules, and a hand-written list here would be a second place
    for them to differ. `run_reconciliations` is not in `UPDATABLE_TABLES`, so
    what this installs is the trigger that refuses `UPDATE` and `DELETE` —
    which is the property the revision is for rather than a side effect of it.
    """
    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Drop the table and the trigger that was attached to it."""
    op.execute("DROP TRIGGER IF EXISTS ravel_append_only ON run_reconciliations")
    op.drop_index(
        "ix_run_reconciliations_node_created", table_name="run_reconciliations"
    )
    op.drop_table("run_reconciliations")
