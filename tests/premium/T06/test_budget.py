"""T06-AC1: the budget gate holds under concurrency, and only known outcomes release.

Every test here runs against the real PostgreSQL database. A promise made
before an external submission is only worth anything if two processes cannot
both read the same remainder and both spend it; the advisory lock and the
committed-total read are the mechanism, so a fake would not exercise it.
"""

from __future__ import annotations

import threading
from decimal import Decimal

import pytest
from sqlalchemy.orm import Session
from tests.premium.T06.support import create_mail, install_envelope

from ravel.actions.service import ActionService
from ravel.budgets.service import BudgetService
from ravel.config import Settings
from ravel.domain.actions import BudgetExceeded
from ravel.domain.enums import (
    ActionAttemptOutcome,
    ActionStatus,
    BudgetReservationStatus,
)
from ravel.domain.project import Project
from ravel.state.database import Database


def _budget(session: Session, project_id: str, settings: Settings) -> BudgetService:
    return BudgetService(session, project_id, settings=settings)


@pytest.mark.timeout(180)
def test_two_concurrent_reservations_cannot_break_the_remaining_limit(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """Two writers race for the last 40 fen; exactly one of them gets it.

    Each thread has its own session and its own transaction. They meet at the
    barrier and then both try to reserve against a 1.00 CNY account. The
    service serialises on a transaction-scoped advisory lock, so the loser
    reads the winner's committed reservation rather than the empty account the
    winner also read.
    """
    owner = project.created_by
    with database.transaction() as session:
        _budget(session, project.project_id, integration_settings).configure(
            limit_cny="1.00", configured_by=owner
        )

    barrier = threading.Barrier(2)
    outcomes: list[tuple[str, str]] = []

    def attempt(index: int) -> None:
        session = database.sessions()
        try:
            service = ActionService(
                session, project.project_id, settings=integration_settings
            )
            barrier.wait(timeout=30)
            action = create_mail(
                service,
                owner,
                message_id=f"<race-{index}@ravel.example>",
                estimate="0.60",
            )
            session.commit()
            outcomes.append(("reserved", action.action_id))
        except BudgetExceeded as error:
            session.rollback()
            outcomes.append(("refused", str(error)))
        except BaseException as error:  # pragma: no cover - a bug, not a branch
            session.rollback()
            outcomes.append(("error", repr(error)))
        finally:
            session.close()

    threads = [
        threading.Thread(target=attempt, args=(index,), name=f"t06-budget-{index}")
        for index in (1, 2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert not any(thread.is_alive() for thread in threads), (
        "a reservation blocked past the test's patience; the advisory lock is "
        "not being released"
    )
    reserved = [outcome for outcome in outcomes if outcome[0] == "reserved"]
    refused = [outcome for outcome in outcomes if outcome[0] == "refused"]
    errors = [outcome for outcome in outcomes if outcome[0] == "error"]
    assert errors == [], f"concurrent reservation failed unexpectedly: {errors}"
    assert len(reserved) == 1, (
        "both concurrent requests were admitted against a 1.00 CNY limit; one "
        f"reservation should have won: {outcomes}"
    )
    assert len(refused) == 1, (
        "the losing concurrent request was not refused by the budget gate: "
        f"{outcomes}"
    )

    with database.read_only() as session:
        budget = _budget(session, project.project_id, integration_settings)
        actions = ActionService(
            session, project.project_id, settings=integration_settings
        ).list()
        assert len(actions) == 1, "the refused transaction left an Action behind"
        assert actions[0].status is ActionStatus.READY
        assert budget.committed("default") == Decimal("0.60"), (
            "the committed total does not equal the winning reservation"
        )


def test_unknown_cost_is_refused_against_a_zero_limit(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """An unpriced submission cannot be promised against an unconfigured budget.

    "Unknown" is not free. With no Owner-configured limit the account is zero,
    and a reservation whose cost nobody knows would be a promise of an amount
    nobody can bound — so it is refused rather than written as zero.
    """
    owner = project.created_by

    with pytest.raises(BudgetExceeded, match="unknown"), database.transaction() as session:
        create_mail(
            ActionService(
                session, project.project_id, settings=integration_settings
            ),
            owner,
            message_id="<unpriced@ravel.example>",
            estimate=None,
            cost_known=False,
        )

    with database.transaction() as session:
        _budget(session, project.project_id, integration_settings).configure(
            limit_cny="100.00", configured_by=owner
        )

    # A positive limit does not make an unbounded unknown reservable: it would
    # commit zero against a cost nobody can bound. The caller must state an
    # upper bound; UNKNOWN is still not zero.
    with pytest.raises(BudgetExceeded, match="upper bound"), database.transaction() as session:
        create_mail(
            ActionService(session, project.project_id, settings=integration_settings),
            owner,
            message_id="<unpriced@ravel.example>",
            estimate=None,
            cost_known=False,
        )

    with database.transaction() as session:
        action = create_mail(
            ActionService(session, project.project_id, settings=integration_settings),
            owner,
            message_id="<unpriced@ravel.example>",
            estimate="5.00",
            cost_known=False,
        )
    assert action.status is ActionStatus.READY

    with database.read_only() as session:
        budget = _budget(session, project.project_id, integration_settings)
        reservation = budget.reservations.for_action(action.action_id)
        assert reservation is not None
        assert reservation.status is BudgetReservationStatus.RESERVED
        assert reservation.cost_known is False
        assert Decimal(reservation.estimated_cost_cny or "0") == Decimal("5.00"), (
            "the explicit upper bound must be the committed amount"
        )
        assert Decimal(reservation.cost_upper_bound_cny or "0") == Decimal("5.00")
        assert budget.committed("default") == Decimal("5.00"), (
            "an unknown-cost reservation with an upper bound must commit that bound"
        )


def test_known_outcomes_release_the_reservation_and_unknown_keeps_it(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """AC1's two halves: cancellation and confirmed failure release; uncertainty does not."""
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")
        _budget(session, project.project_id, integration_settings).configure(
            limit_cny="10.00", configured_by=owner
        )

    # ── A cancelled Action releases its promise ─────────────────────────────
    with database.transaction() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        cancelling = create_mail(
            service, owner, message_id="<cancel-me@ravel.example>", estimate="1.50"
        )
        assert cancelling.status is ActionStatus.AWAITING_APPROVAL
        service.approve(cancelling.action_id, resolved_by=owner)
        cancelled = service.cancel(cancelling.action_id, cancelled_by=owner)
    assert cancelled.status is ActionStatus.CANCELLED

    with database.read_only() as session:
        budget = _budget(session, project.project_id, integration_settings)
        reservation = budget.reservations.for_action(cancelling.action_id)
        assert reservation is not None
        assert reservation.status is BudgetReservationStatus.RELEASED
        assert budget.committed("default") == Decimal("0")

    # ── An unknown external result keeps the promise ────────────────────────
    with database.transaction() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        uncertain = create_mail(
            service, owner, message_id="<maybe-sent@ravel.example>", estimate="1.50"
        )
        service.approve(uncertain.action_id, resolved_by=owner)
        service.claim(uncertain.action_id, claimed_by=owner)
        unknown = service.record_unknown(
            uncertain.action_id, reason="SMTP DATA was issued and the connection died"
        )
    assert unknown.status is ActionStatus.UNKNOWN
    assert unknown.projected_status == "UNKNOWN_DELIVERY", (
        "a mail send with an uncertain outcome must project UNKNOWN_DELIVERY, "
        "not a generic unknown or a success"
    )

    with database.read_only() as session:
        budget = _budget(session, project.project_id, integration_settings)
        reservation = budget.reservations.for_action(uncertain.action_id)
        assert reservation is not None
        assert reservation.status is BudgetReservationStatus.UNKNOWN
        assert budget.committed("default") == Decimal("1.50"), (
            "an unknown outcome released budget; the external effect may still "
            "have happened and its cost is unresolved"
        )

    # A known failure after reconciliation is what releases it.
    with database.transaction() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        failed = service.reconcile(
            uncertain.action_id,
            outcome=ActionAttemptOutcome.FAILED_CONFIRMED,
            note="provider confirmed the message never left the queue",
        )
    assert failed.status is ActionStatus.FAILED_CONFIRMED

    with database.read_only() as session:
        budget = _budget(session, project.project_id, integration_settings)
        reservation = budget.reservations.for_action(uncertain.action_id)
        assert reservation is not None
        assert reservation.status is BudgetReservationStatus.RELEASED
        assert budget.committed("default") == Decimal("0")


def test_outcome_reports_require_the_claimant_or_a_live_owner(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """A caller that did not claim the Action cannot write its ending."""
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")
        _budget(session, project.project_id, integration_settings).configure(
            limit_cny="10.00", configured_by=owner
        )

    with database.transaction() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        action = create_mail(
            service, owner, message_id="<reporter@ravel.example>", estimate="0.50"
        )
        service.approve(action.action_id, resolved_by=owner)
        service.claim(action.action_id, claimed_by=owner)

        with pytest.raises(PermissionError, match="did not claim"):
            service.record_accepted(
                action.action_id, reported_by="not-a-project-member"
            )
        with pytest.raises(PermissionError, match="did not claim"):
            service.record_unknown(
                action.action_id,
                reason="an intruder must not be able to close the attempt",
                reported_by="not-a-project-member",
            )

        accepted = service.record_accepted(action.action_id)
    assert accepted.status is ActionStatus.ACCEPTED
    assert accepted.claimed_by == owner


def test_completion_requires_an_actual_cost_and_an_overrun_pauses_new_work(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """A known actual cost settles; an overrun records the real number and pauses.

    Completion with no cost is refused rather than settled at zero, and the
    estimate is never edited down to meet the actual. The overrun event pauses
    the account so the next submission waits for a person.
    """
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")
        _budget(session, project.project_id, integration_settings).configure(
            limit_cny="1.00", configured_by=owner
        )

    with database.transaction() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        action = create_mail(
            service, owner, message_id="<overrun@ravel.example>", estimate="0.60"
        )
        service.approve(action.action_id, resolved_by=owner)
        service.claim(action.action_id, claimed_by=owner)
        service.record_accepted(action.action_id, external_ref="smtp:accepted")

        with pytest.raises(ValueError, match="actual cost"):
            service.complete(action.action_id, actual_cny=None)
        completed = service.complete(action.action_id, actual_cny="1.50")

    assert completed.status is ActionStatus.COMPLETED
    assert completed.overrun_event_ref, "the overrun was not recorded"

    with database.read_only() as session:
        budget = _budget(session, project.project_id, integration_settings)
        reservation = budget.reservations.for_action(action.action_id)
        assert reservation is not None
        assert reservation.status is BudgetReservationStatus.SETTLED
        assert reservation.estimated_cost_cny == "0.6", (
            "the estimate was rewritten to match the actual; both numbers must "
            "stay on the record"
        )
        assert reservation.actual_cost_cny == "1.5"
        account = budget.account("default")
        assert account is not None and account.is_paused

    # The envelope still requires an approval, so the refusal lands when the
    # approval tries to turn into a budget reservation — the point where the
    # money would actually be promised.
    with pytest.raises(BudgetExceeded, match="paused"), database.transaction() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        following = create_mail(
            service,
            owner,
            message_id="<after-overrun@ravel.example>",
            estimate="0.01",
        )
        service.approve(following.action_id, resolved_by=owner)
