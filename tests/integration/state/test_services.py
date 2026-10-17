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
    TEMPORAL_WORKER,
    ServiceReport,
    heartbeat_is_stale,
)
from ravel.state.database import Database
from ravel.state.repositories.services import ServiceRepository, instance_name

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def an_empty_table(clean: None) -> None:
    """Start from nothing, because several cases here read the whole table.

    `all()` is one of the things this module is about — "a second beat replaces
    the first" is a claim about every row there is — and a row another suite
    left behind would fail it for a reason that has nothing to do with the
    code. `clean` is the suite's own fixture rather than a `TRUNCATE` written
    here: it is the one place that knows about the lock timeout that keeps a
    leaked reader from hanging every test after it.

    The row that made this necessary is real and worth naming. Since Phase 11
    the Gateway beats from its own lifespan, so any suite that serves the
    application through a `TestClient` leaves a `gateway` row behind when it
    finishes — which is the behaviour a deployment wants and a reason for the
    next suite to clean up after itself.
    """


def _beat(database: Database, service: str, **detail: object) -> ServiceReport:
    """One report, in its own committed transaction."""
    started = utcnow() - timedelta(minutes=5)
    with database.transaction() as session:
        return ServiceRepository(session).report(
            service, started_at=started, detail=dict(detail)
        )


def _retire(database: Database, service: str) -> ServiceReport | None:
    """One shutdown, in its own committed transaction."""
    with database.transaction() as session:
        return ServiceRepository(session).retire(service)


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


# ── Saying it is going ──────────────────────────────────────────────────────


def test_a_service_that_said_it_was_going_is_not_a_service_that_went_quiet(
    database: Database,
) -> None:
    """The distinction a deploy turns on, and the only thing that makes it.

    Both endings are silence: a supervisor stopped for an upgrade and a
    supervisor that was killed both stop beating, and from the beats alone the
    screen cannot tell them apart. What separates them is that one of them said
    so on the way out. With that written down, the question the staleness
    budget answers does not arise — which is asserted here at a full hour past
    the last beat, a silence that would otherwise be a dead process many times
    over.
    """
    started = _beat(database, SUPERVISOR, poll_seconds=5)
    stopped = _retire(database, SUPERVISOR)

    assert stopped is not None
    assert stopped.stopped_at is not None
    assert stopped.heartbeat_at == started.heartbeat_at, (
        "retiring moved the last beat, so the moment the process was last "
        "working is no longer readable"
    )
    assert heartbeat_is_stale(
        stopped, budget_seconds=1.0, now=stopped.heartbeat_at + timedelta(hours=1)
    ) is False


def test_a_replacement_clears_the_shutdown_it_is_replacing(database: Database) -> None:
    """Because a row that kept it reports a service as down while it answers.

    A beat and a retirement write the same row, and this is the case where the
    two must not agree: a process that came back is running, so the restart is
    the fact and the shutdown before it is not. Without the clear, restarting a
    service leaves an operator watching it serve requests while the screen says
    it is stopped — the failure the field was added to prevent, produced by the
    field itself.
    """
    _beat(database, SUPERVISOR)
    _retire(database, SUPERVISOR)
    again = _beat(database, SUPERVISOR, poll_seconds=5)

    assert again.stopped_at is None
    assert heartbeat_is_stale(
        again, budget_seconds=1.0, now=again.heartbeat_at + timedelta(hours=1)
    ) is True, "a restarted service is running, and an hour of silence is a fault again"


def test_retiring_something_that_never_reported_does_not_invent_a_row(
    database: Database,
) -> None:
    """Because a process on its way out cannot know whether its beats landed.

    The supervisor retires unconditionally as it shuts down, and the database
    may have been unreachable for the beats before that — so "I am stopping"
    routinely arrives for a service with nothing on record. `None` is the
    answer to that. A row written here would put a service on the
    administrator's screen that this deployment has never run, reporting a
    shutdown that is the only thing ever heard from it.
    """
    assert _retire(database, TEMPORAL_WORKER) is None
    assert _read(database, TEMPORAL_WORKER) is None

    with database.read_only() as session:
        assert ServiceRepository(session).all() == []


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


def test_a_service_that_has_stopped_may_not_be_deleted_out_of_the_table(
    database: Database,
) -> None:
    """Why a shutdown is an update, and not the row simply going away.

    A process able to erase its own row could erase one belonging to a service
    that never said anything — and it cannot, because the guard is
    statement-level and refuses every `DELETE` rather than the ones it can
    attribute to a caller. That is the schema's answer to the question, and
    this is where the repository's choice to update is held to it: after a
    shutdown the row is still there, carrying a time, which is the fact a
    reader wanted. An absence would have said only that something used to be.
    """
    _beat(database, SUPERVISOR)
    _retire(database, SUPERVISOR)

    with pytest.raises(DBAPIError, match="never deleted"), database.transaction() as session:
        session.execute(text("DELETE FROM runtime_services WHERE service = 'supervisor'"))

    left = _read(database, SUPERVISOR)
    assert left is not None
    assert left.stopped_at is not None


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
