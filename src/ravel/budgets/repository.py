"""Budget account and reservation persistence.

Both repositories are project-scoped. An account's key is `(project, cluster)`,
and its row lock plus the advisory lock the service takes are what make two
concurrent reservations serialise instead of both reading the same remainder.
A reservation's key is the Action it belongs to: one approved Action cannot
consume two promises.
"""

from __future__ import annotations

from ravel.domain.actions import BudgetAccount, BudgetReservation
from ravel.state.mapping import build_row, from_row, to_row_data
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.tables import BudgetAccountRow, BudgetReservationRow

#: Account columns an operator configuration may change. The identity trigger
#: keeps who opened the account and when out of reach; the limit moves.
MUTABLE_ACCOUNT_COLUMNS = (
    "limit_cny",
    "paused_at",
    "pause_reason",
    "overrun_at",
)

#: Reservation columns the outcome may change. Estimates and upper bounds are
#: what the approval covered, so they are identity columns and absent here.
MUTABLE_RESERVATION_COLUMNS = (
    "status",
    "cost_known",
    "actual_cost_cny",
    "overrun_event_ref",
    "settled_at",
    "released_at",
)


class BudgetAccountRepository(ProjectScopedRepository[BudgetAccount]):
    """The explicit paid-budget configuration for one project's clusters."""

    row_type = BudgetAccountRow
    record_type = BudgetAccount

    def add(self, record: BudgetAccount) -> BudgetAccount:  # type: ignore[override]
        self._authorize(record)
        self.session.add(build_row(BudgetAccountRow, record))
        return record

    def for_cluster(self, cluster: str) -> BudgetAccount | None:
        """The account for this cluster, or `None` when none was configured."""
        return self._one(cluster=cluster)

    def lock_for_cluster(
        self, cluster: str
    ) -> tuple[BudgetAccount | None, BudgetAccountRow | None]:
        """Read the cluster's account with a row lock, if it exists."""
        row = (
            self.session.execute(
                self._scoped(BudgetAccountRow.cluster == cluster).with_for_update()
            )
            .scalars()
            .one_or_none()
        )
        return (from_row(BudgetAccount, row) if row is not None else None), row

    def save(
        self, record: BudgetAccount, row: BudgetAccountRow | None = None
    ) -> BudgetAccount:
        """Write a changed limit or pause back to the account row."""
        self._authorize(record)
        target = row
        if target is None:
            target = (
                self.session.execute(
                    self._scoped(BudgetAccountRow.cluster == record.cluster)
                )
                .scalars()
                .one_or_none()
            )
        if target is None:
            raise LookupError(
                f"no budget account for {self.project_id!r}/{record.cluster!r}"
            )
        data = to_row_data(record, BudgetAccountRow)
        for column in MUTABLE_ACCOUNT_COLUMNS:
            setattr(target, column, data[column])
        return record

    def all_accounts(self) -> list[BudgetAccount]:
        """Every configured cluster, alphabetically."""
        rows = (
            self.session.execute(
                self._scoped().order_by(BudgetAccountRow.cluster)
            )
            .scalars()
            .all()
        )
        return [from_row(BudgetAccount, row) for row in rows]


class BudgetReservationRepository(ProjectScopedRepository[BudgetReservation]):
    """Promises made before an external submission."""

    row_type = BudgetReservationRow
    record_type = BudgetReservation

    def add(self, record: BudgetReservation) -> BudgetReservation:  # type: ignore[override]
        self._authorize(record)
        self.session.add(build_row(BudgetReservationRow, record))
        return record

    def for_action(self, action_id: str) -> BudgetReservation | None:
        """The one reservation belonging to an Action, if it has one."""
        return self._one(action_id=action_id)

    def lock_for_action(self, action_id: str) -> BudgetReservation | None:
        """The Action's reservation, locked for settlement."""
        row = (
            self.session.execute(
                self._scoped(BudgetReservationRow.action_id == action_id).with_for_update()
            )
            .scalars()
            .one_or_none()
        )
        return from_row(BudgetReservation, row) if row is not None else None

    def save(
        self,
        record: BudgetReservation,
        row: BudgetReservationRow | None = None,
    ) -> BudgetReservation:
        """Write the outcome half of a reservation."""
        self._authorize(record)
        target = row
        if target is None:
            target = (
                self.session.execute(
                    self._scoped(
                        BudgetReservationRow.reservation_id == record.reservation_id
                    )
                )
                .scalars()
                .one_or_none()
            )
        if target is None:
            raise LookupError(
                f"no reservation {record.reservation_id!r} in {self.project_id!r}"
            )
        data = to_row_data(record, BudgetReservationRow)
        for column in MUTABLE_RESERVATION_COLUMNS:
            setattr(target, column, data[column])
        return record

    def for_cluster(self, cluster: str) -> list[BudgetReservation]:
        """Every reservation on this cluster, for a committed-total read.

        No row lock: the account decision is serialised by the service's
        advisory lock, and a read that locked every reservation would make two
        unrelated clusters on one project wait for each other.
        """
        rows = (
            self.session.execute(
                self._scoped(BudgetReservationRow.cluster == cluster).order_by(
                    BudgetReservationRow.reserved_at
                )
            )
            .scalars()
            .all()
        )
        return [from_row(BudgetReservation, row) for row in rows]

    def open_for_cluster(self, cluster: str) -> list[BudgetReservation]:
        """Reservations that still hold budget, locked for an outcome sweep."""
        rows = (
            self.session.execute(
                self._scoped(
                    BudgetReservationRow.cluster == cluster,
                    BudgetReservationRow.status.in_(["RESERVED", "UNKNOWN"]),
                ).with_for_update()
            )
            .scalars()
            .all()
        )
        return [from_row(BudgetReservation, row) for row in rows]
