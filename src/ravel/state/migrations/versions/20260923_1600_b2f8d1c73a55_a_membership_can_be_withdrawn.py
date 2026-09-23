"""a membership can be withdrawn

`project_memberships` was append-only and unique per `(project, user)`, which
between them said that a user's role in a project is granted once and holds
forever. The spec's membership management — an owner adding a lab user, an
owner removing one — needs the other direction, and the two obvious ways to get
it are both wrong. Deleting the row would make a withdrawn membership
indistinguishable from one that never existed, which is the first question an
inquiry into a project's decisions asks. Editing `role` in place would let one
row stand for two different grants of authority, and the `MEMBER_ADDED` event
that recorded the first one would then describe something that is no longer
anywhere on the record.

So this revision adds the two columns that say a membership was withdrawn and
by whom, and replaces the uniqueness with a *partial* one:

    unique (project_id, user_id) where revoked_at is null

**That is the same rule the old constraint meant**, stated correctly: a user
holds at most one live role in a project at a time. The revoked rows behind the
live one are what the pair of columns makes possible, and the partial index is
what makes "at most one" survive them.

`project_memberships` joins `UPDATABLE_TABLES` with an identity trigger, so the
two new columns are the only ones an `UPDATE` may touch. `role` is in the
identity list: a role change is a revocation followed by a grant, which is a
sentence the stream can state and an edit is not.

The same reasoning is why the event vocabulary widens here. A withdrawal that
left no trace would be the edit this revision exists to prevent, so
`MEMBER_ADDED` and `MEMBER_REVOKED` join the closed list, and the constraint is
*replaced* rather than dropped and left off: the point of a closed vocabulary
is that the database refuses a value nobody has thought about, and a migration
that made room by removing the rule would turn the next typo into a row.

Revision ID: b2f8d1c73a55
Revises: a7c3e5b1f284
Created: 2026-09-23 16:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b2f8d1c73a55"
down_revision: str | None = "a7c3e5b1f284"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The vocabulary before and after this revision. Spelled out rather than
#: imported from `ravel.domain.events`, because a migration has to describe the
#: schema as it was at this revision: an enum that gains a twenty-sixth event
#: in a year would otherwise rewrite history and make this step unreplayable.
BEFORE = (
    "'PROJECT_CREATED', 'PROJECT_STATUS_CHANGED', 'MASTER_STARTED', "
    "'MASTER_CHECKPOINTED', 'MASTER_RECOVERED', 'NODE_CREATED', 'NODE_READY', "
    "'NODE_STARTED', 'NODE_WAITING', 'NODE_COMPLETED', 'NODE_FAILED', "
    "'NODE_CANCELLED', 'REVIEW_SUBMITTED', 'DECISION_CREATED', 'DAG_MUTATED', "
    "'DEVIATION_REPORTED', 'ARTIFACT_REGISTERED', 'EVIDENCE_REGISTERED', "
    "'APPROVAL_REQUESTED', 'APPROVAL_RESOLVED', 'BACKEND_STATUS_CHANGED', "
    "'AGENT_SESSION_STARTED', 'AGENT_SESSION_ENDED'"
)
AFTER = (
    "'PROJECT_CREATED', 'PROJECT_STATUS_CHANGED', 'MEMBER_ADDED', "
    "'MEMBER_REVOKED', 'MASTER_STARTED', 'MASTER_CHECKPOINTED', "
    "'MASTER_RECOVERED', 'NODE_CREATED', 'NODE_READY', 'NODE_STARTED', "
    "'NODE_WAITING', 'NODE_COMPLETED', 'NODE_FAILED', 'NODE_CANCELLED', "
    "'REVIEW_SUBMITTED', 'DECISION_CREATED', 'DAG_MUTATED', "
    "'DEVIATION_REPORTED', 'ARTIFACT_REGISTERED', 'EVIDENCE_REGISTERED', "
    "'APPROVAL_REQUESTED', 'APPROVAL_RESOLVED', 'BACKEND_STATUS_CHANGED', "
    "'AGENT_SESSION_STARTED', 'AGENT_SESSION_ENDED'"
)


def _events_constraint() -> str:
    """The vocabulary constraint as the database knows it.

    Composed in full and marked with `op.f`, as the initial migration does:
    `NAMING_CONVENTION` would otherwise prefix the already-composed name a
    second time. A function rather than a module constant because `op` is a
    proxy — calling it while the revisions are being loaded, before any
    migration is running, raises instead of returning a name.
    """
    return op.f("ck_project_events_event_type_is_known")


def upgrade() -> None:
    """Add the withdrawal columns and make uniqueness per live membership."""
    op.add_column(
        "project_memberships",
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "project_memberships",
        sa.Column("revoked_by", sa.String(length=32), nullable=True),
    )
    op.drop_constraint(
        "uq_project_memberships_one_role", "project_memberships", type_="unique"
    )
    op.create_index(
        "uq_project_memberships_one_active_role",
        "project_memberships",
        ["project_id", "user_id"],
        unique=True,
        postgresql_where=sa.text("revoked_at IS NULL"),
    )

    op.drop_constraint(_events_constraint(), "project_events", type_="check")
    op.create_check_constraint(
        _events_constraint(), "project_events", f"event_type IN ({AFTER})"
    )

    _install_guards()


def _install_guards() -> None:
    """Attach the guards the new columns warrant.

    Delegated rather than written out, for the reason every revision delegates
    it: the migration, the test schema and `create_all` must install
    byte-identical rules. `project_memberships` is now in `UPDATABLE_TABLES`,
    so it takes the no-delete trigger and the identity trigger — the pair that
    makes "a membership may be withdrawn and is never deleted" a property of
    the database rather than of the code that happens to write it today.
    """
    from ravel.state import guards

    guards.install(op.get_bind())


def downgrade() -> None:
    """Put the single-role constraint back and drop the withdrawal columns.

    The event vocabulary narrows last, and it is the step that can refuse. A
    project whose record holds a `MEMBER_ADDED` says something a rollback would
    have to delete, and there is no honest rewrite of it — the constraint is
    reinstated over the existing rows and the `ALTER` fails, naming them, if
    any of them hold one of the two values. An operator who genuinely wants to
    go back has to decide what those events were.
    """
    op.drop_constraint(_events_constraint(), "project_events", type_="check")
    op.create_check_constraint(
        _events_constraint(), "project_events", f"event_type IN ({BEFORE})"
    )

    op.execute("DROP TRIGGER IF EXISTS ravel_no_delete ON project_memberships")
    op.execute(
        "DROP TRIGGER IF EXISTS ravel_project_memberships_identity ON project_memberships"
    )
    op.drop_index(
        "uq_project_memberships_one_active_role", table_name="project_memberships"
    )
    # The old constraint cannot be restored over withdrawn rows: a user who was
    # granted twice would have two rows and the downgrade would fail on them.
    # Rather than delete history to make room, this says so.
    op.create_unique_constraint(
        "uq_project_memberships_one_role",
        "project_memberships",
        ["project_id", "user_id"],
    )
    op.drop_column("project_memberships", "revoked_by")
    op.drop_column("project_memberships", "revoked_at")
