"""T05: the retry budget as PostgreSQL state.

Every test here builds a *new* `RetryEpisodeStore` for each reservation. The
store holds no in-memory budget, so that is a supervisor restart in miniature:
nothing that happened before the new object existed is visible to it except the
rows, which is exactly the property AC1 needs. The clock is a value the test
passes, so a 300-second cooldown costs no wall time.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from tests.premium.T05.support import T0, FakeClock, read_project

from ravel.domain.enums import ProjectStatus, RetryEpisodeStatus, RetryQuestion
from ravel.execution.retry_policy import (
    RetryEpisodeStore,
    RetryPolicy,
)
from ravel.state.database import Database


def _reserve(
    database: Database,
    project_id: str,
    *,
    clock: FakeClock,
    problem_key: str = "ending",
    state_version: str = "v1",
    policy: RetryPolicy | None = None,
):
    """One model turn's reservation, through a store that just started.

    A fresh store per call on purpose: the store keeps nothing between calls,
    so a sequence of them is a sequence of supervisor restarts as far as the
    budget is concerned. If any of it lived in the object, this would fail.
    """
    return RetryEpisodeStore(database, policy).reserve(
        project_id=project_id,
        problem_key=problem_key,
        state_version=state_version,
        now=clock.now(),
    )


def _episode(database: Database, project_id: str, *, include_retired: bool = False):
    return RetryEpisodeStore(database).episodes(
        project_id, include_retired=include_retired
    )


# ── AC1: nine calls, across restarts ───────────────────────────────────────


def test_a_spent_budget_survives_restarts_and_stops_at_nine(database, project) -> None:
    """The row is the ceiling: nine dispatches, then nothing, however restarted."""
    clock = FakeClock()

    dispatches = []
    for _ in range(3):
        reservation = _reserve(database, project.project_id, clock=clock)
        assert reservation.dispatch, reservation.reason
        dispatches.append(reservation.attempts)

    assert dispatches == [1, 2, 3]
    opened = _episode(database, project.project_id)[0]
    assert opened.round_number == 1
    assert opened.cooldown_until == T0 + timedelta(seconds=60)

    # The third attempt ended round one. The row says COOLDOWN, and a restarted
    # store is refused rather than starting the count again.
    assert (
        _reserve(database, project.project_id, clock=clock).reason == "cooldown"
    )
    assert _episode(database, project.project_id)[0].attempts == 3

    clock.advance(seconds=60)
    fourth = _reserve(database, project.project_id, clock=clock)
    assert fourth.dispatch and fourth.attempts == 4
    assert fourth.round_number == 2, "the cooldown restarted the round count"

    assert _reserve(database, project.project_id, clock=clock).attempts == 5
    sixth = _reserve(database, project.project_id, clock=clock)
    assert sixth.attempts == 6 and sixth.round_number == 2
    assert sixth.cooldown_until == clock.moment + timedelta(seconds=300)

    clock.advance(seconds=300)
    seventh = _reserve(database, project.project_id, clock=clock)
    assert seventh.dispatch and seventh.attempts == 7 and seventh.round_number == 3
    assert _reserve(database, project.project_id, clock=clock).attempts == 8
    ninth = _reserve(database, project.project_id, clock=clock)
    assert ninth.dispatch and ninth.attempts == 9
    assert ninth.escalated_at == clock.moment
    assert ninth.escalation_event_id is not None

    # The tenth reservation, a year later, from a store that has never seen
    # this project: still the ninth attempt, still the same escalation.
    clock.advance(days=365)
    tenth = _reserve(database, project.project_id, clock=clock)
    assert not tenth.dispatch
    assert tenth.reason == "awaiting_operator"
    assert tenth.attempts == 9
    assert tenth.escalation_event_id == ninth.escalation_event_id

    rounds = _episode(database, project.project_id)
    assert len(rounds) == 1
    assert rounds[0].status is RetryEpisodeStatus.AWAITING_OPERATOR
    assert rounds[0].attempts == 9
    assert rounds[0].round_number == 3

    # A runtime stall is not a scientific outcome: the project is still CREATED.
    assert read_project(database, project.project_id) is ProjectStatus.CREATED


def test_the_attempt_is_spent_before_the_model_call(database, project) -> None:
    """A reservation that is never followed by a call has still spent its turn.

    This is the crash case: the process reserves, calls the model, and dies.
    A design that incremented *after* a successful call would see zero attempts
    on restart and could exceed the ceiling, which is what AC1 forbids.
    """
    clock = FakeClock()
    first = _reserve(database, project.project_id, clock=clock)
    assert first.dispatch and first.attempts == 1

    # No call happened. The restart sees the spent attempt.
    again = _reserve(database, project.project_id, clock=clock)
    assert again.attempts == 2
    assert again.dispatch


# ── AC2: the cooldown and the operator ─────────────────────────────────────


def test_the_cooldown_boundary_admits_the_next_attempt_and_nothing_earlier(
    database, project
) -> None:
    clock = FakeClock()
    for _ in range(3):
        assert _reserve(database, project.project_id, clock=clock).dispatch

    clock.advance(seconds=59)
    blocked = _reserve(database, project.project_id, clock=clock)
    assert not blocked.dispatch and blocked.reason == "cooldown"
    assert blocked.attempts == 3

    clock.advance(seconds=1)
    admitted = _reserve(database, project.project_id, clock=clock)
    assert admitted.dispatch
    assert admitted.attempts == 4, "the cooldown reset the spent budget"


def test_operator_retry_records_who_and_opens_a_replacement(database, project) -> None:
    """The unlock is a decision with a name on it, not a timeout."""
    clock = FakeClock()
    for _ in range(9):
        _reserve(
            database,
            project.project_id,
            clock=clock,
            policy=RetryPolicy(cooldowns_seconds=(0.0, 0.0)),
        )
    stalled = RetryEpisodeStore(database).pending_escalations(project.project_id)
    assert len(stalled) == 1 and stalled[0].attempts == 9

    replacement_clock = clock.advance(seconds=30)
    replacements = RetryEpisodeStore(database).operator_retry(
        project_id=project.project_id,
        actor="operator-alice",
        now=replacement_clock,
    )

    assert len(replacements) == 1
    replacement = replacements[0]
    assert replacement.attempts == 0
    assert replacement.round_number == 1
    assert replacement.status is RetryEpisodeStatus.ACTIVE
    assert replacement.operator_actor == "operator-alice"
    assert replacement.state_version == stalled[0].state_version
    assert replacement.escalation_event_id is None
    assert RetryEpisodeStore(database).pending_escalations(project.project_id) == []

    retired = _episode(database, project.project_id, include_retired=True)
    assert len(retired) == 2
    old = next(row for row in retired if row.status is RetryEpisodeStatus.RETIRED)
    assert old.episode_id == stalled[0].episode_id
    assert old.retired_reason == "operator_retry"
    assert old.retired_at == replacement_clock
    assert old.operator_actor == "operator-alice"
    assert old.operator_retried_at == replacement_clock
    assert old.attempts == 9, "retiring the episode erased its history"

    # The replacement is live: the next reservation dispatches attempt one.
    next_turn = _reserve(
        database,
        project.project_id,
        clock=FakeClock(moment=replacement_clock),
        policy=RetryPolicy(cooldowns_seconds=(0.0, 0.0)),
    )
    assert next_turn.dispatch and next_turn.attempts == 1
    assert next_turn.episode_id == replacement.episode_id


def test_operator_retry_without_a_named_actor_is_refused(database, project) -> None:
    _reserve(database, project.project_id, clock=FakeClock())
    with pytest.raises(ValueError):
        RetryEpisodeStore(database).operator_retry(
            project_id=project.project_id, actor="   ", now=T0
        )
    # Nothing was retired.
    open_rows = _episode(database, project.project_id)
    assert len(open_rows) == 1 and open_rows[0].attempts == 1


def test_operator_retry_can_name_one_question(database, project) -> None:
    clock = FakeClock()
    _reserve(database, project.project_id, clock=clock, problem_key="ending")
    _reserve(
        database,
        project.project_id,
        clock=clock,
        problem_key="pre-flight:node-1",
        state_version="v2",
    )

    replacements = RetryEpisodeStore(database).operator_retry(
        project_id=project.project_id,
        actor="operator-bob",
        now=clock.advance(seconds=1),
        question=RetryQuestion.PRE_FLIGHT,
    )
    assert len(replacements) == 1
    assert replacements[0].question is RetryQuestion.PRE_FLIGHT
    assert replacements[0].problem_key == "pre-flight:node-1"

    live = _episode(database, project.project_id)
    assert {row.question for row in live} == {
        RetryQuestion.ENDING,
        RetryQuestion.PRE_FLIGHT,
    }
    ending = next(row for row in live if row.question is RetryQuestion.ENDING)
    assert ending.attempts == 1, "an unrelated question's budget was reset"


# ── The automatic unlock ───────────────────────────────────────────────────


def test_a_new_state_version_supersedes_the_open_episode(database, project) -> None:
    """New scientific state is a new question, so it gets a new budget."""
    clock = FakeClock()
    for _ in range(2):
        _reserve(database, project.project_id, clock=clock, state_version="v1")

    fresh = _reserve(
        database, project.project_id, clock=clock, state_version="v2"
    )
    assert fresh.dispatch
    assert fresh.attempts == 1
    assert fresh.state_version == "v2"

    rows = _episode(database, project.project_id, include_retired=True)
    assert len(rows) == 2
    old = next(row for row in rows if row.state_version == "v1")
    assert old.status is RetryEpisodeStatus.RETIRED
    assert old.retired_reason == "state_version_changed"
    assert old.attempts == 2

    # Same version: the new episode's budget is now the authoritative one.
    assert (
        _reserve(database, project.project_id, clock=clock, state_version="v2").attempts
        == 2
    )


def test_a_question_that_stops_being_asked_loses_its_episode(database, project) -> None:
    clock = FakeClock()
    _reserve(database, project.project_id, clock=clock, problem_key="ending")
    _reserve(
        database,
        project.project_id,
        clock=clock,
        problem_key="verdict:node-1",
        state_version="v2",
    )

    store = RetryEpisodeStore(database)
    retired = store.retire_unasked(
        project_id=project.project_id,
        problem_keys={"ending"},
        now=clock.advance(seconds=1),
    )
    assert retired == 1

    live = _episode(database, project.project_id)
    assert [row.problem_key for row in live] == ["ending"]

    # An empty set means the round asked nothing at all: every live question
    # ended, which is the "master ended the project" path.
    retired = store.retire_unasked(
        project_id=project.project_id,
        problem_keys=set(),
        now=clock.advance(seconds=1),
    )
    assert retired == 1
    assert _episode(database, project.project_id) == []
    history = _episode(database, project.project_id, include_retired=True)
    assert {row.retired_reason for row in history} == {"question_ended"}
    assert all(row.status is RetryEpisodeStatus.RETIRED for row in history)


# ── One durable escalation per episode ────────────────────────────────────


def test_pending_escalations_is_one_item_across_repeated_reads(database, project) -> None:
    """A consumer that renders this list renders each stall once, not per poll."""
    clock = FakeClock()
    policy = RetryPolicy(cooldowns_seconds=(0.0, 0.0))
    for _ in range(9):
        _reserve(
            database,
            project.project_id,
            clock=clock,
            problem_key="verdict:node-1",
            state_version="v1",
            policy=policy,
        )

    observed = [
        RetryEpisodeStore(database).pending_escalations(project.project_id)
        for _ in range(5)
    ]
    assert all(len(rows) == 1 for rows in observed)
    assert len({rows[0].episode_id for rows in observed}) == 1
    assert len({rows[0].escalation_event_id for rows in observed}) == 1

    # Further polling does not mint another notification identity.
    for _ in range(3):
        blocked = _reserve(
            database,
            project.project_id,
            clock=clock,
            problem_key="verdict:node-1",
            state_version="v1",
            policy=policy,
        )
        assert blocked.reason == "awaiting_operator"
        assert blocked.escalation_event_id == observed[0][0].escalation_event_id


def test_episodes_and_escalations_are_scoped_to_their_project(
    database, project, other_project
) -> None:
    clock = FakeClock()
    policy = RetryPolicy(cooldowns_seconds=(0.0, 0.0))
    for _ in range(9):
        _reserve(
            database,
            project.project_id,
            clock=clock,
            policy=policy,
        )
    _reserve(database, other_project.project_id, clock=clock)

    store = RetryEpisodeStore(database)
    assert len(store.pending_escalations(project.project_id)) == 1
    assert store.pending_escalations(other_project.project_id) == []
    assert len(store.episodes(project.project_id)) == 1
    assert len(store.episodes(other_project.project_id)) == 1

    # An operator retry is scoped too: the other project's live episode is
    # untouched by an intervention in this one.
    store.operator_retry(
        project_id=project.project_id,
        actor="operator-carol",
        now=clock.advance(seconds=1),
    )
    assert store.pending_escalations(project.project_id) == []
    untouched = store.episodes(other_project.project_id)
    assert len(untouched) == 1 and untouched[0].attempts == 1


def test_retire_unasked_is_scoped_to_one_project(
    database, project, other_project
) -> None:
    """Ending this project's questions leaves another project's budget alone."""
    clock = FakeClock()
    _reserve(database, project.project_id, clock=clock)
    _reserve(database, other_project.project_id, clock=clock)

    retired = RetryEpisodeStore(database).retire_unasked(
        project_id=project.project_id,
        problem_keys=set(),
        now=clock.advance(seconds=1),
    )
    assert retired == 1
    assert RetryEpisodeStore(database).episodes(project.project_id) == []
    assert len(RetryEpisodeStore(database).episodes(other_project.project_id)) == 1
