"""actions, approvals bound to them, and resource budgets

An Action is the only record through which RAVEL may cause an external effect:
a Slurm submission, a mail send, a lab handover, a sandbox command. Its content
hash, target, contract, actor, envelope version, expiry and budget estimate are
fixed at creation, because a process that could rewrite them after a human
approved one thing could submit another. The transition trigger and the
identity trigger are installed here, next to the tables they guard.

Budget rows are deliberately separate from the Action. An account is an
Owner's explicit spending configuration for one cluster — absence means zero,
never unlimited — and a reservation is the promise made before a submission.
`UNKNOWN` is a real reservation state: an external result nobody can classify
keeps its money held until reconciliation, rather than settling at a
convenient zero.

Revision ID: d1b8e4f6a2c9
Revises: c3a9f1d2e4b7
Created: 2026-10-17 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from ravel.state import guards

revision: str = "d1b8e4f6a2c9"
down_revision: str | None = "c3a9f1d2e4b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "actions",
        sa.Column("action_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("payload_hash", sa.String(71), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("payload_artifact_ref", sa.String(32), nullable=True),
        sa.Column("contract_id", sa.String(32), nullable=True),
        sa.Column("contract_version", sa.Integer(), nullable=True),
        sa.Column("node_id", sa.String(32), nullable=True),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("requesting_binding", sa.String(32), nullable=False),
        sa.Column("target_ref", sa.String(256), nullable=True),
        sa.Column("required_approval_ref", sa.String(32), nullable=True),
        sa.Column("envelope_version", sa.Integer(), nullable=False),
        sa.Column("budget_reservation_ref", sa.String(32), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempts",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("cluster", sa.String(64), nullable=False),
        sa.Column("prior_action_id", sa.String(32), nullable=True),
        sa.Column(
            "resource_request",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("estimated_cny", sa.String(64), nullable=True),
        sa.Column("cost_known", sa.Boolean(), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("claimed_by", sa.String(128), nullable=True),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_ref", sa.String(256), nullable=True),
        sa.Column("external_state", sa.String(64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("uncertainty", sa.Text(), nullable=True),
        sa.Column("overrun_event_ref", sa.String(32), nullable=True),
        sa.CheckConstraint(
            "kind IN ('COMPUTE_SUBMIT', 'MAIL_SEND', 'LAB_HANDOVER', 'SANDBOX_EXEC')",
            name=op.f("ck_actions_kind_is_known"),
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'AWAITING_APPROVAL', 'READY', 'CLAIMED', "
            "'ACCEPTED', 'COMPLETED', 'UNKNOWN', 'FAILED_CONFIRMED', "
            "'REJECTED', 'EXPIRED', 'CANCELLED')",
            name=op.f("ck_actions_status_is_known"),
        ),
        sa.CheckConstraint(
            "payload_hash ~ '^sha256:[0-9a-f]{64}$'",
            name=op.f("ck_actions_payload_hash_is_sha256"),
        ),
        sa.CheckConstraint(
            "contract_version IS NULL OR contract_version >= 1",
            name=op.f("ck_actions_contract_version_positive_when_present"),
        ),
        sa.CheckConstraint(
            "envelope_version >= 1", name=op.f("ck_actions_envelope_version_positive")
        ),
        sa.CheckConstraint(
            "estimated_cny IS NULL OR estimated_cny ~ '^[0-9]+(\\.[0-9]+)?$'",
            name=op.f("ck_actions_estimated_cny_is_fixed_point"),
        ),
        sa.CheckConstraint(
            "expires_at > created_at", name=op.f("ck_actions_expiry_follows_creation")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_actions_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("action_id", name=op.f("pk_actions")),
        sa.UniqueConstraint(
            "project_id", "idempotency_key", name=op.f("one_action_per_idempotency_key")
        ),
    )
    op.create_index(
        "ix_actions_project_status", "actions", ["project_id", "status"]
    )
    op.create_index(
        "ix_actions_project_kind_status", "actions", ["project_id", "kind", "status"]
    )
    op.create_index("ix_actions_expires_at", "actions", ["expires_at"])
    op.create_index(
        "one_live_compute_per_submission_key",
        "actions",
        ["project_id", sa.text("(payload->>'submission_key')")],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'COMPUTE_SUBMIT' AND status NOT IN "
            "('FAILED_CONFIRMED', 'REJECTED', 'EXPIRED', 'CANCELLED')"
        ),
    )
    op.create_index(
        "one_live_mail_per_message_id",
        "actions",
        ["project_id", sa.text("(payload->>'message_id')")],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'MAIL_SEND' AND status NOT IN "
            "('FAILED_CONFIRMED', 'REJECTED', 'EXPIRED', 'CANCELLED')"
        ),
    )

    op.create_table(
        "budget_accounts",
        sa.Column("account_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("cluster", sa.String(64), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("limit_cny", sa.String(64), nullable=False),
        sa.Column("configured_by", sa.String(128), nullable=False),
        sa.Column("configured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pause_reason", sa.Text(), nullable=True),
        sa.Column("overrun_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "currency = 'CNY'", name=op.f("ck_budget_accounts_currency_is_cny")
        ),
        sa.CheckConstraint(
            "limit_cny ~ '^[0-9]+(\\.[0-9]+)?$'",
            name=op.f("ck_budget_accounts_limit_is_fixed_point"),
        ),
        sa.CheckConstraint(
            "(paused_at IS NULL) = (pause_reason IS NULL)",
            name=op.f("ck_budget_accounts_pause_has_a_reason"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_budget_accounts_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("account_id", name=op.f("pk_budget_accounts")),
        sa.UniqueConstraint(
            "project_id", "cluster", name=op.f("one_account_per_project_cluster")
        ),
    )
    op.create_index("ix_budget_accounts_project", "budget_accounts", ["project_id"])

    op.create_table(
        "budget_reservations",
        sa.Column("reservation_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("action_id", sa.String(32), nullable=False),
        sa.Column("cluster", sa.String(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("cost_known", sa.Boolean(), nullable=False),
        sa.Column("estimated_cost_cny", sa.String(64), nullable=True),
        sa.Column("cost_upper_bound_cny", sa.String(64), nullable=True),
        sa.Column("actual_cost_cny", sa.String(64), nullable=True),
        sa.Column("resource_hours", sa.String(64), nullable=True),
        sa.Column("resource_hours_upper_bound", sa.String(64), nullable=True),
        sa.Column("overrun_event_ref", sa.String(32), nullable=True),
        sa.Column("reserved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('RESERVED', 'UNKNOWN', 'SETTLED', 'RELEASED')",
            name=op.f("ck_budget_reservations_status_is_known"),
        ),
        sa.CheckConstraint(
            "(status = 'SETTLED') = (settled_at IS NOT NULL)",
            name=op.f("ck_budget_reservations_settled_timestamp_matches_status"),
        ),
        sa.CheckConstraint(
            "status <> 'SETTLED' OR actual_cost_cny IS NOT NULL",
            name=op.f("ck_budget_reservations_settled_has_actual_cost"),
        ),
        sa.CheckConstraint(
            "(status = 'RELEASED') = (released_at IS NOT NULL)",
            name=op.f("ck_budget_reservations_released_timestamp_matches_status"),
        ),
        sa.CheckConstraint(
            "estimated_cost_cny IS NULL OR estimated_cost_cny ~ '^[0-9]+(\\.[0-9]+)?$'",
            name=op.f("ck_budget_reservations_estimated_cost_is_fixed_point"),
        ),
        sa.CheckConstraint(
            "actual_cost_cny IS NULL OR actual_cost_cny ~ '^[0-9]+(\\.[0-9]+)?$'",
            name=op.f("ck_budget_reservations_actual_cost_is_fixed_point"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name=op.f("fk_budget_reservations_project_id_projects"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["action_id"],
            ["actions.action_id"],
            name=op.f("fk_budget_reservations_action_id_actions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("reservation_id", name=op.f("pk_budget_reservations")),
        sa.UniqueConstraint("action_id", name=op.f("one_reservation_per_action")),
    )
    op.create_index(
        "ix_budget_reservations_project_status",
        "budget_reservations",
        ["project_id", "status"],
    )
    op.create_index(
        "ix_budget_reservations_project_cluster",
        "budget_reservations",
        ["project_id", "cluster"],
    )

    guards.install(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "ix_budget_reservations_project_cluster",
        table_name=op.f("budget_reservations"),
    )
    op.drop_index(
        "ix_budget_reservations_project_status", table_name=op.f("budget_reservations")
    )
    op.drop_table("budget_reservations")
    op.drop_index("ix_budget_accounts_project", table_name=op.f("budget_accounts"))
    op.drop_table("budget_accounts")
    op.drop_index("one_live_mail_per_message_id", table_name=op.f("actions"))
    op.drop_index("one_live_compute_per_submission_key", table_name=op.f("actions"))
    op.drop_index("ix_actions_expires_at", table_name=op.f("actions"))
    op.drop_index("ix_actions_project_kind_status", table_name=op.f("actions"))
    op.drop_index("ix_actions_project_status", table_name=op.f("actions"))
    op.drop_table("actions")
