"""The budget gate an external submission passes through.

Every method takes a session and is therefore part of the caller's transaction.
That is what makes "verify the budget, then claim the Action" atomic: if the
claim fails, the account read and the reservation write roll back with it.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ravel.budgets.repository import (
    BudgetAccountRepository,
    BudgetReservationRepository,
)
from ravel.config import Settings, get_settings
from ravel.domain.actions import (
    Action,
    ActionConflict,
    BudgetAccount,
    BudgetExceeded,
    BudgetReservation,
    cny,
    format_cny,
    parse_cny,
)
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    ActionKind,
    BudgetReservationStatus,
    UserRole,
)
from ravel.domain.ids import new_id
from ravel.domain.state_machines import BUDGET_HOLDING_ACTION_STATUSES
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.tables import ActionRow

_ZERO = Decimal("0")


class BudgetService:
    """Accounts and reservations for one project.

    The deployment-wide compute ceiling is deliberately read across projects.
    It is the one query in this class that is not project-scoped, and it has to
    be: a per-project repository cannot answer "how many jobs is the deployment
    running". It counts Action rows and reads no record content, so it is a
    capacity fact rather than a cross-project read of somebody's work.
    """

    def __init__(
        self,
        session: Session,
        project_id: str,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.project_id = project_id
        self.settings = settings or get_settings()
        self.accounts = BudgetAccountRepository(session, project_id)
        self.reservations = BudgetReservationRepository(session, project_id)

    # ── Operator configuration ──────────────────────────────────────────────

    def account(self, cluster: str = "default") -> BudgetAccount | None:
        """The account configured for one cluster, if any."""
        return self.accounts.for_cluster(cluster)

    def accounts_all(self) -> list[BudgetAccount]:
        """Every configured cluster, alphabetically."""
        return self.accounts.all_accounts()

    def configure(
        self,
        *,
        limit_cny: str,
        configured_by: str,
        cluster: str = "default",
        resume: bool = False,
    ) -> BudgetAccount:
        """Set the paid limit for one project cluster.

        Only a live `PROJECT_OWNER` membership may do this. Admin is not
        enough, and that is the task's rule rather than an omission: the
        authority to spend the project's money is part of directing it, and an
        administrator holds operational authority without holding that.

        A paused account stays paused unless `resume` is true. Raising a limit
        after an overrun is not by itself a statement that the cause was
        understood, so the two acts are separate.

        Raises:
            PermissionError: The caller is not an active owner of this project.
            BudgetExceeded: The limit is not a fixed-point non-negative CNY
                amount.
        """
        membership = MembershipRepository(self.session, self.project_id).for_user(
            configured_by
        )
        if (
            membership is None
            or membership.role is not UserRole.PROJECT_OWNER
            or not membership.is_active
        ):
            raise PermissionError(
                f"{configured_by!r} may not configure a budget in "
                f"{self.project_id}; the paid limit is set by a live "
                "PROJECT_OWNER"
            )
        if not UserRepository(self.session).get(configured_by).is_active:
            raise PermissionError(f"account {configured_by!r} is deactivated")

        limit = cny(limit_cny, field="budget limit")
        assert limit is not None
        account, row = self.accounts.lock_for_cluster(cluster)
        now = utcnow()
        if account is None:
            created = BudgetAccount(
                project_id=self.project_id,
                cluster=cluster,
                limit_cny=limit,
                configured_by=configured_by,
                configured_at=now,
                created_at=now,
            )
            self.accounts.add(created)
            self.session.flush()
            return created

        updates: dict[str, object] = {"limit_cny": limit}
        if resume:
            updates.update(
                {"paused_at": None, "pause_reason": None, "overrun_at": None}
            )
        changed = account.model_copy(update=updates)
        self.accounts.save(changed, row)
        return changed

    def pause(self, *, reason: str, cluster: str = "default") -> BudgetAccount | None:
        """Stop new submissions to one cluster.

        Called by an overrun and by an operator. It does not cancel a
        reservation already made: the external effect may be in flight, and a
        pause that promised money and then took it back would leave the site
        unpaid for work it is already doing.
        """
        account, row = self.accounts.lock_for_cluster(cluster)
        if account is None:
            return None
        if account.is_paused:
            return account
        now = utcnow()
        paused = account.model_copy(
            update={"paused_at": now, "pause_reason": reason or "paused"}
        )
        self.accounts.save(paused, row)
        return paused

    # ── Reservation ─────────────────────────────────────────────────────────

    def reserve(self, action: Action) -> BudgetReservation:
        """Promise this Action's estimate before anything is submitted.

        Serialisation is by transaction-scoped advisory lock, taken before the
        account row is read. A row lock alone would not help the first time a
        project spends: there is no account row to lock, and the correct
        behaviour for two simultaneous first reservations is still "one of
        them fits and the other waits to find out".

        Raises:
            BudgetExceeded: The account is paused, the projection exceeds the
                limit, there is no configured limit for a positive or unknown
                cost, or a compute concurrency slot is full.
        """
        existing = self.reservations.for_action(action.action_id)
        if existing is not None:
            return existing

        self._advisory_lock(f"ravel-budget:{self.project_id}:{action.cluster}")
        if action.kind is ActionKind.COMPUTE_SUBMIT:
            self._advisory_lock("ravel-compute-global")
            self._advisory_lock(f"ravel-compute:{self.project_id}")
            self._check_compute_slots()

        account, _row = self.accounts.lock_for_cluster(action.cluster)
        if account is not None and account.is_paused:
            raise BudgetExceeded(
                f"cluster {action.cluster!r} is paused: "
                f"{account.pause_reason or 'no reason recorded'}"
            )

        limit = account.limit if account is not None else _ZERO
        committed = self.committed(action.cluster)
        estimate = parse_cny(action.estimated_cny, field="estimated cost") or _ZERO

        if not action.cost_known:
            if account is None or limit == _ZERO:
                raise BudgetExceeded(
                    "this Action's cost is unknown and the project has configured "
                    "no paid budget; unknown never means free, so it cannot be "
                    "promised against a zero limit"
                )
            if estimate <= _ZERO:
                raise BudgetExceeded(
                    "this Action's cost is unknown and no positive upper bound was "
                    "provided; unknown cannot be reserved as zero"
                )
        projected = committed + estimate
        if projected > limit:
            raise BudgetExceeded(
                f"reserving {format_cny(estimate)} CNY would commit "
                f"{format_cny(projected)} against a limit of {format_cny(limit)} "
                f"on {action.cluster!r}"
            )

        reservation = BudgetReservation(
            project_id=self.project_id,
            action_id=action.action_id,
            cluster=action.cluster,
            status=BudgetReservationStatus.RESERVED,
            cost_known=action.cost_known,
            estimated_cost_cny=action.estimated_cny,
            cost_upper_bound_cny=action.estimated_cny,
            resource_hours=_hours(action.resource_request.cpu_hours),
            resource_hours_upper_bound=_hours(action.resource_request.cpu_hours),
        )
        self.reservations.add(reservation)
        self.session.flush()
        return reservation

    def verify(self, action: Action) -> BudgetReservation:
        """Re-read the account and the Action's reservation before execution.

        The account is locked, so a concurrent overrun that pauses it cannot
        interleave between this check and the claim that follows in the same
        transaction.

        Raises:
            BudgetExceeded: The account is paused or the committed total now
                exceeds its limit.
            ActionConflict: The Action has no reservation, or the reservation
                is not the live one the Action names.
        """
        reservation = self.reservations.lock_for_action(action.action_id)
        if reservation is None:
            raise ActionConflict(
                f"Action {action.action_id} has no budget reservation; budget is "
                "reserved before an external submission, not after it"
            )
        if action.budget_reservation_ref != reservation.reservation_id:
            raise ActionConflict(
                "the Action names a different reservation than the one on record; "
                "the budget binding changed after approval"
            )
        if reservation.status is not BudgetReservationStatus.RESERVED:
            raise BudgetExceeded(
                f"the reservation for this Action is {reservation.status.value}; "
                "only a RESERVED promise may be executed"
            )
        account, _row = self.accounts.lock_for_cluster(action.cluster)
        if account is not None and account.is_paused:
            raise BudgetExceeded(
                f"cluster {action.cluster!r} is paused: "
                f"{account.pause_reason or 'no reason recorded'}"
            )
        limit = account.limit if account is not None else _ZERO
        if self.committed(action.cluster) > limit:
            raise BudgetExceeded(
                f"committed {format_cny(self.committed(action.cluster))} CNY "
                f"exceeds the {format_cny(limit)} CNY limit on {action.cluster!r}"
            )
        return reservation

    # ── Outcomes ────────────────────────────────────────────────────────────

    def release(self, action_id: str, *, reason: str = "") -> BudgetReservation | None:
        """Release an unused promise after a cancellation or known rejection.

        A settled reservation is history and is left alone. An unknown one is
        not released here: `release` is for outcomes that are known, and the
        caller that confuses the two is caught by the name.
        """
        reservation = self.reservations.lock_for_action(action_id)
        if reservation is None:
            return None
        if reservation.status in (
            BudgetReservationStatus.SETTLED,
            BudgetReservationStatus.RELEASED,
        ):
            return reservation
        released = reservation.model_copy(
            update={
                "status": BudgetReservationStatus.RELEASED,
                "released_at": utcnow(),
            }
        )
        self.reservations.save(released)
        return released

    def mark_unknown(self, action_id: str) -> BudgetReservation | None:
        """Keep the promise while the external outcome is uncertain.

        The status becomes `UNKNOWN`; the amount is unchanged. This is the
        whole of the rule that an unknown result does not release budget.
        """
        reservation = self.reservations.lock_for_action(action_id)
        if reservation is None:
            return None
        if reservation.status is BudgetReservationStatus.UNKNOWN:
            return reservation
        if reservation.status is not BudgetReservationStatus.RESERVED:
            raise ActionConflict(
                f"reservation {reservation.reservation_id} is "
                f"{reservation.status.value}; an unknown result cannot rewrite a "
                "settled or released outcome"
            )
        unknown = reservation.model_copy(update={"status": BudgetReservationStatus.UNKNOWN})
        self.reservations.save(unknown)
        return unknown

    def settle(
        self, action_id: str, actual_cny: str | None
    ) -> tuple[BudgetReservation, str | None]:
        """Record what the work actually cost, and detect an overrun.

        The estimate is never edited. If the actual is higher, the reservation
        records both numbers, an overrun reference is attached, and the account
        is paused for new submissions — a person decides what happens next.
        `actual_cny` is required: a caller that does not know the cost leaves
        the reservation `UNKNOWN` instead of settling it at zero.

        Raises:
            ValueError: `actual_cny` is absent. Unknown is a state, not zero.
            ActionConflict: The reservation was released by a known outcome.
        """
        if actual_cny is None:
            raise ValueError(
                "settling requires the actual cost; an unknown cost stays UNKNOWN "
                "and is never written as zero"
            )
        reservation = self.reservations.lock_for_action(action_id)
        if reservation is None:
            raise ActionConflict(f"Action {action_id} has no reservation to settle")
        if reservation.status is BudgetReservationStatus.RELEASED:
            raise ActionConflict(
                "the reservation was released by a known outcome and cannot be "
                "settled afterwards; a late cost is a new Action's problem"
            )
        actual = parse_cny(actual_cny, field="actual cost")
        assert actual is not None
        if reservation.status is BudgetReservationStatus.SETTLED:
            return reservation, reservation.overrun_event_ref
        settled = reservation.model_copy(
            update={
                "status": BudgetReservationStatus.SETTLED,
                "actual_cost_cny": format_cny(actual),
                "cost_known": True,
                "settled_at": utcnow(),
            }
        )
        self.reservations.save(settled)

        estimate = parse_cny(reservation.estimated_cost_cny) or _ZERO
        overrun = actual > estimate
        account, row = self.accounts.lock_for_cluster(settled.cluster)
        limit = account.limit if account is not None else _ZERO
        if not overrun and self.committed(settled.cluster) > limit:
            overrun = True
        if not overrun:
            return settled, None

        overrun_ref = new_id()
        settled = settled.model_copy(update={"overrun_event_ref": overrun_ref})
        self.reservations.save(settled)
        now = utcnow()
        reason = (
            f"actual cost {format_cny(actual)} CNY exceeded the approved "
            f"estimate {format_cny(estimate)} CNY for Action {action_id}"
        )
        if account is None:
            # A positive actual against no configured account is an overrun of
            # the zero limit. The account is created paused, so the next
            # submission is refused until an Owner acknowledges it.
            account = BudgetAccount(
                project_id=self.project_id,
                cluster=settled.cluster,
                limit_cny="0",
                configured_by="system:overrun",
                configured_at=now,
                created_at=now,
                paused_at=now,
                pause_reason=reason,
                overrun_at=now,
            )
            self.accounts.add(account)
            self.session.flush()
        else:
            paused = account.model_copy(
                update={"paused_at": now, "pause_reason": reason, "overrun_at": now}
            )
            self.accounts.save(paused, row)
        return settled, overrun_ref

    # ── Reads and internal checks ───────────────────────────────────────────

    def committed(self, cluster: str = "default") -> Decimal:
        """What this cluster currently counts against its limit.

        Settled reservations count their actual; open ones count their
        estimate; released ones count nothing. Summed from validated domain
        records rather than from SQL strings, so the fixed-point rule is the
        same one that wrote the rows.
        """
        return sum(
            (reservation.committed_cny for reservation in self.reservations.for_cluster(cluster)),
            _ZERO,
        )

    def _check_compute_slots(self) -> None:
        held = self._held_compute(project_only=True)
        cap = int(self.settings.action_compute_per_project)
        if held >= cap:
            raise BudgetExceeded(
                f"this project already holds {held} of its {cap} concurrent compute "
                "slots; a new submission waits for one to finish"
            )
        deployment = self._held_compute(project_only=False)
        global_cap = int(self.settings.action_compute_global)
        if deployment >= global_cap:
            raise BudgetExceeded(
                f"the deployment already holds {deployment} of its {global_cap} "
                "concurrent compute slots"
            )

    def _held_compute(self, *, project_only: bool) -> int:
        conditions = [
            ActionRow.kind == ActionKind.COMPUTE_SUBMIT.value,
            ActionRow.status.in_(
                [status.value for status in BUDGET_HOLDING_ACTION_STATUSES]
            ),
        ]
        if project_only:
            conditions.append(ActionRow.project_id == self.project_id)
        return int(
            self.session.execute(
                select(func.count()).select_from(ActionRow).where(*conditions)
            ).scalar_one()
        )

    def _advisory_lock(self, key: str) -> None:
        """Serialise one budget decision across every process.

        Transaction-scoped, so the lock disappears when the caller's
        transaction ends — including on rollback. The key is hashed by
        PostgreSQL, which can only over-serialise (two keys colliding), never
        under-serialise.
        """
        self.session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": key}
        )


def _hours(value: Decimal | str | int | None) -> str | None:
    """A resource quantity as fixed point, using the money formatter's rules."""
    if value is None:
        return None
    return format_cny(value)


__all__ = ["BudgetService"]
