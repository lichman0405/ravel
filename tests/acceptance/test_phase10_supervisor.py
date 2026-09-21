"""Phase 10 items about the supervisor: discovery, isolation, recovery, chaos.

Phase 10's whole point is that nobody drives a project by hand any more. The
supervisor is what replaces that hand: it reads PostgreSQL, finds the projects
that are still going, and gives each one a loop.

What is scripted here is the five seats — Master, Review and the two Workers —
because a test about *discovery and isolation* should not depend on a model
being in a good mood. Everything between the seats is the real thing: the real
`DshRuntimePool`, the real `TemporalNodeRuns`, the real `ProjectLoop`, the real
mock backends behind a real worker, and the real DAG services the seats write
through. The supervisor is started the way a deployment starts it — `run()`,
its own tick loop and all — and stopped the way a deployment stops it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from tests.acceptance.phase10_support import (
    FakeHarness,
    ScriptedSeats,
    ScriptedWorker,
    patched_agents,
    project_status,
    second_project,
    until,
)
from tests.e2e.conftest import Headless, LiveGateway, Task
from tests.e2e.test_tui import World, console
from tests.integration.conftest import DEFAULT_OUTPUTS, Prepared

from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, ProjectStatus
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.dsh import runtime as runtime_module
from ravel.dsh.pool import DshRuntimePool
from ravel.execution.loop import Situation
from ravel.execution.supervisor import ProjectSupervisor
from ravel.execution.temporal.contracts import ExternalResult
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import RecordRepositories

pytestmark = [pytest.mark.phase10, pytest.mark.timeout(600)]

MEASURE = "Measure conductivity across the dopant series."
ANALYSE = "Fit the conductivity model to the measurements."

#: What a computation node is asked to produce, in the one case that starts a
#: node by hand rather than letting a scripted Master plan it.
COMPUTE_OUTPUTS = ("conductivity.csv", "notes.json")

#: What an experiment node is asked to produce, for the cases about the seat
#: that serves one.
LAB_OUTPUTS = ("experiment_log", "raw_data")

#: A loop ceiling a case will not reach. The cases that look at a seat look at a
#: deployment that is still working, and a loop that halted and was re-driven
#: underneath them — which is what the default ceiling does in four seconds —
#: would have them asserting about a churn the test harness caused.
LOOP_CEILING = 1_000_000

#: How long the compute mock holds each state, in the cases where the task has to
#: still be running when the case looks at it. The mock advances on wall clock,
#: so at the default a `COMPUTE_SUCCESS` computation is RUNNING for a fraction of
#: a second — less than the time between starting a run and starting a supervisor.
STILL_RUNNING = 3_600.0

#: The statuses a project never leaves.
TERMINAL = frozenset(
    {
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.INCONCLUSIVE,
        ProjectStatus.CANCELLED,
    }
)


def task(project_id: str, name: str, objective: str) -> Task:
    """One node for a scripted Master to plan, named so a test can find it."""

    def build() -> DagNode:
        return DagNode.create(
            project_id=project_id,
            node_type=NodeType.COMPUTATION,
            objective=f"{name}: {objective}",
            created_by="script",
        )

    return Task(build=build)


@dataclass
class SupervisorRun:
    """A supervisor running as its own task, and what a test can ask of it."""

    supervisor: ProjectSupervisor
    task: asyncio.Task[None]

    def driving(self) -> set[str]:
        """The projects this supervisor's tick has given a loop to."""
        return set(self.supervisor._tasks)

    @property
    def pool(self) -> DshRuntimePool | None:
        """Every seat's runtime, shared, or nothing before the supervisor starts."""
        return self.supervisor.pool

    def scopes(self) -> set[tuple[str, str]]:
        """The `(project, role)` scopes the pool holds, empty before it is built."""
        pool = self.pool
        return set(pool.stats().scopes) if pool is not None else set()

    def status(self, project_id: str) -> ProjectStatus:
        return project_status(self.supervisor.database, project_id)

    async def finished(self, project_id: str, *, timeout: float = 120.0) -> None:
        """Wait for a project to end and its loop to be reaped."""
        await until(
            lambda: project_id not in self.driving() and self.status(project_id) in TERMINAL,
            timeout=timeout,
        )

    async def stop(self) -> None:
        self.supervisor.stop()
        await asyncio.wait_for(self.task, timeout=60.0)


@asynccontextmanager
async def supervising(
    headless: Headless,
    monkeypatch: pytest.MonkeyPatch,
    seats: ScriptedSeats | None,
    settings: Settings | None = None,
    *,
    loop_max_rounds: int = 200,
) -> AsyncIterator[SupervisorRun]:
    """Start a supervisor over the real stack, with the five seats scripted.

    `seats=None` is the other half of the same idea: the supervisor builds the
    agents a deployment gets, over its own real pool, and only the harness
    process underneath them is stood in for. That is what makes the *seats* the
    subject rather than what the seats were told to do.

    `settings` is for those cases: a runtime writes a working directory the
    moment a scope is created, and a case that used the deployment's root would
    leave one behind in the checkout.

    `loop_max_rounds` is the ceiling on the loop a project gets. At the default
    poll a 200-round loop is about four seconds of wall clock, which is a test's
    speed rather than a deployment's — a case that looks at a task still in
    flight needs the deployment to still be there when it looks, so it passes a
    ceiling the loop will not reach while the case runs. The supervisor re-drives
    a halted loop on its next tick, so a low ceiling does not end anything; it
    churns the seats, and churn is what such a case must not be measuring.
    """
    with patched_agents(monkeypatch, seats) if seats is not None else nullcontext():
        supervisor = ProjectSupervisor(
            database=headless.database,
            settings=settings or headless.settings,
            poll_seconds=0.05,
            loop_poll_seconds=0.02,
            loop_max_rounds=loop_max_rounds,
        )
        task = asyncio.create_task(supervisor.run())
        run = SupervisorRun(supervisor=supervisor, task=task)
        try:
            yield run
        finally:
            if task.done():
                task.result()  # a supervisor that died early is a test failure
            else:
                await run.stop()


# ── P10-12 ─────────────────────────────────────────────────────────────────────


async def test_p10_12_the_supervisor_finds_and_drives_active_projects(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-12: a project nobody named is discovered, driven, and finished.

    Nothing here passes a project identifier to the supervisor: it reads
    PostgreSQL, decides which projects are still going, and gives each one a
    loop. The assertion is that the project reached an ending, which is a fact
    only a loop can produce — and that a Worker took a turn, which is the
    difference between this and the loop Phase 9 had.
    """
    headless.compute("COMPUTE_SUCCESS")
    project = headless.project
    seats.plan(project, (task(project.project_id, "measure", MEASURE),))

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(project.project_id)

    assert run.status(project.project_id) is ProjectStatus.COMPLETED
    assert seats.plans[project.project_id].master.trace == [
        "contract_defined",
        "planned",
        "concluded:SUCCESS",
    ]
    assert seats.turns_for(project.project_id, AgentRole.COMPUTE_WORKER), (
        "the Compute Worker took no turn, so four seats ran and not five"
    )


# ── P10-12, second case ─────────────────────────────────────────────────────────────────────


async def test_p10_12_one_project_failing_does_not_stop_the_others(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-12: a loop that dies is one project's problem, not the supervisor's.

    The broken project's Master raises on its first turn. The supervisor is
    expected to notice the dead task, log it, and keep driving the project that
    is still working — a supervisor that stopped would turn one project's fault
    into every project's outage.
    """
    headless.compute("COMPUTE_SUCCESS")
    healthy = headless.project
    broken = second_project(headless.database, title="Broken")
    seats.plan(healthy, (task(healthy.project_id, "measure", MEASURE),))
    seats.plan(broken, (task(broken.project_id, "measure", MEASURE),))

    class Exploding:
        """A Master whose every turn fails."""

        async def act(self, _situation: object) -> None:
            raise RuntimeError("this Master is broken")

        def close(self) -> None:
            pass

    seats.override(broken.project_id, AgentRole.MASTER, Exploding())

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(healthy.project_id)

    assert run.status(healthy.project_id) is ProjectStatus.COMPLETED
    assert run.status(broken.project_id) is not ProjectStatus.COMPLETED


# ── P10-12, the other half of discovery ─────────────────────────────────────────


async def test_p10_12_paused_and_ended_projects_are_left_alone(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-12: the supervisor's discovery is a filter, not a sweep.

    A paused project is a human's decision about that project, and an ended one
    is over. Neither may be picked up: driving a paused project would override
    the one control an operator has, and re-driving an ended one would restart
    science that has already concluded.
    """
    live = headless.project
    paused = second_project(headless.database, title="Paused")
    ended = second_project(headless.database, title="Ended")
    for project in (live, paused, ended):
        seats.plan(project, (task(project.project_id, "measure", MEASURE),))

    with headless.database.transaction() as session:
        registry = ProjectRegistry(session)
        registry.transition(
            paused.project_id,
            ProjectStatus.CONTRACT_DEFINED,
            actor_id="owner",
            reason="Its contract is frozen; the work has not started.",
        )
        registry.transition(
            paused.project_id,
            ProjectStatus.PAUSED,
            actor_id="owner",
            reason="The operator paused this one.",
        )
        registry.transition(
            ended.project_id,
            ProjectStatus.CANCELLED,
            actor_id="owner",
            reason="This one was abandoned.",
        )

    async with supervising(headless, monkeypatch, seats) as run:
        await until(lambda: run.driving() == {live.project_id})
        assert run.status(paused.project_id) is ProjectStatus.PAUSED
        assert run.status(ended.project_id) is ProjectStatus.CANCELLED


# ── P10-12, after a restart ─────────────────────────────────────────────────────


async def test_p10_12_a_restarted_supervisor_resumes_from_postgres(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-12: killing the supervisor loses the process, not the project.

    The first supervisor is stopped while the project is mid-run. A second one
    is started over the same database and must pick the project up from where
    the record says it is: the node that was running is still running, its run
    is still Temporal's, and the project still ends.

    "Mid-run" is waited for rather than assumed. EXECUTING is the project's own
    statement that its first node began, and nothing but the DAG produces it —
    so a supervisor that had only discovered the project and written its success
    contract would be stopped before there was a run to resume.
    """
    headless.compute("COMPUTE_SUCCESS")
    project = headless.project
    seats.plan(project, (task(project.project_id, "measure", MEASURE),))

    async with supervising(headless, monkeypatch, seats) as first:
        await until(
            lambda: first.driving() == {project.project_id}
            and first.status(project.project_id) is ProjectStatus.EXECUTING
        )

    # Nothing about the project lived in that process: it is all in PostgreSQL.
    assert first.status(project.project_id) is ProjectStatus.EXECUTING

    async with supervising(headless, monkeypatch, seats) as second:
        await second.finished(project.project_id)

    assert second.status(project.project_id) is ProjectStatus.COMPLETED


# ── P10-12, when nobody answers ────────────────────────────────────────────────

#: How many times the loop asks one question before it stops asking. The
#: shipped default of `ProjectLoop.max_turns_per_question`, written out because
#: this case is about what that number costs: a change to it changes what an
#: unanswered question costs an unattended deployment, and this is where that
#: shows up.
TURNS_PER_QUESTION = 3


class ReviewThatNeverAnswers:
    """Review, scripted to take every turn it is given and write nothing.

    The failure this models is the quiet one: a turn that *succeeds* and submits
    no verdict, which is what a model that answered in prose, or one whose write
    was refused, looks like from the loop's side. A Review that raised would end
    the attempt through `_advance`, which the supervisor already handles; this
    one leaves the condition that asks the question exactly as it was.
    """

    def __init__(self) -> None:
        #: Every turn the loop asked for, and what it had asked by the time each
        #: attempt ended. The snapshots are taken inside the supervisor's own
        #: flow, so a case that reads them cannot be racing the next attempt.
        self.turns = 0
        self.ended_with: list[int] = []

    async def act(self, situation: Situation) -> None:
        _ = situation
        self.turns += 1

    def close(self) -> None:
        """End one attempt, and note what it cost.

        The supervisor closes each seat when its loop returns — one loop, one
        close — which is what makes this the attempt counter: the case can tell
        three turns in one attempt from one turn in each of three attempts
        without watching a clock.
        """
        self.ended_with.append(self.turns)


async def test_p10_12_a_question_nobody_answers_is_asked_a_bounded_number_of_times(
    headless: Headless,
    seats: ScriptedSeats,
    monkeypatch: pytest.MonkeyPatch,
    prepare: Callable[..., Prepared],
) -> None:
    """P10-12: what an unanswered question costs is bounded, and the retry is too.

    One node waiting to be cleared, and a Review that takes every turn it is
    offered and submits nothing. Dispatch fires whenever the condition holds, so
    this used to be a model call *per round* for as long as the node waited: an
    unattended deployment's worst case was a bill rather than a stall, and it is
    the case Phase 10 exists to make survivable.

    What is asserted is the shape rather than a number of calls: each attempt
    asks the question `max_turns_per_question` times however many rounds it ran,
    the loop then ends as halted — which is RAVEL's word for "this project has
    stopped", the same ending a stopped project already got — and the
    supervisor's re-drive is a *fresh* bounded attempt rather than a
    continuation of the old one. Three attempts are watched, because one says
    nothing about whether the second inherits a spent budget or a fresh one.
    """
    project = headless.project
    waiting = prepare(node_type=NodeType.COMPUTATION, cleared=False)
    seats.plan(project, (task(project.project_id, "measure", MEASURE),))
    review = ReviewThatNeverAnswers()
    seats.override(project.project_id, AgentRole.REVIEW, review)

    async with supervising(headless, monkeypatch, seats) as run:
        await until(lambda: len(review.ended_with) >= 3)
        status = run.status(project.project_id)

    assert review.ended_with[:3] == [
        TURNS_PER_QUESTION,
        2 * TURNS_PER_QUESTION,
        3 * TURNS_PER_QUESTION,
    ], (
        "an attempt asked a question nobody answers some other number of times "
        f"than its budget: {review.ended_with}"
    )
    assert status is not ProjectStatus.PAUSED and status not in TERMINAL, (
        f"the project ended at {status.value}, so the supervisor stopped retrying "
        "a project that had not been decided about"
    )
    with headless.database.read_only() as session:
        still_waiting = DagRepository(session, project.project_id).node(
            waiting.node_id
        ).status
    assert still_waiting is NodeStatus.READY, (
        "a verdict was applied by a Review that submitted none"
    )


# ── P10-16 ─────────────────────────────────────────────────────────────────────


async def test_p10_16_two_projects_run_at_once_without_touching_each_other(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-16: two projects, two loops, one database, nothing shared but the stack.

    Both are planned, both run, both end — and each one's nodes belong to it. A
    supervisor that leaked a project identifier between loops would show it
    here: the rows would exist under the wrong project, or the second project
    would never move at all.
    """
    headless.compute("COMPUTE_SUCCESS")
    first = headless.project
    second = second_project(headless.database, title="Second")
    seats.plan(first, (task(first.project_id, "measure", MEASURE),))
    seats.plan(second, (task(second.project_id, "analyse", ANALYSE),))

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(first.project_id)
        await run.finished(second.project_id)

    assert run.status(first.project_id) is ProjectStatus.COMPLETED
    assert run.status(second.project_id) is ProjectStatus.COMPLETED

    markers = {first.project_id: "measure", second.project_id: "analyse"}
    for project_id, marker in markers.items():
        with headless.database.read_only() as session:
            nodes = DagRepository(session, project_id).nodes()
        assert nodes, f"{project_id} has no nodes"
        assert all(node.project_id == project_id for node in nodes), (
            "a node was written under the wrong project"
        )
        assert all(marker in node.objective for node in nodes), (
            f"{project_id} is holding another project's work"
        )


# ── P10-12, the closing case ─────────────────────────────────────────────────────────────────────


async def test_p10_12_an_unattended_run_exercises_all_five_seats(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-12: one unattended run, five agents, one ending.

    A stage of two nodes, planned and answered by Master, cleared and judged by
    Review, run on a real backend, and monitored by a real Worker turn on the
    node that was live. No hand drove it: the supervisor found the project, and
    the project ended because the seats moved it there.
    """
    headless.compute("COMPUTE_SUCCESS")
    project = headless.project
    planned = (
        task(project.project_id, "measure", MEASURE),
        task(project.project_id, "analyse", ANALYSE),
    )
    plan = seats.plan(project, planned)

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(project.project_id)

    assert run.status(project.project_id) is ProjectStatus.COMPLETED
    assert plan.master.trace == ["contract_defined", "planned", "concluded:SUCCESS"]

    with headless.database.read_only() as session:
        nodes = DagRepository(session, project.project_id).nodes()
    assert len(nodes) == 2, "the stage Master planned is not the stage that ran"
    assert all(node.status is NodeStatus.PASSED for node in nodes)

    worker_turns = seats.turns_for(project.project_id, AgentRole.COMPUTE_WORKER)
    assert worker_turns, "the Compute Worker never took a turn"
    assert {node_id for node_id, _status in worker_turns} == {
        node.node_id for node in nodes
    }, "the Worker was given a turn on a node that was not its own"


# ── P10-W01 / P10-W02 / P10-W03 ─────────────────────────────────────────────────


class RecordingReview:
    """Review, and a note of what the Worker had been asked by the time it ran.

    The ordering between the two powers is the claim: a result reaches Review
    through the Worker's hands, so every node Review is asked to judge must
    already appear in the Worker's turn list. What is recorded is the Worker's
    list as it stood at each Review round rather than a flag, so a violation
    says which node arrived unescorted.
    """

    def __init__(self, inner: Any, worker: ScriptedWorker) -> None:
        self.inner = inner
        self.worker = worker
        self.seen: list[tuple[list[tuple[str, str]], list[str]]] = []

    async def act(self, situation: Situation) -> None:
        self.seen.append(
            (
                list(self.worker.turns),
                [
                    node.node_id
                    for node in situation.nodes
                    if node.status is NodeStatus.REVIEWING
                ],
            )
        )
        await self.inner.act(situation)

    def close(self) -> None:
        """The scripted Review below holds no runtime; there is nothing to close."""


async def test_p10_08_no_node_reaches_review_without_a_worker_turn(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-08: the road to Review runs through a Worker.

    The loop dispatches in the order a node lives: the run starts, the Worker is
    given the node while it is in somebody's hands, and only then does a verdict
    get asked for. Reversing that would leave the Worker as decoration — a seat
    that exists in the roster and is never in the path a result takes — and the
    project would still reach COMPLETED, which is why the order is asserted
    rather than the ending.

    Review is what watches, because Review is the power on the far side: at each
    of its rounds, every node it is being asked to judge has already been served
    a Worker turn.
    """
    headless.compute("COMPUTE_SUCCESS")
    project = headless.project
    plan = seats.plan(project, (task(project.project_id, "measure", MEASURE),))
    review = RecordingReview(
        plan.review, seats.worker(project.project_id, AgentRole.COMPUTE_WORKER)
    )
    seats.override(project.project_id, AgentRole.REVIEW, review)

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(project.project_id)

    assert run.status(project.project_id) is ProjectStatus.COMPLETED
    assert review.seen, "Review was never asked for a verdict, so this proved nothing"
    for served, judging in review.seen:
        asked = {node_id for node_id, _status in served}
        assert set(judging) <= asked, (
            f"Review was handed {sorted(set(judging) - asked)} without a Worker turn "
            "on it, so a result reached a verdict without passing through its Worker"
        )


# ── P10-W20 ─────────────────────────────────────────────────────────────────────


async def test_p10_w20_no_run_bypasses_the_worker_that_owns_it(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-W20: work does not happen except through the Worker that owns it.

    P10-08 asserts the *order* — a result reaches Review after a Worker turn —
    and order alone would be satisfied by a scheduler that started runs and told
    the Worker afterwards. This is the stronger claim and the one Phase 10H is
    about: there is exactly one door into a run, it is the Worker's own tool, and
    so every node that has an Execution Record is a node a Worker began.

    Asserted against the record rather than against the tool's transcript,
    because a bypass would not announce itself: the question is whether any run
    exists that the seat's `started` list does not account for. The seat is the
    real one — its `start` calls the real `start_execution` handler — so a
    supervisor that started runs by any other route would show up here as an
    execution its Worker never asked for.
    """
    headless.compute("COMPUTE_SUCCESS")
    project = headless.project
    planned = (
        task(project.project_id, "measure", MEASURE),
        task(project.project_id, "analyse", ANALYSE),
    )
    seats.plan(project, planned)

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(project.project_id)

    assert run.status(project.project_id) is ProjectStatus.COMPLETED

    with headless.database.read_only() as session:
        nodes = DagRepository(session, project.project_id).nodes()
        records = RecordRepositories(session, project.project_id)
        ran = {
            node.node_id: bool(records.executions.for_node(node.node_id))
            for node in nodes
        }
    assert any(ran.values()), "nothing ran, so there is no run to account for"

    started = set(seats.worker(project.project_id, AgentRole.COMPUTE_WORKER).started)
    unaccounted = sorted(node_id for node_id, executed in ran.items() if executed) 
    unaccounted = [node_id for node_id in unaccounted if node_id not in started]
    assert not unaccounted, (
        f"{unaccounted} produced an Execution Record without the Compute Worker "
        "ever starting them, so a run was reached through some other door"
    )


# ── P10-10 / P10-11 ─────────────────────────────────────────────────────────────


async def test_p10_10_a_dead_worker_does_not_lose_the_run(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-10: a Worker session that dies mid-run costs the session, not the work.

    This is the failure a Worker agent invites and must not be able to cause: an
    agent sitting next to a run is a process that can die while the run is in
    flight. What makes that survivable is what the Worker is *not*: the job
    belongs to Temporal, the record to PostgreSQL, and the seat's identity to
    `(project, role)` — so a dead session is replaced, the run finishes, the
    Execution Records are written, and the project ends.

    The death is on the first turn, which is the one covering both nodes of the
    stage, and the assertion is that both nodes ended up run and judged anyway.
    """
    headless.compute("COMPUTE_SUCCESS")
    project = headless.project
    planned = (
        task(project.project_id, "measure", MEASURE),
        task(project.project_id, "analyse", ANALYSE),
    )
    seats.plan(project, planned)

    deaths: list[str] = []

    async def dies_on_its_first_turn(node: DagNode, situation: Situation) -> None:
        _ = situation
        if not deaths:
            deaths.append(node.node_id)
            raise RuntimeError("the Compute Worker's session died mid-run")

    seats.behaviors[AgentRole.COMPUTE_WORKER] = dies_on_its_first_turn

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(project.project_id)

    assert deaths, "the Worker never died, so this case proved nothing"
    assert run.status(project.project_id) is ProjectStatus.COMPLETED, (
        "a Worker session dying ended a project whose work was never the Worker's "
        "to lose"
    )

    with headless.database.read_only() as session:
        nodes = DagRepository(session, project.project_id).nodes()
        records = RecordRepositories(session, project.project_id)
        executions = {
            node.node_id: records.executions.for_node(node.node_id) for node in nodes
        }

    assert all(node.status is NodeStatus.PASSED for node in nodes), (
        "a node that was in flight when the session died did not reach its verdict"
    )
    assert all(executions[node.node_id] for node in nodes), (
        "a run was lost with the session that was watching it"
    )

    worker = seats.worker(project.project_id, AgentRole.COMPUTE_WORKER)
    assert set(worker.nodes) == {node.node_id for node in nodes}, (
        "the seat's identity did not outlive the session that died: the replacement "
        "was not given both nodes"
    )


class _DiesOnce:
    """A Master whose session dies on its first turn, and is replaced.

    The seat is the same object across both loops, because a seat's identity in
    this system is `(project, role)` and not a session: what the second loop
    builds is a new session for the same Master, which is exactly what a
    replacement is.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.attempts = 0
        self.deaths = 0

    async def act(self, situation: Situation) -> None:
        self.attempts += 1
        if self.deaths == 0:
            self.deaths += 1
            raise RuntimeError("the Master's session died")
        await self.inner.act(situation)

    def close(self) -> None:
        """The script underneath holds no runtime; there is nothing to close."""


async def test_p10_11_a_dead_master_does_not_lose_the_project(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P10-11: a Master session that dies is replaced and the project goes on.

    A dead Master takes its loop down with it, because a loop whose decision
    power raises has nothing to sequence. That is a failure of one *process*,
    and the supervisor's answer is the one it gives every project: the record is
    what the project is, so a loop that died is restarted from PostgreSQL, with
    a new Master session under the same Master identity.

    The project here has not been planned yet when the first session dies, which
    is the worst case: nothing about it exists except its row. The replacement
    plans it, runs it, reviews it and ends it.
    """
    headless.compute("COMPUTE_SUCCESS")
    project = headless.project
    plan = seats.plan(project, (task(project.project_id, "measure", MEASURE),))
    master = _DiesOnce(plan.master)
    seats.override(project.project_id, AgentRole.MASTER, master)

    async with supervising(headless, monkeypatch, seats) as run:
        await run.finished(project.project_id)

    assert master.deaths == 1, "the Master never died, so this case proved nothing"
    assert master.attempts >= 2, "the dead Master was never replaced"
    assert run.status(project.project_id) is ProjectStatus.COMPLETED, (
        "a Master session dying ended a project that was entirely in PostgreSQL"
    )
    assert plan.master.trace == ["contract_defined", "planned", "concluded:SUCCESS"], (
        "the replacement Master did not carry the project through its own stage"
    )


# ── P10-W01 / P10-W02 / P10-W03, the real seats ─────────────────────────────────


def prompts_about(node_id: str) -> list[str]:
    """Every turn a harness process was handed that named one node.

    Read off the processes rather than off an agent's memory, because the claim
    is about what actually reached a model: a prompt that was built and never
    sent would satisfy an assertion about the agent and prove nothing.
    """
    return [
        prompt
        for process in FakeHarness.instances
        for prompt in process.prompts
        if node_id in prompt
    ]


async def a_node_in_flight(
    headless: Headless,
    prepare: Callable[..., Prepared],
    *,
    node_type: NodeType,
    outputs: tuple[str, ...],
    status: NodeStatus,
) -> Prepared:
    """A node started the way the runtime starts one, left where it is.

    Started through the real client rather than by a Worker seat, because what
    these cases are about is the seat that *serves* a task: a node whose run a
    Worker began and a node whose run began before the worker process existed
    are the same node as far as the seat is concerned.
    """
    prepared = prepare(node_type=node_type, required_outputs=outputs)
    await headless.client.start_node_run(
        project_id=headless.project.project_id,
        node_id=prepared.node_id,
        actor_id="phase10-worker",
        execution_contract_version=prepared.contract.version,
    )
    await until(lambda: headless.status_of(prepared.node) is status)
    return prepared


def seat_assertions(
    runtime: Any,
    turns: list[str],
    settings: Settings,
    project_id: str,
    role: AgentRole,
) -> None:
    """What is true of a Worker seat a supervisor built for one role.

    Four facts, and they are one fact: the seat is the `(project, role)` scope
    the pool holds, it was launched under a brief naming that scope, it wrote
    inside the runtime root this deployment configures, and the turn it sent its
    model said which role it was serving as. A seat that failed any of them
    would be a session reaching work by some route other than its own scope.
    """
    assert json.loads(runtime.brief_path.read_text(encoding="utf-8")) == {
        "project_id": project_id,
        "role": role.value,
    }, "the seat was launched under a scope that is not the one it serves"
    assert runtime.work_dir.is_relative_to(settings.runtime_path("dsh_cwd")), (
        "the seat wrote outside the runtime root the deployment configures"
    )
    assert turns, "the seat never sent its model a turn about its own node"
    assert f"You are serving as {role.display_name} for one task." in turns[0], (
        "the turn did not tell the agent which role it was serving as"
    )


async def test_p10_w01_the_compute_workers_seat_is_a_dsh_session(
    headless: Headless,
    prepare: Callable[..., Prepared],
    execution_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P10-W01: the Compute Worker the supervisor builds is a harness session.

    Nothing is scripted here except the harness process itself. The supervisor
    builds the agents a deployment gets — `HarnessAgent` and `WorkerAgent` — over
    its own real `DshRuntimePool`, and the computation is put in flight so that
    the Compute Worker owes a turn. It is still in flight while the seat is
    asserted: a task that finished before the supervisor's first read would owe
    nobody anything, and the case would be asserting about a run that is over.
    """
    FakeHarness.instances.clear()
    monkeypatch.setattr(runtime_module, "DeepSeekHarness", FakeHarness)
    headless.compute("COMPUTE_SUCCESS", step_seconds=STILL_RUNNING)

    prepared = await a_node_in_flight(
        headless,
        prepare,
        node_type=NodeType.COMPUTATION,
        outputs=COMPUTE_OUTPUTS,
        status=NodeStatus.RUNNING,
    )
    project_id = headless.project.project_id
    settings = execution_settings.model_copy(update={"runtime_dir": tmp_path / "runtime"})

    async with supervising(
        headless,
        monkeypatch,
        seats=None,
        settings=settings,
        loop_max_rounds=LOOP_CEILING,
    ) as run:
        await until(
            lambda: (project_id, AgentRole.COMPUTE_WORKER.value) in run.scopes()
        )
        pool = run.pool
        assert pool is not None, "the supervisor is driving without a pool"
        runtime = pool.live_runtime(project_id, AgentRole.COMPUTE_WORKER)
        assert runtime is not None, "the seat was given a scope the pool does not hold"
        turns = prompts_about(prepared.node_id)
        assert turns, "the seat never sent its model a turn about its own node"

    seat_assertions(runtime, turns, settings, project_id, AgentRole.COMPUTE_WORKER)


async def test_p10_w02_the_experimental_workers_seat_is_a_dsh_session(
    headless: Headless,
    prepare: Callable[..., Prepared],
    execution_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P10-W02: the Experimental Worker the supervisor builds is a harness session.

    The other half of W01, and not a restatement of it: this seat serves a
    different node type, is launched with a different scope, and owes its turn
    on a node that is *waiting on a bench* rather than one that is running. A
    deployment that built four seats and wired one of them twice would pass W01
    and fail here.
    """
    FakeHarness.instances.clear()
    monkeypatch.setattr(runtime_module, "DeepSeekHarness", FakeHarness)
    headless.lab("LAB_LONG_WAIT")

    prepared = await a_node_in_flight(
        headless,
        prepare,
        node_type=NodeType.EXPERIMENT,
        outputs=LAB_OUTPUTS,
        status=NodeStatus.WAITING_EXTERNAL,
    )
    project_id = headless.project.project_id
    settings = execution_settings.model_copy(update={"runtime_dir": tmp_path / "runtime"})

    async with supervising(
        headless,
        monkeypatch,
        seats=None,
        settings=settings,
        loop_max_rounds=LOOP_CEILING,
    ) as run:
        await until(
            lambda: (project_id, AgentRole.EXPERIMENTAL_WORKER.value) in run.scopes()
        )
        pool = run.pool
        assert pool is not None, "the supervisor is driving without a pool"
        runtime = pool.live_runtime(project_id, AgentRole.EXPERIMENTAL_WORKER)
        assert runtime is not None, "the seat was given a scope the pool does not hold"
        turns = prompts_about(prepared.node_id)
        assert turns, "the seat never sent its model a turn about its own node"

    seat_assertions(runtime, turns, settings, project_id, AgentRole.EXPERIMENTAL_WORKER)


async def test_p10_w03_the_two_seats_are_two_scoped_identities(
    headless: Headless,
    prepare: Callable[..., Prepared],
    execution_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P10-W03: one project, two Workers, two identities that do not merge.

    Both benches are busy at once, so both seats are live under one supervisor
    at the same time — which is when "task-scoped identity" stops being a
    definition and becomes a question about two things that exist together. The
    claim is that each seat is its own scope with its own working directory, and
    that neither seat's model is told about the other's task: a Worker that
    could see the project's other work would be holding the plan, and holding
    the plan is what W04 says a Worker does not do.

    Both nodes are still in flight while the seats are asserted, which is what
    makes it "at once": the bench waits on an operator, and the computation is
    given a timeline long enough that it has not finished by the time the second
    node is started, let alone by the time a supervisor reads the DAG. A
    computation that ended before the supervisor's first round would leave one
    seat that never existed, and the case would be measuring the mock's clock.
    """
    FakeHarness.instances.clear()
    monkeypatch.setattr(runtime_module, "DeepSeekHarness", FakeHarness)
    headless.lab("LAB_LONG_WAIT")
    headless.compute("COMPUTE_SUCCESS", step_seconds=STILL_RUNNING)

    mine = await a_node_in_flight(
        headless,
        prepare,
        node_type=NodeType.COMPUTATION,
        outputs=COMPUTE_OUTPUTS,
        status=NodeStatus.RUNNING,
    )
    theirs = await a_node_in_flight(
        headless,
        prepare,
        node_type=NodeType.EXPERIMENT,
        outputs=LAB_OUTPUTS,
        status=NodeStatus.WAITING_EXTERNAL,
    )
    project_id = headless.project.project_id
    settings = execution_settings.model_copy(update={"runtime_dir": tmp_path / "runtime"})

    async with supervising(
        headless,
        monkeypatch,
        seats=None,
        settings=settings,
        loop_max_rounds=LOOP_CEILING,
    ) as run:
        await until(
            lambda: {
                (project_id, AgentRole.COMPUTE_WORKER.value),
                (project_id, AgentRole.EXPERIMENTAL_WORKER.value),
            }
            <= run.scopes()
        )
        pool = run.pool
        assert pool is not None, "the supervisor is driving without a pool"
        compute = pool.live_runtime(project_id, AgentRole.COMPUTE_WORKER)
        experimental = pool.live_runtime(project_id, AgentRole.EXPERIMENTAL_WORKER)
        assert compute is not None and experimental is not None, (
            "both benches are live, so both seats are serving"
        )
        compute_turns = prompts_about(mine.node_id)
        lab_turns = prompts_about(theirs.node_id)
        assert compute_turns and lab_turns, "a live task went unserved"

    assert compute is not experimental
    assert compute.work_dir != experimental.work_dir, (
        "two seats share one working directory, so neither is scoped to its own task"
    )
    seat_assertions(compute, compute_turns, settings, project_id, AgentRole.COMPUTE_WORKER)
    seat_assertions(
        experimental, lab_turns, settings, project_id, AgentRole.EXPERIMENTAL_WORKER
    )

    assert theirs.node_id not in compute_turns[0], (
        "the Compute Worker was told about the lab's task; a Worker is handed one "
        "node, and the rest of the plan is not its business"
    )
    assert mine.node_id not in lab_turns[0], (
        "the Experimental Worker was told about the computation"
    )


# ── P10-W16 ─────────────────────────────────────────────────────────────────────


async def test_p10_w16_a_wait_needs_no_live_turn_and_no_process(
    headless: Headless,
    prepare: Callable[..., Prepared],
    execution_settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P10-W16: a bench that will answer in three days costs RAVEL nothing to wait for.

    W02 shows the seat taking a real turn on a node that is waiting on a lab.
    This is what that wait costs afterwards, and it is the whole reason
    `WAITING_EXTERNAL` is a status rather than a loop: the wait is served once
    and then holds nothing at all — not a turn, and not a process.

    So the deployment is taken away mid-wait. The supervisor is stopped and its
    pool closed, which is the strongest form of "no resident process": there is
    no RAVEL agent process left at all, and the node's task is still waiting on
    the bench exactly as it was. Days pass in that gap. The event then arrives
    and the run picks up from the record: the node leaves the wait and the
    delivery is written down, with no session anywhere near it. That is what
    P10-14's "the wait ends by an event" means when it is asked of the process
    rather than of the loop.
    """
    FakeHarness.instances.clear()
    monkeypatch.setattr(runtime_module, "DeepSeekHarness", FakeHarness)
    headless.lab("LAB_LONG_WAIT")

    prepared = await a_node_in_flight(
        headless,
        prepare,
        node_type=NodeType.EXPERIMENT,
        outputs=LAB_OUTPUTS,
        status=NodeStatus.WAITING_EXTERNAL,
    )
    project_id = headless.project.project_id
    settings = execution_settings.model_copy(update={"runtime_dir": tmp_path / "runtime"})

    async with supervising(
        headless,
        monkeypatch,
        seats=None,
        settings=settings,
        loop_max_rounds=LOOP_CEILING,
    ) as run:
        await until(lambda: (project_id, AgentRole.EXPERIMENTAL_WORKER.value) in run.scopes())
        pool = run.pool
        assert pool is not None, "the supervisor is driving without a pool"

        # The turn the wait costs. Everything after it is the claim.
        await until(lambda: bool(prompts_about(prepared.node_id)))
        await asyncio.sleep(0.5)
        runtime = pool.live_runtime(project_id, AgentRole.EXPERIMENTAL_WORKER)
        assert runtime is not None, "the seat that served the wait is gone already"
        served = runtime.turns
        assert served >= 1, "a waiting task was handed to a seat that never asked its model"

        await asyncio.sleep(1.0)
        assert runtime.turns == served, (
            f"the wait is still costing turns: {served} became {runtime.turns} "
            "while the node did nothing but wait for a bench"
        )
        sent = len(prompts_about(prepared.node_id))

    # Days would pass here, and none of them need a RAVEL process. The
    # deployment is gone: its seats are closed, its pool is closed, and the
    # node's task is exactly where it was.
    assert runtime.is_closed and runtime.turns == served, (
        "the deployment stopped without closing the seat that was watching the wait"
    )
    assert pool.live_runtime(project_id, AgentRole.EXPERIMENTAL_WORKER) is None
    assert headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL, (
        "stopping the deployment changed the task's state; the wait belongs to the "
        "run, not to the process that was watching it"
    )

    # The event, and only the event. Nothing in RAVEL can substitute for it, and
    # nothing is running to receive it.
    await headless.client.deliver_external_result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
        result=ExternalResult(
            summary="The operator confirmed the run.",
            delivered_outputs=LAB_OUTPUTS,
        ),
    )
    await until(lambda: headless.status_of(prepared.node) is NodeStatus.REVIEWING)

    assert len(prompts_about(prepared.node_id)) == sent, (
        "ending the wait went through a model turn; the event is what ends it, and a "
        "Worker is told about the result by the record rather than by being held "
        "open across three days"
    )
    assert pool.live_runtime(project_id, AgentRole.EXPERIMENTAL_WORKER) is None, (
        "the wait resumed by starting a session, so the run needed an agent to end"
    )

    with headless.database.read_only() as session:
        executions = RecordRepositories(
            session, project_id
        ).executions.for_node(prepared.node_id)
    assert executions, (
        "the wait ended with nothing recorded, so what the bench delivered was "
        "never written down"
    )


# ── P10-15 ──────────────────────────────────────────────────────────────────────


def _status_of(database: Database, project_id: str, node_id: str) -> NodeStatus:
    """One node's status, read the way everything else here reads state."""
    with database.read_only() as session:
        return DagRepository(session, project_id).node(node_id).status


def _retire(database: Database, *projects: Project) -> None:
    """Take projects out of the supervisor's discovery, so a case is about one.

    The supervisor is right to pick up a project that is neither ended nor
    paused — that is the whole item — so a case about *one* project has to say
    what the others are, and "abandoned" is what they are. Left alone they would
    be driven too, and a case that asserted on one project while three ran would
    be reading a different run every time.
    """
    with database.transaction() as session:
        registry = ProjectRegistry(session)
        for project in projects:
            registry.transition(
                project.project_id,
                ProjectStatus.CANCELLED,
                actor_id="owner",
                reason="Not this case's subject.",
            )


async def test_p10_15_closing_the_console_does_not_stop_the_project(
    headless: Headless,
    seats: ScriptedSeats,
    monkeypatch: pytest.MonkeyPatch,
    world: World,
    live_gateway: LiveGateway,
) -> None:
    """P10-15: the console is a window, not a switch.

    A person closes their terminal, their SSH session drops, their laptop shuts
    — and the project has to be exactly where it was. What makes that true is
    that nothing about a project lives in the console: the console signs in to
    the Gateway, reads the project state and draws it, and the loop driving the
    project is in the supervisor, which never heard of it.

    So the demonstration is a project left mid-flight by a console that came and
    went. The lab is holding one node — it answers when something outside RAVEL
    says so — and the console is opened and closed while that is true. The
    assertion is that the project is still being driven afterwards and that the
    event which ends the wait, arriving after the console is gone, still ends
    it: the console's disappearance has to be invisible to the run, and the
    record has to show it was.
    """
    headless.compute("COMPUTE_SUCCESS")
    headless.lab("LAB_LONG_WAIT")
    project = world.owner_project
    seats.plan(project, ())
    # The headless fixture is here for its worker and its registry, not for its
    # project; the admin's is a runtime project with no DAG at all.
    _retire(headless.database, headless.project, world.admin_project)

    async with supervising(headless, monkeypatch, seats) as run:
        await until(lambda: run.driving() == {project.project_id})
        await until(
            lambda: _status_of(headless.database, project.project_id, world.experiment)
            is NodeStatus.WAITING_EXTERNAL
        )

        async with console(
            live_gateway,
            username="ada",
            project_id=project.project_id,
            first_panel="master-focus",
        ):
            pass

        assert run.status(project.project_id) is ProjectStatus.EXECUTING, (
            "the project left EXECUTING while the console was open"
        )
        # Not an instantaneous check: a loop that hits its round ceiling is
        # reaped and restarted, which is the supervisor recovering a project
        # rather than dropping one, and the console's visit is long enough to
        # span one of those restarts.
        await until(lambda: run.driving() == {project.project_id}, timeout=30.0)

        with headless.database.read_only() as session:
            contract = ExecutionContractRepository(session, project.project_id).for_node(
                world.experiment
            )
        await headless.client.deliver_external_result(
            node_id=world.experiment,
            execution_contract_version=contract.version,
            result=ExternalResult(
                summary="The operator confirmed the run.",
                delivered_outputs=DEFAULT_OUTPUTS,
            ),
        )
        await run.finished(project.project_id)

    assert run.status(project.project_id) is ProjectStatus.COMPLETED, (
        "the project did not survive the console's departure"
    )
    assert _status_of(headless.database, project.project_id, world.experiment) is (
        NodeStatus.PASSED
    )
