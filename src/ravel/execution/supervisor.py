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
process the harness spawns per session, so this one holds no Temporal client at
all. What the control plane contributes is the *turn*: a READY node, cleared by
the DAG, handed to the seat that owns it. What the data plane contributes is
running it, durably, without either of the other two waiting on it.

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

from ravel.config import Settings
from ravel.domain.enums import ProjectStatus
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TERMINAL_PROJECT_STATUSES
from ravel.dsh.agents import HarnessAgent, ResearchAgent, WorkerAgent
from ravel.dsh.pool import DshRuntimePool, create_pool
from ravel.execution.loop import ProjectLoop
from ravel.state.database import Database
from ravel.state.repositories.projects import ProjectRegistry

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
        """Start the supervisor loop and run until signalled."""
        self._pool = create_pool(self.settings)
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
        """Start project loops for newly active projects, reap finished ones."""
        self._reap()
        active = self._active_projects()
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
        self.database.dispose()
