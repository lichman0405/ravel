"""Action persistence, scoped to one project.

The repository is deliberately thin: the lifecycle belongs to the service, and
the invariants that must survive any caller — immutable approved content,
legal status transitions, one live submission per external idempotency key —
belong to PostgreSQL triggers and indexes. What remains here is the one rule
every repository in RAVEL enforces: a statement is filtered by the project the
repository was constructed with, so a model-supplied identifier cannot reach a
row in another project.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select

from ravel.domain.actions import Action
from ravel.domain.enums import ActionKind, ActionStatus
from ravel.state.mapping import build_row, from_row, to_row_data
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.tables import ActionRow

#: The columns a lifecycle transition may change. Everything else is approved
#: content and is protected by the `ravel_actions_identity` trigger; naming the
#: mutable set here means a bug that tried to write a frozen column would be a
#: missing attribute rather than a trigger exception after the external effect.
MUTABLE_ACTION_COLUMNS = (
    "status",
    "budget_reservation_ref",
    "attempts",
    "claimed_by",
    "claimed_at",
    "accepted_at",
    "completed_at",
    "external_ref",
    "external_state",
    "failure_reason",
    "uncertainty",
    "overrun_event_ref",
)


class ActionRepository(ProjectScopedRepository[Action]):
    """Actions in one project, with the locks the lifecycle needs."""

    row_type = ActionRow
    record_type = Action

    def add(self, record: Action) -> Action:  # type: ignore[override]
        self._authorize(record)
        self.session.add(build_row(ActionRow, record))
        return record

    def find_by_idempotency_key(self, key: str) -> Action | None:
        """The Action a repeated create should resolve to, if any."""
        return self._one(idempotency_key=key)

    def get_with_row(self, action_id: str) -> tuple[Action, ActionRow]:
        """Read one Action and its row without taking a lock."""
        row = (
            self.session.execute(self._scoped(ActionRow.action_id == action_id))
            .scalars()
            .one_or_none()
        )
        if row is None:
            raise NotFound(
                f"no Action {action_id!r} in project {self.project_id!r}"
            )
        return from_row(Action, row), row

    def lock(self, action_id: str) -> tuple[Action, ActionRow]:
        """Read one Action with a row lock, for a compare-and-set transition.

        Locking is what makes the claim a compare-and-set rather than a read
        followed by a write: two workers racing to execute one approved Action
        queue here, and the second sees the status the first committed.
        """
        row = (
            self.session.execute(
                self._scoped(ActionRow.action_id == action_id).with_for_update()
            )
            .scalars()
            .one_or_none()
        )
        if row is None:
            raise NotFound(
                f"no Action {action_id!r} in project {self.project_id!r}"
            )
        return from_row(Action, row), row

    def find_by_idempotency_key_for_update(self, key: str) -> tuple[Action, ActionRow] | None:
        """A repeated create under the same key, locked against a second writer."""
        row = (
            self.session.execute(
                self._scoped(ActionRow.idempotency_key == key).with_for_update()
            )
            .scalars()
            .one_or_none()
        )
        return (from_row(Action, row), row) if row is not None else None

    def save(self, record: Action, row: ActionRow | None = None) -> Action:
        """Write the mutable half of an Action back to its row.

        The mutable columns are read from the validated record, so a service
        cannot accidentally persist a field the domain refused. The row is
        located under the project scope even when the caller already holds it.
        """
        self._authorize(record)
        target = row
        if target is None:
            _, target = self.get_with_row(record.action_id)
        data = to_row_data(record, ActionRow)
        for column in MUTABLE_ACTION_COLUMNS:
            setattr(target, column, data[column])
        return record

    def pending_expiry(self, *, at: Any) -> list[Action]:
        """Actions whose approval window has closed and which may still move."""
        rows = (
            self.session.execute(
                self._scoped(
                    ActionRow.status.in_(
                        [
                            ActionStatus.DRAFT.value,
                            ActionStatus.AWAITING_APPROVAL.value,
                            ActionStatus.READY.value,
                        ]
                    ),
                    ActionRow.expires_at <= at,
                ).order_by(ActionRow.expires_at)
            )
            .scalars()
            .all()
        )
        return [from_row(Action, row) for row in rows]

    def list_actions(
        self,
        *,
        status: ActionStatus | None = None,
        kind: ActionKind | None = None,
        limit: int = 200,
    ) -> list[Action]:
        """Newest first, optionally narrowed by status or kind."""
        conditions = []
        if status is not None:
            conditions.append(ActionRow.status == status.value)
        if kind is not None:
            conditions.append(ActionRow.kind == kind.value)
        rows = (
            self.session.execute(
                self._scoped(*conditions)
                .order_by(ActionRow.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )
        return [from_row(Action, row) for row in rows]

    def live_for_node(self, node_id: str) -> Action | None:
        """The Action holding this node's execution slot, if one exists.

        A finished run can leave an `ACCEPTED` or `UNKNOWN` Action behind:
        acceptance records that the external system took the work, not that
        the work ended. The newest live Action is the one the run belongs to;
        an older Action for the same node is already terminal and has released
        its slot.
        """
        rows = (
            self.session.execute(
                self._scoped(
                    ActionRow.node_id == node_id,
                    ActionRow.status.in_(
                        (
                            ActionStatus.CLAIMED.value,
                            ActionStatus.ACCEPTED.value,
                            ActionStatus.UNKNOWN.value,
                        )
                    ),
                )
                .order_by(ActionRow.created_at.desc())
                .limit(1)
            )
            .scalars()
            .all()
        )
        return from_row(Action, rows[0]) if rows else None

    def held_for_concurrency(self, *, kind: ActionKind) -> int:
        """How many Actions of this kind currently hold a concurrency slot.

        A slot is held from the moment budget is reserved (`READY`) until the
        action reaches a terminal state. `UNKNOWN` deliberately still counts:
        an external system may be running work nobody has accounted for yet,
        and treating it as finished would let a duplicate start past the cap.
        """
        from ravel.domain.state_machines import BUDGET_HOLDING_ACTION_STATUSES

        return int(
            self.session.execute(
                select(func.count())
                .select_from(ActionRow)
                .where(
                    ActionRow.project_id == self.project_id,
                    ActionRow.kind == kind.value,
                    ActionRow.status.in_(
                        [status.value for status in BUDGET_HOLDING_ACTION_STATUSES]
                    ),
                )
            ).scalar_one()
        )
