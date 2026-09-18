"""backend jobs, the in-flight record of one attempt

A job handed to a compute or experiment backend has to be a fact in PostgreSQL
before the backend is asked to do anything. A worker that dies between "the
backend accepted it" and "the result was recorded" is retried by Temporal, and
the retry has to find the job rather than start a second one — which is what
`one_job_per_attempt` makes true, since the uniqueness is decided by the
database rather than by the activity checking first.

The uniqueness constraint is what carries the guarantee. The identity trigger
installed at the end of `upgrade` is what keeps it meaningful: `attempt` and
`execution_contract_ref` are what a job is, so a row whose attempt could be
rewritten would let one job stand in for another.

Revision ID: 603e26d7e0c4
Revises: d3e067ead13a
Created: 2026-09-19 07:28:22.471208
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "603e26d7e0c4"
down_revision: str | None = "d3e067ead13a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "backend_jobs",
        sa.Column("job_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("execution_contract_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_contract_version", sa.Integer(), nullable=False),
        sa.Column("backend", sa.String(length=64), nullable=False),
        sa.Column("backend_job_ref", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("backend_state", sa.String(length=64), nullable=False),
        sa.Column("failure_class", sa.String(length=32), nullable=True),
        sa.Column("detail", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(state IN ('COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT')) = "
            "(ended_at IS NOT NULL)",
            name=op.f("ck_backend_jobs_ending_is_all_or_nothing"),
        ),
        sa.CheckConstraint(
            "failure_class IN ('INFRA_RETRYABLE', 'NON_RETRYABLE')",
            name=op.f("ck_backend_jobs_failure_class_is_known"),
        ),
        sa.CheckConstraint(
            "failure_class IS NULL OR state IN ('FAILED', 'TIMED_OUT')",
            name=op.f("ck_backend_jobs_failure_class_only_on_failure"),
        ),
        sa.CheckConstraint(
            "state IN ('SUBMITTED', 'RUNNING', 'WAITING_EXTERNAL', 'COMPLETED', "
            "'FAILED', 'CANCELLED', 'TIMED_OUT')",
            name=op.f("ck_backend_jobs_state_is_known"),
        ),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_backend_jobs_attempt_is_positive")),
        sa.CheckConstraint(
            "execution_contract_version >= 1",
            name=op.f("ck_backend_jobs_contract_version_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_backend_jobs_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_backend_jobs_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("job_id", name=op.f("pk_backend_jobs")),
        sa.UniqueConstraint(
            "project_id", "node_id", "attempt", name="one_job_per_attempt"
        ),
    )
    op.create_index(
        "ix_backend_jobs_node_state", "backend_jobs", ["node_id", "state"], unique=False
    )

    _install_guards()


def _install_guards() -> None:
    """Attach the guards, which now include the identity trigger this table needs.

    `install` is idempotent and covers every table in the metadata, so running
    it here does more than add one trigger: it is also what removes the
    node-specific identity function that `ravel_protect_identity` replaced.

    Delegated rather than written out, for the same reason the initial revision
    delegates it: the migration, the test schema, and `create_all` must install
    byte-identical rules, and a hand-written list here would be a second place
    for them to differ.
    """
    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Remove the table and the trigger that was attached to it.

    The trigger goes with its table. The guard *function* stays, because the
    DAG node's identity trigger still uses it — dropping it here would take out
    a rule this revision did not add.
    """
    op.execute("DROP TRIGGER IF EXISTS ravel_backend_jobs_identity ON backend_jobs")
    op.drop_index("ix_backend_jobs_node_state", table_name="backend_jobs")
    op.drop_table("backend_jobs")
