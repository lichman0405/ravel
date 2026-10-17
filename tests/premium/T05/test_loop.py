"""T05 AC1-AC4 at the level a supervisor drives.

The store tests pin the row. These pin what a `ProjectLoop` does with it: that a
supervisor restart finds the spent budget (AC1), that a cooldown admits no model
request and an operator retry does (AC2), that a long HPC or lab wait is not a
run of unanswered questions and an escalation is one item (AC3), and that a
paused, ended, or lease-lost project dispatches nothing while keeping what it
had (AC4).

Model turns are counted by the ports, not by a sleep or a log line. A test that
asserted "it felt bounded" would be asserting the clock.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from tests.premium.T05.support import (
    T0,
    CountingPort,
    CountingSeat,
    FakeClock,
    StartingSeat,
    make_loop,
    read_project,
    transition_project,
    wiring,
)

from ravel.config import Settings
from ravel.domain.enums import (
    NodeStatus,
    ProjectStatus,
    RetryEpisodeStatus,
    RetryQuestion,
    ReviewOutcome,
)
from ravel.execution.loop import Situation, read_situation
from ravel.execution.retry_policy import DEFAULT_COOLDOWNS
from ravel.execution.supervisor import ProjectSupervisor
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository

SLOW_HPC = 30.0  # days; a long job, not a model stall


class _HungMaster(CountingPort):
    """A Master turn that never returns, to exercise the 180-second ceiling."""

    async def act(self, situation: Situation) -> None:
        _ = situation
        self.turns += 1
        await asyncio.Event().wait()


async def _advance(loop, situation: Situation, times: int) -> None:
    for _ in range(times):
        await loop._advance(situation)


# ── AC1: ten restarts, nine calls ──────────────────────────────────────────


async def test_ten_supervisor_restarts_make_at_most_nine_model_calls(
    database: Database, integration_settings: Settings, project
) -> None:
    """The ceiling is a row, so rebuilding the supervisor does not reset it."""
    master = CountingPort()
    review = CountingPort()
    calls_per_restart: list[int] = []

    supervisor = None
    for _ in range(10):
        supervisor = ProjectSupervisor(
            database=database, settings=integration_settings
        )
        assert supervisor.retry.policy.max_attempts == 9
        loop = make_loop(
            database,
            project.project_id,
            retry=supervisor.retry,
            master=master,
            review=review,
            max_rounds=6,
            max_stalled_rounds=4,
        )
        before = master.turns
        run = await loop.run()
        calls_per_restart.append(master.turns - before)
        assert run.status is ProjectStatus.CREATED

    assert calls_per_restart == [3, 3, 3, 0, 0, 0, 0, 0, 0, 0]
    assert master.turns == 9, "ten restarts exceeded the nine-call budget"

    assert supervisor is not None
    stalled = supervisor.pending_escalations(project.project_id)
    assert len(stalled) == 1
    assert stalled[0].attempts == 9
    assert stalled[0].status is RetryEpisodeStatus.AWAITING_OPERATOR

    # A runtime stall is not a scientific outcome. The project never moved.
    assert read_project(database, project.project_id) is ProjectStatus.CREATED


async def test_a_restart_during_a_healthy_round_does_not_duplicate_a_call(
    database: Database, integration_settings: Settings, project
) -> None:
    """Two supervisors one after the other spend two attempts, not four."""
    master = CountingPort()
    situation = read_situation(database, project.project_id)

    first = ProjectSupervisor(database=database, settings=integration_settings)
    second = ProjectSupervisor(database=database, settings=integration_settings)
    assert first.retry is not second.retry

    await make_loop(
        database, project.project_id, retry=first.retry, master=master
    )._advance(situation)
    await make_loop(
        database, project.project_id, retry=second.retry, master=master
    )._advance(situation)

    assert master.turns == 2
    assert second.retry.store.episodes(project.project_id)[0].attempts == 2


# ── AC2: cooldown, and the operator who unlocks it ─────────────────────────


async def test_cooldown_admits_no_request_and_an_operator_retry_opens_a_new_episode(
    database: Database, integration_settings: Settings, project
) -> None:
    """A 60-second wait, a 300-second wait, then a person — never a timer."""
    clock = FakeClock()
    budget = wiring(database, clock=clock, cooldowns=DEFAULT_COOLDOWNS)
    master = CountingPort()
    review = CountingPort()
    situation = read_situation(database, project.project_id)

    def restarted_loop():
        return make_loop(
            database,
            project.project_id,
            retry=budget,
            master=master,
            review=review,
        )

    # Round one: three attempts, then a 60-second cooldown. Still the same
    # episode and still the same budget.
    loop = restarted_loop()
    await _advance(loop, situation, 3)
    assert master.turns == 3
    episode = budget.store.episodes(project.project_id)[0]
    assert episode.status is RetryEpisodeStatus.COOLDOWN
    assert episode.attempts == 3
    assert episode.cooldown_until == T0 + timedelta(seconds=60)

    # At the instant of the third attempt and one second before the cooldown
    # expires, a freshly restarted loop makes no request at all.
    await restarted_loop()._advance(situation)
    clock.advance(seconds=59)
    await restarted_loop()._advance(situation)
    assert master.turns == 3, "a request was made during the cooldown"
    assert budget.store.episodes(project.project_id)[0].attempts == 3

    # The expiry is a dispatch, and it is attempt four: a cooldown delays the
    # budget, it does not refill it.
    clock.advance(seconds=1)
    await restarted_loop()._advance(situation)
    assert master.turns == 4
    assert budget.store.episodes(project.project_id)[0].attempts == 4

    # Finish round two, wait out the 300 seconds, spend round three.
    await restarted_loop()._advance(situation)
    await restarted_loop()._advance(situation)
    assert master.turns == 6
    assert budget.store.episodes(project.project_id)[0].cooldown_until == (
        T0 + timedelta(seconds=360)
    )
    clock.advance(seconds=300)
    await _advance(restarted_loop(), situation, 3)
    assert master.turns == 9

    stalled = budget.store.pending_escalations(project.project_id)
    assert len(stalled) == 1 and stalled[0].attempts == 9

    # A year is not an answer. No new scientific state, no operator, no calls.
    clock.advance(days=365)
    await _advance(restarted_loop(), situation, 5)
    assert master.turns == 9, "a timer unlocked the operator gate"
    assert budget.store.pending_escalations(project.project_id)[0].attempts == 9

    # The operator's retry is explicit and named. It retires the spent episode
    # and opens a replacement with a fresh budget.
    replacement_clock = clock.advance(seconds=30)
    replacements = budget.store.operator_retry(
        project_id=project.project_id,
        actor="operator-dana",
        now=replacement_clock,
    )
    assert len(replacements) == 1
    assert replacements[0].attempts == 0
    assert replacements[0].operator_actor == "operator-dana"
    assert budget.store.pending_escalations(project.project_id) == []

    retired = budget.store.episodes(project.project_id, include_retired=True)
    old = next(row for row in retired if row.status is RetryEpisodeStatus.RETIRED)
    assert old.retired_reason == "operator_retry"
    assert old.operator_actor == "operator-dana"
    assert old.operator_retried_at == replacement_clock

    # The next supervisor — or the same one, on its next loop — dispatches the
    # replacement's first attempt without any signal.
    await restarted_loop()._advance(situation)
    assert master.turns == 10
    live = budget.store.episodes(project.project_id)
    assert len(live) == 1
    assert live[0].attempts == 1
    assert live[0].episode_id == replacements[0].episode_id


def test_the_supervisor_exposes_the_escalation_and_the_operator_retry(
    database: Database, integration_settings: Settings, project
) -> None:
    """The TUI/operator route has two calls, and the supervisor owns both."""
    supervisor = ProjectSupervisor(database=database, settings=integration_settings)

    # Spend the whole policy through the supervisor's own wiring, then poll it
    # from outside a loop: this is the operator's view.
    for _ in range(9):
        supervisor.retry.store.reserve(
            project_id=project.project_id,
            problem_key="ending",
            state_version="v1",
            now=T0,
        )
    # The supervisor's test wiring has zero cooldowns, so every reserve
    # dispatched; the ninth is the gate.

    pending = supervisor.pending_escalations(project.project_id)
    assert len(pending) == 1 and pending[0].attempts == 9

    replacements = supervisor.operator_retry(
        project.project_id, actor="operator-erin", question=RetryQuestion.ENDING
    )
    assert len(replacements) == 1
    assert replacements[0].attempts == 0
    assert replacements[0].operator_actor == "operator-erin"
    assert supervisor.pending_escalations(project.project_id) == []


# ── AC3: long waits are not stalls; one escalation is one item ──────────────


async def test_a_long_hpc_or_lab_wait_is_not_counted_as_a_model_stall(
    database: Database, integration_settings: Settings, project, prepare
) -> None:
    """A RUNNING node, then WAITING_EXTERNAL, polls without asking anybody again."""
    prepared = prepare(cleared=True)
    compute = StartingSeat(database)
    master = CountingPort()
    review = CountingPort()
    clock = FakeClock()
    budget = wiring(database, clock=clock)
    transition_project(database, project.project_id, ProjectStatus.CONTRACT_DEFINED)

    # The seat begins the job: its own DAG transition, so the node is RUNNING
    # and the project is EXECUTING by the time the next round reads it.
    running = await make_loop(
        database,
        project.project_id,
        retry=budget,
        master=master,
        review=review,
        compute_worker=compute,
        max_rounds=6,
        max_stalled_rounds=2,
    ).run()

    assert running.status is ProjectStatus.EXECUTING
    assert running.stalled_rounds == 0, "an HPC run was counted as a stall"
    assert compute.started == [prepared.node_id]
    assert compute.turns == [(prepared.node_id, NodeStatus.RUNNING.value)]
    assert master.turns == 0
    assert review.turns == 0
    assert budget.store.episodes(project.project_id) == []

    # The job is now held by a lab. Thirty days later the loop still only
    # monitors it once; nobody is being asked an unanswered question.
    with database.transaction() as session:
        DagRepository(session, project.project_id).transition_node(
            prepared.node_id, NodeStatus.WAITING_EXTERNAL, actor_id="lab"
        )
    clock.advance(days=SLOW_HPC)

    waiting = await make_loop(
        database,
        project.project_id,
        retry=budget,
        master=master,
        review=review,
        compute_worker=compute,
        max_rounds=6,
        max_stalled_rounds=2,
    ).run()

    assert waiting.status is ProjectStatus.EXECUTING
    assert waiting.stalled_rounds == 0, "a lab wait was counted as a stall"
    assert compute.turns[-1] == (
        prepared.node_id,
        NodeStatus.WAITING_EXTERNAL.value,
    )
    assert master.turns == 0
    assert review.turns == 0
    assert budget.store.episodes(project.project_id) == []

    # The scientific state is untouched: the node is still where the lab left
    # it, and its pre-run clearance and contracts are still there.
    after = read_situation(database, project.project_id)
    assert after.project.status is ProjectStatus.EXECUTING
    assert after.nodes[0].status is NodeStatus.WAITING_EXTERNAL
    assert after.pre_run[prepared.node_id] is ReviewOutcome.PASS


async def test_a_stalled_question_is_one_pending_item_and_one_log_line(
    database: Database, integration_settings: Settings, project, caplog
) -> None:
    """The loop may poll forever; the escalation does not multiply."""
    clock = FakeClock()
    budget = wiring(database, clock=clock, cooldowns=(0.0, 0.0))
    master = CountingPort()
    situation = read_situation(database, project.project_id)

    # Three restarts, three attempts each: the full nine, to the gate.
    for _ in range(3):
        loop = make_loop(
            database,
            project.project_id,
            retry=budget,
            master=master,
            max_rounds=5,
            max_stalled_rounds=4,
        )
        await _advance(loop, situation, 3)
    assert master.turns == 9

    escalation = budget.store.pending_escalations(project.project_id)
    assert len(escalation) == 1
    assert escalation[0].attempts == 9
    assert escalation[0].escalation_event_id is not None

    # Repeated reads of the projection are the same item, not one per poll.
    reads = [
        budget.store.pending_escalations(project.project_id) for _ in range(10)
    ]
    assert all(len(rows) == 1 for rows in reads)
    assert len({rows[0].episode_id for rows in reads}) == 1
    assert len({rows[0].escalation_event_id for rows in reads}) == 1

    # One loop polling ten times logs the operator escalation once. A message
    # per tick is the failure this assertion exists for.
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="ravel.execution.loop"):
        polling = make_loop(
            database,
            project.project_id,
            retry=budget,
            master=master,
            max_rounds=10,
            max_stalled_rounds=4,
        )
        await polling.run()

    assert master.turns == 9, "a poll after the gate still called a model"
    escalations = [
        record
        for record in caplog.records
        if "awaits an operator" in record.getMessage()
    ]
    assert len(escalations) == 1
    assert budget.store.pending_escalations(project.project_id)[0].attempts == 9

    # Again: a runtime stall is not a scientific failure or a success.
    assert read_project(database, project.project_id) is ProjectStatus.CREATED


# ── The turn ceiling ───────────────────────────────────────────────────────


async def test_a_hung_turn_times_out_and_still_spends_the_attempt(
    database: Database, project
) -> None:
    """A model that never answers is one attempt, not a hung supervisor."""
    clock = FakeClock()
    budget = wiring(database, clock=clock, cooldowns=(0.0, 0.0), timeout=0.05)
    master = _HungMaster()
    situation = read_situation(database, project.project_id)

    for _ in range(3):
        loop = make_loop(
            database, project.project_id, retry=budget, master=master
        )
        await loop._advance(situation)

    assert master.turns == 3, "the hung turn was retried inside one attempt"
    episode = budget.store.episodes(project.project_id)[0]
    assert episode.attempts == 3
    assert episode.status is RetryEpisodeStatus.COOLDOWN


# ── AC4: pause, ending, lease loss ─────────────────────────────────────────


def _assert_evidence_preserved(
    database: Database, project_id: str, node_id: str
) -> None:
    """The prepared node's clearance and contracts are exactly as they were."""
    with database.read_only() as session:
        dag = DagRepository(session, project_id)
        assert dag.has_frozen_acceptance(node_id)
        assert dag.has_execution_contract(node_id)
        assert dag.latest_pre_run_outcome(node_id) is ReviewOutcome.PASS
        assert dag.node(node_id).status is NodeStatus.READY


async def test_a_paused_project_dispatches_nothing_and_keeps_its_evidence(
    database: Database, project, prepare
) -> None:
    prepared = prepare(cleared=True)
    transition_project(
        database,
        project.project_id,
        ProjectStatus.CONTRACT_DEFINED,
        ProjectStatus.PAUSED,
    )
    master = CountingPort()
    review = CountingPort()
    compute = CountingSeat()
    budget = wiring(database)

    run = await make_loop(
        database,
        project.project_id,
        retry=budget,
        master=master,
        review=review,
        compute_worker=compute,
    ).run()

    assert run.status is ProjectStatus.PAUSED
    assert run.halted
    assert run.rounds == 1
    assert master.turns == 0
    assert review.turns == 0
    assert compute.started == []
    assert compute.turns == []
    assert budget.store.episodes(project.project_id) == []
    assert read_project(database, project.project_id) is ProjectStatus.PAUSED
    _assert_evidence_preserved(database, project.project_id, prepared.node_id)


async def test_an_ended_project_dispatches_nothing_and_keeps_its_evidence(
    database: Database, project, prepare
) -> None:
    prepared = prepare(cleared=True)
    transition_project(database, project.project_id, ProjectStatus.CANCELLED)
    master = CountingPort()
    review = CountingPort()
    compute = CountingSeat()
    budget = wiring(database)

    run = await make_loop(
        database,
        project.project_id,
        retry=budget,
        master=master,
        review=review,
        compute_worker=compute,
    ).run()

    assert run.status is ProjectStatus.CANCELLED
    assert run.finished
    assert run.rounds == 1
    assert master.turns == 0
    assert review.turns == 0
    assert compute.started == []
    assert compute.turns == []
    assert budget.store.episodes(project.project_id) == []
    assert read_project(database, project.project_id) is ProjectStatus.CANCELLED
    _assert_evidence_preserved(database, project.project_id, prepared.node_id)


async def test_losing_the_lease_stops_before_the_next_turn(
    database: Database, project, prepare
) -> None:
    """One turn the lease allowed, then nothing; the DAG is left where it was."""
    prepared = prepare(cleared=True)
    checks = iter([True, False])

    def lease_guard() -> bool:
        return next(checks, False)

    master = CountingPort()
    review = CountingPort()
    compute = CountingSeat()
    budget = wiring(database)

    run = await make_loop(
        database,
        project.project_id,
        retry=budget,
        master=master,
        review=review,
        compute_worker=compute,
        lease_guard=lease_guard,
        max_rounds=10,
        max_stalled_rounds=3,
    ).run()

    assert run.halted
    assert run.rounds == 1, "the loop read or advanced after losing the lease"
    assert compute.started == [prepared.node_id]
    assert compute.turns == []
    assert master.turns == 0
    assert review.turns == 0
    assert budget.store.episodes(project.project_id) == []
    assert read_project(database, project.project_id) is ProjectStatus.CREATED
    _assert_evidence_preserved(database, project.project_id, prepared.node_id)


# ── The supervisor's policy wiring ─────────────────────────────────────────


def test_the_supervisor_uses_the_shipped_limits_outside_the_test_environment(
    database: Database, integration_settings: Settings
) -> None:
    development = integration_settings.model_copy(update={"env": "development"})
    supervisor = ProjectSupervisor(database=database, settings=development)

    assert supervisor.retry.policy.cooldowns_seconds == DEFAULT_COOLDOWNS
    assert supervisor.retry.policy.max_attempts == 9
    assert (
        supervisor.retry.policy.turn_timeout_seconds
        == development.model_turn_timeout_seconds
    )
    assert (
        supervisor.retry.policy.per_project_requests
        == development.model_requests_per_project
    )
    assert (
        supervisor.retry.policy.global_requests
        == development.model_requests_global
    )


def test_only_the_cooldowns_are_compressed_in_the_test_environment(
    database: Database, integration_settings: Settings
) -> None:
    """The wall clock cannot be accelerated; the budget must not be shortened."""
    supervisor = ProjectSupervisor(database=database, settings=integration_settings)

    assert supervisor.retry.policy.cooldowns_seconds == (0.0, 0.0)
    assert supervisor.retry.policy.max_attempts == 9
    assert (
        supervisor.retry.policy.turn_timeout_seconds
        == integration_settings.model_turn_timeout_seconds
    )
    assert (
        supervisor.retry.policy.per_project_requests
        == integration_settings.model_requests_per_project
    )
    assert (
        supervisor.retry.policy.global_requests
        == integration_settings.model_requests_global
    )
