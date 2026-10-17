"""preparation is not a decision

The step between a frozen contract and a started job leaves two kinds of trace
and only one of them was a table: a run that starts writes an Execution Record,
and what RAVEL built for it before it started was nowhere. So a workspace
nothing can attest to, and — when the contract cannot be materialized at all —
a node parked at WAITING_DECISION with Master asked to decide and no record of
what was wrong with the contract it read.

This revision creates the table that holds both. One row per preparation: the
workspace, the manifest with the hashes of what was written, the checks that
were run, or the refusal and why. Written in the same transaction as the node's
move, so "Master is being asked" and "here is what could not be built" are
never two facts that can disagree.

Append-only, because what RAVEL built for a run is what makes a result
traceable to its inputs, and a row that could be rewritten afterwards would be
a trace that follows whatever was written last rather than what was run.
Absent from `UPDATABLE_TABLES`, so the append-only trigger applies and no
guard code changed.

**No unique constraint, unlike `run_reconciliations`.** A reconciliation is
written by a scan that will look at the same stranded run again, so PostgreSQL
has to deduplicate it. Preparation is written once per run by an activity that
precedes the job, and the rows a unique constraint would collapse are the
interesting ones: an attempt that refused and the one after it that prepared,
two preparations whose manifests differ because the host changed under them.
`latest_for_run` is how a reader finds the one that matters, and what the
history says is that there was more than one.

`execution_contract_ref` carries no foreign key, for the reason
`execution_records`' does not: the row quotes the contract version it read, and
the contract is not deleted out from under the record of what was built from
it.

Revision ID: e1b7c3f4a920
Revises: c8f2a5d10e47
Created: 2026-09-23 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e1b7c3f4a920"
down_revision: str | None = "c8f2a5d10e47"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the table RAVEL's prepared workspaces are recorded in."""
    op.create_table(
        "execution_preparations",
        sa.Column("preparation_id", sa.String(length=32), nullable=False),
        sa.Column("project_id", sa.String(length=32), nullable=False),
        sa.Column("node_id", sa.String(length=32), nullable=False),
        sa.Column("execution_contract_ref", sa.String(length=64), nullable=False),
        sa.Column("execution_contract_version", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=32), nullable=False),
        sa.Column("materializer", sa.String(length=64), nullable=False),
        sa.Column("materializer_version", sa.String(length=32), nullable=False),
        sa.Column("workspace_path", sa.Text(), nullable=False),
        sa.Column("required_outputs", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("manifest", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("checks", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("execution_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("refusal", sa.String(length=32), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('PREPARED', 'REFUSED')",
            name=op.f("ck_execution_preparations_outcome_is_known"),
        ),
        sa.CheckConstraint(
            "refusal IN ('MISSING_SCIENTIFIC_PARAMETER', 'INCONSISTENT_CONTRACT', "
            "'UNSUPPORTED_ENVIRONMENT', 'ENVIRONMENT_UNAVAILABLE')",
            name=op.f("ck_execution_preparations_refusal_is_known"),
        ),
        sa.CheckConstraint(
            "execution_contract_version >= 1",
            name=op.f("ck_execution_preparations_contract_version_is_positive"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_execution_preparations_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["node_id"],
            ["dag_nodes.node_id"],
            name=op.f("fk_execution_preparations_node_id_dag_nodes"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "preparation_id", name=op.f("pk_execution_preparations")
        ),
    )
    op.create_index(
        "ix_execution_preparations_node_created",
        "execution_preparations",
        ["node_id", "created_at"],
        unique=False,
    )

    _install_guards()


def _install_guards() -> None:
    """Attach the append-only trigger to the new table.

    Delegated rather than written out, for the reason every revision delegates
    it: the migration, the test schema, and `create_all` must install
    byte-identical rules, and a hand-written list here would be a second place
    for them to differ. `execution_preparations` is not in `UPDATABLE_TABLES`,
    so what this installs is the trigger that refuses `UPDATE` and `DELETE`,
    which is the property the revision is for rather than a side effect of it.
    """
    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Drop the table and the trigger that was attached to it."""
    op.execute("DROP TRIGGER IF EXISTS ravel_append_only ON execution_preparations")
    op.drop_index(
        "ix_execution_preparations_node_created", table_name="execution_preparations"
    )
    op.drop_table("execution_preparations")
