"""retry episodes: a persisted budget for unanswered model turns

A project loop's question budget used to live in a Python dictionary. A
supervisor restart rebuilt the loop and the budget with it, so a question that
had already been asked nine times could be asked nine more after every deploy —
the failure mode was a model bill rather than a stall, and nothing in the
database recorded that it had happened.

This table is the budget. One row per *(project, question fingerprint,
scientific state version)*: the attempt and round counters, the cooldown a
round boundary imposes, the escalation that the ninth attempt raises, and the
operator who can retire the episode for a fresh one. `status` is automation
state, never a project status, because a runtime stall is not a scientific
ending.

Revision ID: c3a9f1d2e4b7
Revises: b7e4c2a1d9f3
Created: 2026-10-17 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from ravel.state import guards

revision: str = "c3a9f1d2e4b7"
down_revision: str | None = "b7e4c2a1d9f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "retry_episodes",
        sa.Column("episode_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("question", sa.String(16), nullable=False),
        sa.Column("problem_key", sa.Text(), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("state_version", sa.String(64), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("round_number", sa.Integer(), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("escalation_event_id", sa.String(32), nullable=True),
        sa.Column("operator_actor", sa.String(64), nullable=True),
        sa.Column("operator_retried_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retired_reason", sa.String(48), nullable=True),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "question IN ('decision', 'pre-flight', 'verdict', 'ending')",
            name=op.f("ck_retry_episodes_question_is_known"),
        ),
        sa.CheckConstraint(
            "status IN ('ACTIVE', 'COOLDOWN', 'AWAITING_OPERATOR', 'RETIRED')",
            name=op.f("ck_retry_episodes_status_is_known"),
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= 9",
            name=op.f("ck_retry_episodes_attempts_within_budget"),
        ),
        sa.CheckConstraint(
            "round_number >= 1 AND round_number <= 3",
            name=op.f("ck_retry_episodes_round_within_budget"),
        ),
        sa.CheckConstraint(
            "(status = 'COOLDOWN') = (cooldown_until IS NOT NULL)",
            name=op.f("ck_retry_episodes_cooldown_iff_cooldown_status"),
        ),
        sa.CheckConstraint(
            "status <> 'RETIRED' OR retired_at IS NOT NULL",
            name=op.f("ck_retry_episodes_retirement_has_a_moment"),
        ),
        sa.CheckConstraint(
            "status <> 'AWAITING_OPERATOR' OR escalated_at IS NOT NULL",
            name=op.f("ck_retry_episodes_escalation_has_a_moment"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.project_id"],
            name="fk_retry_episodes_project_id_projects",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("episode_id", name="pk_retry_episodes"),
    )
    op.create_index(
        "uq_retry_episodes_open",
        "retry_episodes",
        ["project_id", "fingerprint", "state_version"],
        unique=True,
        postgresql_where=sa.text(
            "status IN ('ACTIVE', 'COOLDOWN', 'AWAITING_OPERATOR')"
        ),
    )
    op.create_index(
        "ix_retry_episodes_project_status",
        "retry_episodes",
        ["project_id", "status"],
        unique=False,
    )
    # The table is mutable (attempts advance, cooldowns clear) but never
    # deleted, and its identity columns are fixed. Delegated to the same guard
    # installer the metadata's `after_create` uses, so a migrated schema and a
    # test schema enforce one rule rather than two that resemble each other.
    guards.install(op.get_bind())


def downgrade() -> None:
    op.drop_index(
        "ix_retry_episodes_project_status", table_name="retry_episodes"
    )
    op.drop_index("uq_retry_episodes_open", table_name="retry_episodes")
    op.drop_table("retry_episodes")
