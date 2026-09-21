"""The pieces the Phase 10 acceptance cases share.

Phase 10 is about *who acts*: five agents in the loop instead of two, a
supervisor that discovers projects instead of somebody naming one, and Workers
whose authority stops at the contract. The tests that demonstrate that need the
real stack — PostgreSQL, Temporal, the mock backends, a real pool — with the
model-owned parts scripted, exactly as `tests/e2e` scripts Master and Review.

What is scripted here is the *seat*, not the mechanism: a `ScriptedWorker` is
an `ExecutorPort` the loop dispatches to, and a `ScriptedSeats` is what the
supervisor's five agent constructions resolve to. A supervisor that built its
agents differently would fail these tests, which is the point — the wiring is
under test, not the script.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, ClassVar

from tests.e2e.conftest import ScriptedMaster, ScriptedReview, Task

from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.domain.enums import ProjectOutcome, ProjectStatus, ReviewOutcome
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.execution.loop import Situation
from ravel.execution.node_runs import ExecutionService
from ravel.mcp.context import ToolContext
from ravel.mcp.scope import ToolScope
from ravel.mcp.tools import IMPLEMENTATIONS
from ravel.state.database import Database
from ravel.state.repositories.projects import ProjectRegistry


class FakeHarness:
    """The harness process, replaced by something that answers without a model.

    The one part of a Phase 10 case that cannot be afforded in most of them is
    the process: it is what costs money and needs a credential. Everything above
    it stays real — the runtime, its working directory, the overlay written for
    it, the session identifiers it mints, the binding registry that records what
    a session serves, and the agent that decides what to send it.

    `prompts` is what the turn was given, which is how a case reads what an
    agent told its model rather than what the model did with it.
    """

    #: Every process this patch has stood in for, in the order it was created.
    instances: ClassVar[list[FakeHarness]] = []

    def __init__(self, config: Any) -> None:
        self.config = config
        self.sessions: list[str] = []
        self.prompts: list[str] = []
        self.closed = False
        FakeHarness.instances.append(self)

    def run(self, *, session_id: str, input: str, on_notification: Any = None) -> Any:
        _ = on_notification
        self.sessions.append(session_id)
        self.prompts.append(input)
        return SimpleNamespace(
            session_id=session_id,
            final_response="Nothing in this turn needs doing.",
            finish_reason="completed",
            events=[],
        )

    def close(self) -> None:
        self.closed = True


@dataclass
class ScriptedWorker:
    """An execution seat whose turn is a script instead of a model.

    Implements `ExecutorPort`, so it can serve any of the three seats the loop
    dispatches to. What it records is what the loop asked it to look at:
    `(node_id, status)` per turn, and the nodes it was asked to begin. A turn
    may also *do* something through the real role tools — the `behavior` hook is
    handed the same context a live session would have — which is how a test gets
    a deviation raised, or a research task handed over, by the path production
    uses.

    `start` does the one thing an execution seat must do rather than a thing it
    may choose to do: a node that is cleared to run runs because this seat asked
    for it, through the real tool — `start_execution` for the two Workers, which
    hands the work to the Execution Service, and `begin_research` for Research,
    which takes the task into its own hands. So a project driven through these
    seats begins its work through the same door a live DSH session does, the
    tools' refusals included.
    """

    #: The tool each role begins a task through — the same act under two names,
    #: which is why a seat has to be told which role it is serving.
    BEGIN: ClassVar[dict[AgentRole, str]] = {
        AgentRole.COMPUTE_WORKER: "start_execution",
        AgentRole.EXPERIMENTAL_WORKER: "start_execution",
        AgentRole.RESEARCH: "begin_research",
    }

    project_id: str
    role: AgentRole
    turns: list[tuple[str, str]] = field(default_factory=list)
    started: list[str] = field(default_factory=list)
    behavior: Callable[[DagNode, Situation], Any] | None = None
    context: ToolContext | None = None
    closed: bool = False

    async def start(self, node: DagNode) -> None:
        """Begin the node's task through the tool a live session would use."""
        self.started.append(node.node_id)
        if self.context is None:
            return
        await seat_tool(self.BEGIN[self.role], self.context)(node_id=node.node_id)

    async def act(self, node: DagNode, situation: Situation) -> None:
        self.turns.append((node.node_id, node.status.value))
        if self.behavior is not None:
            await self.behavior(node, situation)

    def close(self) -> None:
        self.closed = True

    @property
    def nodes(self) -> list[str]:
        """The nodes this Worker was given a turn on, in order."""
        return [node_id for node_id, _status in self.turns]


@dataclass
class ProjectPlan:
    """One project's scripted Master and Review.

    Memoized per project because the script *is* the state a test reads
    afterwards: `master.trace` is what Master did, and a second construction
    would be a second Master whose trace nobody saw.
    """

    database: Database
    project_id: str
    tasks: tuple[Task, ...]
    replan: Callable[[DagNode], tuple[Task, ...]] | None = None
    outcome: ProjectOutcome = ProjectOutcome.SUCCESS
    verdicts: dict[str, ReviewOutcome] = field(default_factory=dict)
    _master: ScriptedMaster | None = field(default=None, init=False, repr=False)
    _review: ScriptedReview | None = field(default=None, init=False, repr=False)

    @property
    def master(self) -> ScriptedMaster:
        if self._master is None:
            self._master = ScriptedMaster(
                database=self.database,
                project_id=self.project_id,
                tasks=self.tasks,
                replan=self.replan,
                outcome=self.outcome,
            )
        return self._master

    @property
    def review(self) -> ScriptedReview:
        if self._review is None:
            self._review = ScriptedReview(
                database=self.database,
                project_id=self.project_id,
                verdicts=self.verdicts,
            )
        return self._review


@dataclass
class ScriptedSeats:
    """The five seats of every project a supervisor drives, scripted.

    Handed to the supervisor in place of `HarnessAgent`, `WorkerAgent` and
    `ResearchAgent`, so that a test can drive the *supervisor* — discovery, one
    loop per project, isolation between them — without a model in any seat. Everything below the
    seats is real: the same pool, the same `TemporalNodeRuns`, the same
    `ProjectLoop`, the same tools.
    """

    database: Database
    #: The deployment settings a Worker's Execution Service is built from. The
    #: seats share one service, because the tool servers of one deployment
    #: share one Temporal namespace and the connection is the expensive part.
    settings: Settings | None = None
    plans: dict[str, ProjectPlan] = field(default_factory=dict)
    workers: dict[tuple[str, AgentRole], ScriptedWorker] = field(default_factory=dict)
    behaviors: dict[AgentRole, Callable[[DagNode, Situation], Any]] = field(
        default_factory=dict
    )
    overrides: dict[tuple[str, AgentRole], Any] = field(default_factory=dict)
    execution: ExecutionService = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.execution = ExecutionService(database=self.database, settings=self.settings)

    def override(self, project_id: str, role: AgentRole, port: Any) -> None:
        """Put a different port in one seat — a Master that fails, say."""
        self.overrides[(project_id, role)] = port

    def plan(
        self,
        project: Project,
        tasks: tuple[Task, ...],
        *,
        replan: Callable[[DagNode], tuple[Task, ...]] | None = None,
        outcome: ProjectOutcome = ProjectOutcome.SUCCESS,
        verdicts: dict[str, ReviewOutcome] | None = None,
    ) -> ProjectPlan:
        """How this project's Master and Review are to behave."""
        self.plans[project.project_id] = ProjectPlan(
            database=self.database,
            project_id=project.project_id,
            tasks=tasks,
            replan=replan,
            outcome=outcome,
            verdicts=verdicts or {},
        )
        return self.plans[project.project_id]

    def worker(self, project_id: str, role: AgentRole) -> ScriptedWorker:
        """The scripted Worker serving one seat of one project.

        Built with a real tool context, so that what the seat does when it is
        asked to begin a node is what a live session does: the same handler,
        the same role check, the same Execution Service.
        """
        key = (project_id, role)
        if key not in self.workers:
            self.workers[key] = ScriptedWorker(
                project_id=project_id,
                role=role,
                behavior=self.behaviors.get(role),
                context=seat_scope(
                    self.database, project_id, role, execution=self.execution
                ),
            )
        return self.workers[key]

    # ── What the supervisor constructs ─────────────────────────────────────

    def harness_agent(self, *, pool: Any, project_id: str, role: AgentRole) -> Any:
        """Master's or Review's seat, as the supervisor builds it."""
        _ = pool
        override = self.overrides.get((project_id, role))
        if override is not None:
            return override
        plan = self.plans[project_id]
        if role is AgentRole.MASTER:
            return _Closable(plan.master)
        assert role is AgentRole.REVIEW, f"the loop does not call {role.value}"
        return _Closable(plan.review)

    def worker_agent(self, *, pool: Any, project_id: str, role: AgentRole) -> ScriptedWorker:
        """A Worker's seat, as the supervisor builds it."""
        _ = pool
        override = self.overrides.get((project_id, role))
        if override is not None:
            return override
        return self.worker(project_id, role)

    def research_agent(self, *, pool: Any, project_id: str, role: AgentRole) -> ScriptedWorker:
        """Research's seat, as the supervisor builds it.

        The same scripted seat as a Worker's, because the loop dispatches to
        both the same way; what differs is the role it serves, and that is what
        decides which begin tool its first turn uses.
        """
        _ = pool
        override = self.overrides.get((project_id, role))
        if override is not None:
            return override
        return self.worker(project_id, role)

    # ── What a test reads ──────────────────────────────────────────────────

    def turns_for(self, project_id: str, role: AgentRole) -> list[tuple[str, str]]:
        key = (project_id, role)
        return self.workers[key].turns if key in self.workers else []


@dataclass
class _Closable:
    """A scripted port plus the `close()` the supervisor calls on every seat."""

    port: Any

    async def act(self, situation: Situation) -> None:
        await self.port.act(situation)

    def close(self) -> None:
        """Master and Review scripts hold no runtime; there is nothing to close."""


def seat_scope(
    database: Database,
    project_id: str,
    role: AgentRole,
    *,
    settings: Settings | None = None,
    execution: ExecutionService | None = None,
) -> ToolContext:
    """An execution seat's tool context, built the way its server builds one.

    The scope comes from an argument rather than the environment because the
    test *is* the process that would have been launched with it; the handlers
    reached through it are the ones a DSH session reaches over MCP, with the
    same role checks and the same writes.

    The Execution Service comes from the caller for the reason the deployed one
    reads it from the environment: it is the deployment's settings that name
    the task queue a run has to land on, and a test whose run landed on the
    deployment's queue would be picked up by a worker that is not listening.
    """
    return ToolContext(
        scope=ToolScope(project_id=project_id, role=role),
        database=database,
        execution=execution or ExecutionService(database=database, settings=settings),
    )


def seat_tool(name: str, context: ToolContext) -> Any:
    """One real role tool handler, as its server would register it."""
    return IMPLEMENTATIONS[name](context)


@contextmanager
def patched_agents(monkeypatch: Any, seats: ScriptedSeats) -> Generator[ScriptedSeats]:
    """Point the supervisor's agent construction at the scripted seats."""
    from ravel.execution import supervisor as supervisor_module

    monkeypatch.setattr(supervisor_module, "HarnessAgent", seats.harness_agent)
    monkeypatch.setattr(supervisor_module, "WorkerAgent", seats.worker_agent)
    monkeypatch.setattr(supervisor_module, "ResearchAgent", seats.research_agent)
    yield seats


async def until(predicate: Callable[[], bool], *, timeout: float = 60.0) -> None:
    """Wait for a fact about the stack, the way every suite here waits."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition was not met within {timeout}s")


def project_status(database: Database, project_id: str) -> ProjectStatus:
    """A project's status, read from PostgreSQL rather than from a return value."""
    with database.read_only() as session:
        return ProjectRegistry(session).get(project_id).status


def second_project(database: Database, title: str = "Unrelated") -> Project:
    """A second project, for the tests about scopes not leaking into each other."""
    with database.transaction() as session:
        return ProjectRegistry(session).create(
            title=title, objective="Something else entirely.", created_by="someone"
        )
