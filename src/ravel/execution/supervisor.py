"""Unattended project supervisor.

Discovers projects that are not ended and not paused, drives each with a
`ProjectLoop`, and keeps driving until the project ends or the supervisor is
stopped. Recovery is from PostgreSQL and the per-role DSH runtimes are shared
through one `DshRuntimePool`.

    python scripts/run_supervisor.py
    python scripts/run_supervisor.py --poll-seconds 10

**Where this sits next to the Temporal worker.** Both are long-running
processes and neither replaces the other, because they hold different things:

- This one is the *control* plane. It answers "what should happen next for this
  project" — it starts a loop per active project, and each loop reads
  PostgreSQL, ticks the DAG's readiness, and hands the turn to whichever power
  owes one: Master, Review, or one of the three execution seats — the two
  Workers and Research. It holds the DSH sessions, and it is where a role's
  authority is bound.
- `scripts/run_temporal_worker.py` is the *data* plane. It answers "do the
  work" — it is the only process that runs node runs and calls a compute or lab
  backend.

**Neither process starts a run on its own account.** A node runs because the
Worker Agent whose node it is asked for one, through the tool that reaches the
Execution Service (`ravel.execution.node_runs`); the tool server is a third
process the harness spawns per session. What the control plane contributes is
the *turn*: a READY node, cleared by the DAG, handed to the seat that owns it.
What the data plane contributes is running it, durably, without either of the
other two waiting on it. Nothing here calls `start_workflow`.

**This process does hold a Temporal client, since Phase 11, and it only ever
reads with it.** The supervisor reconciles before it drives (`_reconcile`):
a node can be left in a live status by a run whose workflow is gone, and
whether that has happened is a fact only Temporal has — so the one thing the
control plane asks the data plane is `describe_workflow`, and the answer is
used to end the stranded job and put the node where Master is asked. No run is
started, no signal is sent, no workflow is terminated. The client is opened
lazily by `ravel.execution.reconcile`, on the first tick that has something to
ask, so a deployment with nothing stranded still opens no connection.

So a Worker agent's `start` turn begins a run and its `act` turns read one; the
backend call itself is an activity, there. Neither writes a decision or a
verdict on its own account — those come from the Master and Review sessions,
through the same services their MCP tools wrap.

**Losing this process loses no state.** Everything a loop needs is in
PostgreSQL, and a restart re-discovers the projects and starts from where the
record says they are. A project that was mid-run resumes by reading that its
node is RUNNING; a session that was mid-conversation loses only the words,
which is why the agents are told to read the state rather than remember it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from datetime import datetime

from ravel.config import Settings
from ravel.domain.clock import utcnow
from ravel.domain.enums import ProjectStatus
from ravel.domain.roles import AgentRole
from ravel.domain.services import SUPERVISOR
from ravel.domain.state_machines import TERMINAL_PROJECT_STATUSES
from ravel.dsh.agents import HarnessAgent, ResearchAgent, WorkerAgent
from ravel.dsh.pool import DshRuntimePool, create_pool
from ravel.execution.loop import ProjectLoop
from ravel.execution.reconcile import ExecutionReconciler
from ravel.state.database import Database
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.services import ServiceRepository

logger = logging.getLogger(__name__)


@dataclass
class ProjectSupervisor:
    """Drives every active project until it ends, pauses, or the supervisor stops."""

    database: Database
    settings: Settings
    poll_seconds: float = 5.0
    loop_poll_seconds: float = 0.5
    loop_max_rounds: int = 200
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict, init=False, repr=False)
    _pool: DshRuntimePool | None = field(default=None, init=False, repr=False)
    _stop_event: asyncio.Event = field(default_factory=asyncio.Event, init=False, repr=False)
    #: When `run` began. `None` until then, so a supervisor that was never
    #: started reports nothing rather than reporting a start it did not have.
    _started_at: datetime | None = field(default=None, init=False, repr=False)
    #: Built from `database` and `settings` in `__post_init__` rather than
    #: declared as a field, because a default would have to be built from the
    #: other two and a caller that replaced one of them would get a reconciler
    #: still pointed at the other. `probe` stays overridable after the fact —
    #: that is the seam a test uses to answer for Temporal without a cluster.
    reconciler: ExecutionReconciler = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self.reconciler = ExecutionReconciler(
            database=self.database, settings=self.settings
        )

    @property
    def pool(self) -> DshRuntimePool | None:
        """The runtime pool every seat of every project shares.

        `None` before `run` has started one, which is the state a supervisor
        that was constructed and never run is in. It is public because what the
        pool holds — which `(project, role)` scopes are live, and where each
        one's working directory is — is a fact about the deployment rather than
        an implementation detail of this class.
        """
        return self._pool

    async def run(self) -> None:
        """Start the supervisor loop and run until signalled.

        The start time is taken here rather than in `__init__`, because an
        object that was constructed and never run is not a process that is
        running — and a beat that counted from construction would report uptime
        for a supervisor whose loop had not begun.

        **Losing this process loses no work, and the beat is how a person finds
        out that it was lost.** Nothing in the project's own state distinguishes
        a project a dead supervisor stopped driving from one with nothing left
        to do, so the row this loop writes is the only thing that does.
        """
        self._pool = create_pool(self.settings)
        self._started_at = utcnow()
        try:
            while not self._stop_event.is_set():
                await self._tick()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.poll_seconds
                    )
        finally:
            await self._shutdown()

    def stop(self) -> None:
        """Ask the supervisor to stop on its next check."""
        self._stop_event.set()

    async def _tick(self) -> None:
        """Recover dead runs, then start loops for active projects and reap.

        The beat is the first thing the tick does and the active set is
        deliberately not in it yet: a supervisor that has read the database and
        is about to reconcile is alive, and a report that waited for the whole
        tick to finish would go quiet every time a reconciliation sweep was
        slow — which is precisely when somebody is looking at the screen.
        """
        self._beat()
        self._reap()
        active = self._active_projects()
        await self._reconcile(active)
        for project_id in active:
            if project_id not in self._tasks:
                task = asyncio.create_task(
                    self._drive_project(project_id),
                    name=f"ravel-project-{project_id}",
                )
                self._tasks[project_id] = task
                logger.info("supervisor started driving project=%s", project_id)

        ended = [pid for pid, task in self._tasks.items() if task.done()]
        for pid in ended:
            task = self._tasks.pop(pid)
            exc = task.exception()
            if exc is None or isinstance(exc, asyncio.CancelledError):
                logger.info("supervisor project loop ended: project=%s", pid)
            else:
                logger.error(
                    "supervisor project loop failed: project=%s error=%s",
                    pid,
                    exc,
                    exc_info=exc,
                )

    def _beat(self) -> None:
        """Say that this process is still here, and what it is holding.

        Once per tick, and the only write in this class that is not about a
        project. It is here rather than in a thread of its own because a tick
        that has stopped happening is exactly the failure the row exists to
        make visible: a beat from a timer would keep reporting a supervisor
        whose loop was stuck.

        **A failure to report does not stop the supervisor.** A database that
        went away mid-beat is the same class of problem as one that went away
        mid-scan, and the loop already answers it by trying again next tick.
        The alternative — dying because a liveness report failed — would make
        the report the least reliable thing in the process.

        `detail` names the projects this supervisor is holding, which is the
        whole reason the pool is on the row: "the supervisor is up" and "the
        supervisor is up and has never heard of my project" are different
        answers to the question an operator is asking, and only the second is
        worth restarting anything over. The Gateway filters this to the project
        being asked about — see `routes/admin.py`.
        """
        if self._started_at is None:  # pragma: no cover - `_tick` runs after `run`
            return
        # `poll_seconds` is the promise the reader measures the silence
        # against: a supervisor configured to poll every five minutes is not
        # dead because it has been quiet for one, and only the process itself
        # knows which it is.
        detail: dict[str, object] = {
            "projects": sorted(self._tasks),
            "poll_seconds": self.poll_seconds,
        }
        pool = self._pool
        if pool is not None:
            stats = pool.stats()
            detail["live_runtimes"] = stats.live_runtimes
            detail["live_sessions"] = stats.live_sessions
        try:
            with self.database.transaction() as session:
                ServiceRepository(session).report(
                    SUPERVISOR, started_at=self._started_at, detail=detail
                )
        except Exception:
            logger.exception("could not record this supervisor's heartbeat; continuing")

    async def _reconcile(self, active: set[str]) -> None:
        """Find runs that are gone, and put their nodes back in play.

        Before the loops rather than beside them, because a project holding a
        stranded node is one the loop cannot move: the node reads as in flight,
        so the loop hands out no turn that could end it. Reconciling first
        means the loop's own read of the project, a moment later, is of a
        project where Master has something to decide.

        **A failure here does not stop the supervisor.** A Temporal frontend
        that is down, a database that went away mid-scan — neither is a reason
        to stop driving the projects that are not affected, and a supervisor
        that died because it could not check one node would take every other
        project down with it. The next tick tries again, which is what a
        polling loop is for.
        """
        if not active:
            return
        try:
            await self.reconciler.reconcile(active)
        except Exception:
            logger.exception("reconciliation sweep failed; continuing to drive")

    def _reap(self) -> None:
        """Close harness runtimes that have gone idle.

        The supervisor is the process a project runs unattended in, so it is
        the one that has to notice dormancy: without this, every runtime a
        project ever started — five per project, two of them seats that spend
        most of their lives waiting on a backend or on nobody — would stay
        resident for as long as the supervisor runs. Reaping does not interrupt
        a turn (a turn holds its runtime's lock) and does not lose anything (a
        session is disposable; the next turn starts a new one and reads the
        database).
        """
        if self._pool is None:
            return
        reaped = self._pool.reap_idle()
        if reaped:
            logger.info("supervisor reaped %d idle harness runtime(s)", reaped)

    def _active_projects(self) -> set[str]:
        """Projects that are not terminal and not paused."""
        with self.database.read_only() as session:
            projects = ProjectRegistry(session).list()
        return {
            project.project_id
            for project in projects
            if project.status not in TERMINAL_PROJECT_STATUSES
            and project.status is not ProjectStatus.PAUSED
        }

    async def _drive_project(self, project_id: str) -> None:
        """One project's loop, from active to ended."""
        assert self._pool is not None
        pool = self._pool
        master = HarnessAgent(
            pool=pool, project_id=project_id, role=AgentRole.MASTER
        )
        review = HarnessAgent(
            pool=pool, project_id=project_id, role=AgentRole.REVIEW
        )
        compute_worker = WorkerAgent(
            pool=pool, project_id=project_id, role=AgentRole.COMPUTE_WORKER
        )
        experimental_worker = WorkerAgent(
            pool=pool, project_id=project_id, role=AgentRole.EXPERIMENTAL_WORKER
        )
        research = ResearchAgent(
            pool=pool, project_id=project_id, role=AgentRole.RESEARCH
        )
        try:
            run = await ProjectLoop(
                database=self.database,
                project_id=project_id,
                master=master,
                review=review,
                compute_worker=compute_worker,
                experimental_worker=experimental_worker,
                research=research,
                poll_seconds=self.loop_poll_seconds,
                max_rounds=self.loop_max_rounds,
            ).run()
            logger.info(
                "supervisor project loop finished: project=%s status=%s rounds=%d halted=%s",
                project_id,
                run.status.value,
                run.rounds,
                run.halted,
            )
        finally:
            master.close()
            review.close()
            compute_worker.close()
            experimental_worker.close()
            research.close()

    async def _shutdown(self) -> None:
        """Cancel running project loops and close shared resources."""
        for task in self._tasks.values():
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()
        if self._pool is not None:
            self._pool.close()
        await self.reconciler.close()
        self.database.dispose()
