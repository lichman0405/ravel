"""T05: the retry arithmetic, the state digest, and the request limiter.

None of these need PostgreSQL. The budget's *storage* is the database's job and
is asserted in `test_store`; what is asserted here is the arithmetic a row is
advanced by and the digest that decides when a question is a new question. Both
are pure functions, so they can be pinned without a clock, a session, or a
sleep.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from tests.premium.T05.support import T0

from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    NodeStatus,
    NodeType,
    ProjectStatus,
    RetryEpisodeStatus,
    ReviewOutcome,
)
from ravel.domain.project import Project
from ravel.execution.loop import Situation
from ravel.execution.retry_policy import (
    DEFAULT_COOLDOWNS,
    ModelRequestLimiter,
    RetryPolicy,
    next_attempt_state,
    problem_fingerprint,
    question_of,
    question_state_version,
    turn_gate,
)

# ── The budget's arithmetic ────────────────────────────────────────────────


def test_the_shipped_policy_is_the_task_card() -> None:
    """Three attempts a round, three rounds, 60s then 300s, 180s, 2/6."""
    policy = RetryPolicy()

    assert policy.attempts_per_round == 3
    assert policy.rounds == 3
    assert policy.cooldowns_seconds == DEFAULT_COOLDOWNS == (60.0, 300.0)
    assert policy.turn_timeout_seconds == 180.0
    assert policy.per_project_requests == 2
    assert policy.global_requests == 6
    assert policy.max_attempts == 9


def test_the_nine_attempts_move_through_two_cooldowns_and_an_operator_gate() -> None:
    """Attempts 1..9, with the round boundaries exactly where the card puts them.

    This is the sequence the database row follows, asserted without a database:
    ACTIVE within a round, COOLDOWN after the third and sixth attempts, and
    AWAITING_OPERATOR after the ninth. The cooldown is the *end* of a round, not
    a reset — the next dispatch is attempt four, still inside the same nine.
    """
    policy = RetryPolicy()
    seen: list[tuple[int, int, RetryEpisodeStatus, float | None]] = []

    for attempts in range(1, policy.max_attempts + 1):
        state = next_attempt_state(attempts, T0, policy)
        wait = (
            (state.cooldown_until - T0).total_seconds()
            if state.cooldown_until is not None
            else None
        )
        seen.append((attempts, state.round_number, state.status, wait))

    assert seen == [
        (1, 1, RetryEpisodeStatus.ACTIVE, None),
        (2, 1, RetryEpisodeStatus.ACTIVE, None),
        (3, 1, RetryEpisodeStatus.COOLDOWN, 60.0),
        (4, 2, RetryEpisodeStatus.ACTIVE, None),
        (5, 2, RetryEpisodeStatus.ACTIVE, None),
        (6, 2, RetryEpisodeStatus.COOLDOWN, 300.0),
        (7, 3, RetryEpisodeStatus.ACTIVE, None),
        (8, 3, RetryEpisodeStatus.ACTIVE, None),
        (9, 3, RetryEpisodeStatus.AWAITING_OPERATOR, None),
    ]


def test_a_cooldown_delays_the_next_attempt_and_never_resets_the_budget() -> None:
    """The gate opens when the timer expires, and the attempt is still #4."""
    policy = RetryPolicy()
    third = next_attempt_state(3, T0, policy)
    assert third.status is RetryEpisodeStatus.COOLDOWN
    assert third.cooldown_until == T0 + timedelta(seconds=60)

    assert turn_gate(third.status, third.cooldown_until, T0) == "cooldown"
    assert (
        turn_gate(third.status, third.cooldown_until, T0 + timedelta(seconds=59))
        == "cooldown"
    )
    assert (
        turn_gate(third.status, third.cooldown_until, T0 + timedelta(seconds=60))
        == "dispatch"
    )

    fourth = next_attempt_state(4, T0, policy)
    assert fourth.attempts == 4, "the cooldown reset the attempt count"
    assert fourth.round_number == 2


def test_no_amount_of_time_unlocks_the_operator_gate() -> None:
    """A timer is not an event: only an operator or a new state version unlocks."""
    ninth = next_attempt_state(9, T0, RetryPolicy())
    assert ninth.status is RetryEpisodeStatus.AWAITING_OPERATOR
    assert ninth.cooldown_until is None

    much_later = T0 + timedelta(days=3650)
    assert (
        turn_gate(ninth.status, ninth.cooldown_until, much_later)
        == "awaiting_operator"
    )
    assert (
        turn_gate(RetryEpisodeStatus.RETIRED, None, much_later) == "retired"
    )


def test_round_of_counts_from_one() -> None:
    policy = RetryPolicy()
    assert [policy.round_of(attempt) for attempt in range(1, 10)] == [
        1,
        1,
        1,
        2,
        2,
        2,
        3,
        3,
        3,
    ]


def test_a_policy_with_an_inconsistent_budget_is_refused() -> None:
    """The row's check constraints say 9/3; a policy that disagreed is refused."""
    with pytest.raises(ValueError):
        RetryPolicy(rounds=3, cooldowns_seconds=(60.0,))
    with pytest.raises(ValueError):
        RetryPolicy(cooldowns_seconds=(-1.0, 300.0))
    with pytest.raises(ValueError):
        RetryPolicy(turn_timeout_seconds=0.0)
    with pytest.raises(ValueError):
        RetryPolicy(per_project_requests=0)
    with pytest.raises(ValueError):
        RetryPolicy(global_requests=0)


def test_the_question_identity_is_stable_and_closed() -> None:
    key = "pre-flight:n2,n1"
    assert problem_fingerprint(key) == problem_fingerprint("pre-flight:n2,n1")
    assert problem_fingerprint(key) != problem_fingerprint("pre-flight:n1")
    assert len(problem_fingerprint(key)) == 64

    assert question_of("ending") is not None
    assert question_of("ending").value == "ending"
    with pytest.raises(ValueError):
        question_of("something-new:n1")


# ── The scientific state digest ────────────────────────────────────────────


def _project() -> Project:
    return Project(
        title="Catalyst screen",
        objective="Find a better dopant.",
        created_by="master",
    )


def _node(project: Project, *, node_type: NodeType = NodeType.COMPUTATION) -> DagNode:
    return DagNode.create(
        project_id=project.project_id,
        node_type=node_type,
        objective="Measure conductivity.",
        created_by="master",
    )


def _situation(
    project: Project,
    nodes: tuple[DagNode, ...],
    *,
    status: ProjectStatus = ProjectStatus.CREATED,
    pre_run: dict[str, ReviewOutcome | None] | None = None,
) -> Situation:
    return Situation(
        project=project.model_copy(update={"status": status}),
        nodes=nodes,
        pre_run=pre_run or {},
        open_deviations=(),
    )


def test_the_state_version_changes_when_the_question_s_state_changes() -> None:
    """A verdict, a contract reference, a status: each is a new question."""
    project = _project()
    node = _node(project)
    key = f"pre-flight:{node.node_id}"
    base = question_state_version(
        _situation(project, (node,), pre_run={node.node_id: None}), key
    )

    assert base == question_state_version(
        _situation(project, (node,), pre_run={node.node_id: None}), key
    ), "the digest is not deterministic across identical reads"

    moved = node.model_copy(update={"status": NodeStatus.READY})
    assert (
        question_state_version(
            _situation(project, (moved,), pre_run={node.node_id: None}), key
        )
        != base
    )

    drafted = node.model_copy(update={"decision_ref": "dec-2"})
    assert (
        question_state_version(
            _situation(project, (drafted,), pre_run={node.node_id: None}), key
        )
        != base
    )

    verdict_changed = question_state_version(
        _situation(project, (node,), pre_run={node.node_id: ReviewOutcome.FAIL}),
        key,
    )
    assert verdict_changed != base

    project_moved = question_state_version(
        _situation(
            project,
            (node,),
            status=ProjectStatus.EXECUTING,
            pre_run={node.node_id: None},
        ),
        key,
    )
    assert project_moved != base


def test_an_unrelated_node_does_not_unlock_a_question_about_another() -> None:
    """The digest is scoped to what is being asked, not to the whole project.

    Otherwise a research node finishing on the other side of the project would
    hand a stalled pre-flight question a fresh nine attempts — the budget would
    be reset by progress that has nothing to do with the question.
    """
    project = _project()
    asked = _node(project)
    unrelated = _node(project, node_type=NodeType.RESEARCH)
    key = f"pre-flight:{asked.node_id}"
    before = _situation(
        project,
        (asked, unrelated),
        pre_run={asked.node_id: None, unrelated.node_id: None},
    )
    after = _situation(
        project,
        (asked, unrelated.model_copy(update={"status": NodeStatus.READY})),
        pre_run={asked.node_id: None, unrelated.node_id: None},
    )

    assert question_state_version(before, key) == question_state_version(after, key)

    # The ending names no node, so it is about all of them, and the same change
    # does give it a new version.
    assert question_state_version(before, "ending") != question_state_version(
        after, "ending"
    )


# ── Deployment concurrency ─────────────────────────────────────────────────


async def test_a_project_never_holds_more_than_two_model_requests() -> None:
    limiter = ModelRequestLimiter(per_project=2, global_requests=6)
    inside = 0
    peak = 0
    two_inside = asyncio.Event()
    release = asyncio.Event()

    async def hold() -> None:
        nonlocal inside, peak
        async with limiter.slot("project-a"):
            inside += 1
            peak = max(peak, inside)
            if inside == 2:
                two_inside.set()
            await release.wait()
            inside -= 1

    tasks = [asyncio.create_task(hold()) for _ in range(5)]
    await asyncio.wait_for(two_inside.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    assert inside == 2, "the third concurrent request for one project was admitted"

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 2


async def test_the_deployment_never_holds_more_than_six_model_requests() -> None:
    limiter = ModelRequestLimiter(per_project=2, global_requests=6)
    inside = 0
    peak = 0
    six_inside = asyncio.Event()
    release = asyncio.Event()

    async def hold(project_id: str) -> None:
        nonlocal inside, peak
        async with limiter.slot(project_id):
            inside += 1
            peak = max(peak, inside)
            if inside == 6:
                six_inside.set()
            await release.wait()
            inside -= 1

    tasks = [
        asyncio.create_task(hold(f"project-{index}")) for index in range(8)
    ]
    await asyncio.wait_for(six_inside.wait(), timeout=2.0)
    await asyncio.sleep(0.05)
    assert inside == 6, "the seventh concurrent deployment request was admitted"

    release.set()
    await asyncio.gather(*tasks)
    assert peak == 6


# ── Configuration ──────────────────────────────────────────────────────────


def test_the_turn_limits_are_shipped_defaults() -> None:
    """The settings' defaults are the task's numbers, not values only tests set."""
    fields = Settings.model_fields
    assert fields["model_turn_timeout_seconds"].default == 180.0
    assert fields["model_requests_per_project"].default == 2
    assert fields["model_requests_global"].default == 6
