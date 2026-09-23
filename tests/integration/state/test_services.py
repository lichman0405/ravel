"""A process saying it is alive, and the two ways that row can be wrong.

The row is unusual in this schema and the tests are shaped by that. It is
**overwritten rather than appended** — a report is the current answer to a
question about now, not a record of something that happened — so the first
thing to check is that a second beat replaces the first rather than accumulating
beside it. And its identity is one column wide, which the guard has to protect
without also freezing the fields a restart legitimately changes.

Nothing here decides when a heartbeat is stale. That judgement belongs with the
service's cadence and lives in `ravel.domain.services`; what is checked here is
that `None` — never reported — is not the same answer as "reported and then went
quiet", because conflating them reports a service as crashed on a deployment
that never started it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ravel.domain.clock import utcnow
from ravel.domain.services import (
    GATEWAY,
    SERVICE_NAMES,
    SUPERVISOR,
    ServiceReport,
    heartbeat_is_stale,
)
from ravel.state.database import Database
from ravel.state.repositories.services import ServiceRepository, instance_name

pytestmark = pytest.mark.integration


def _beat(database: Database, service: str, **detail: object) -> ServiceReport:
    """One report, in its own committed transaction."""
    started = utcnow() - timedelta(minutes=5)
    with database.transaction() as session:
        return ServiceRepository(session).report(
            service, started_at=started, detail=dict(detail)
        )


def _read(database: Database, service: str) -> ServiceReport | None:
    with database.read_only() as session:
        return ServiceRepository(session).get(service)


# ── The vocabulary ──────────────────────────────────────────────────────────


def test_the_service_names_are_a_closed_list_of_three() -> None:
    """Two spellings of `supervisor` would be two rows and one dead panel.

    A screen reading `SERVICE_NAMES` reports a service as absent when nothing
    has ever written under that exact string, so the tuple is the contract
    between whoever reports and whoever reads. Three because V0 is three
    processes on one host, and this assertion is where adding a fourth gets
    noticed.
    """
    assert SERVICE_NAMES == (SUPERVISOR, "temporal-worker", GATEWAY)
    assert len(set(SERVICE_NAMES)) == len(SERVICE_NAMES)


# ── Writing one ─────────────────────────────────────────────────────────────


def test_a_report_is_written_with_the_instance_it_came_from(database: Database) -> None:
    """`host:pid` is derived rather than demanded, so it cannot be forgotten.

    It is the field a reader needs in order to go and look at the process, and
    a service that reported without one would be a row saying only that
    something, somewhere, is alive.
    """
    written = _beat(database, SUPERVISOR, poll_seconds=5)

    assert written.service == SUPERVISOR
    assert written.instance == instance_name()
    assert ":" in written.instance
    assert written.detail == {"poll_seconds": 5}

    reread = _read(database, SUPERVISOR)
    assert reread is not None
    assert reread.instance == written.instance


def test_a_second_beat_replaces_the_first_rather_than_joining_it(database: Database) -> None:
    """The row is the current answer, and a restart takes it over.

    `started_at` travels with the beat for exactly this reason: a process that
    came back a minute ago must say so, and a row that kept the first start
    would report hours of uptime for a process minutes old — which is the
    number an operator uses to decide whether they are looking at a restart
    loop.
    """
    first = _beat(database, SUPERVISOR, poll_seconds=5)
    restarted = utcnow()
    with database.transaction() as session:
        second = ServiceRepository(session).report(
            SUPERVISOR, started_at=restarted, detail={"poll_seconds": 5}
        )

    assert second.heartbeat_at >= first.heartbeat_at
    assert second.started_at > first.started_at
    assert _read(database, SUPERVISOR) is not None
    with database.read_only() as session:
        assert ServiceRepository(session).all() == [second], (
            "a second beat left the first row behind, so the table is a log "
            "rather than a current answer"
        )


def test_reading_a_service_that_never_reported_is_none_and_not_an_empty_row(
    database: Database,
) -> None:
    """`named` is a mapping so that a missing service is distinguishable.

    The caller asks about a fixed set of *names* this deployment runs, and some
    of them may never have reported. A list would silently omit those, and a
    reader iterating it could not tell a service missing from the answer from
    one missing from the deployment.
    """
    _beat(database, SUPERVISOR)

    with database.read_only() as session:
        found = ServiceRepository(session).named(SERVICE_NAMES)

    assert set(found) == {SUPERVISOR}
    assert found[SUPERVISOR].service == SUPERVISOR
    assert GATEWAY not in found


def test_every_service_is_read_back_in_name_order(database: Database) -> None:
    """So two reads a second apart compare, and a panel does not reshuffle."""
    for name in (GATEWAY, SUPERVISOR, "temporal-worker"):
        _beat(database, name)

    with database.read_only() as session:
        reported = ServiceRepository(session).all()

    assert [report.service for report in reported] == ["gateway", "supervisor", "temporal-worker"]


# ── What the guard protects ─────────────────────────────────────────────────


def test_a_process_may_not_be_renamed_into_another_services_row(database: Database) -> None:
    """The identity is one column, and it is the shortest list in the schema.

    Short because there is nothing else worth freezing: `instance`,
    `started_at` and `heartbeat_at` all change on a restart *by design*, so a
    guard that protected them would refuse the write this table exists for.
    What it stops is one process beating on another's row — a worker's report
    landing under `supervisor` would show an operator a supervisor that is
    alive when none is running, which is the single answer this table is
    trusted for.
    """
    _beat(database, SUPERVISOR)

    with pytest.raises(DBAPIError, match="service"), database.transaction() as session:
        session.execute(
            text("UPDATE runtime_services SET service = 'gateway' WHERE service = 'supervisor'")
        )

    assert _read(database, SUPERVISOR) is not None


def test_a_report_may_be_updated_in_place_without_being_a_log(database: Database) -> None:
    """The other half of the same rule, and the one that could break the beat.

    A beat *is* an update — a later one replaces an earlier — so a table on the
    append-only guard would refuse the second tick and the supervisor would
    look alive for exactly one poll interval. That is the failure this asserts
    against, not a write it tolerates.
    """
    _beat(database, SUPERVISOR)

    with database.transaction() as session:
        session.execute(
            text(
                "UPDATE runtime_services SET heartbeat_at = :now, detail = :detail "
                "WHERE service = 'supervisor'"
            ),
            {"now": utcnow(), "detail": '{"poll_seconds": 1}'},
        )

    reread = _read(database, SUPERVISOR)
    assert reread is not None
    assert reread.detail == {"poll_seconds": 1}


# ── When silence is a fault ─────────────────────────────────────────────────


def test_never_having_reported_is_not_the_same_as_having_gone_quiet() -> None:
    """The distinction the whole panel turns on.

    A deployment that has not started a worker has not lost one, and a screen
    that reported both as a crash would send somebody looking for a process
    that was never run. Only the second of these two is a fault, and taking an
    optional report is what keeps them apart.
    """
    assert heartbeat_is_stale(None, budget_seconds=10.0) is False

    silent = ServiceReport(
        service=SUPERVISOR,
        instance="host:1",
        started_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
        heartbeat_at=datetime(2026, 9, 23, 12, 0, tzinfo=UTC),
    )
    now = datetime(2026, 9, 23, 12, 0, 30, tzinfo=UTC)

    assert silent.age_seconds(now=now) == 30.0
    assert heartbeat_is_stale(silent, budget_seconds=10.0, now=now) is True
    assert heartbeat_is_stale(silent, budget_seconds=60.0, now=now) is False


def test_a_clock_that_went_backwards_is_not_read_as_ancient_silence() -> None:
    """A negative age compares as fresh, and that is left alone rather than clamped.

    A host whose time moved is a real thing to diagnose, and a repository that
    clamped the age to zero would hide it. What must not happen is the other
    direction — a report from the future reading as a service that has been
    dead for a very long time — and it does not, because a negative number is
    below every budget.
    """
    future = ServiceReport(
        service=SUPERVISOR,
        instance="host:1",
        started_at=utcnow() + timedelta(hours=1),
        heartbeat_at=utcnow() + timedelta(hours=1),
    )
    now = utcnow()

    assert future.age_seconds(now=now) < 0
    assert heartbeat_is_stale(future, budget_seconds=1.0, now=now) is False


def test_the_instance_a_report_carries_is_this_process(database: Database) -> None:
    """Asserted through the repository's own reader rather than against a literal.

    A row naming a host it had merely been told about would be a row asserting
    something the reporting process has no way to know, so the name is derived
    from the caller's own machine — and the caller here is this test.
    """
    written = _beat(database, GATEWAY)
    host, _, pid = written.instance.partition(":")

    assert host and pid.isdigit()
    assert written.instance == instance_name()
