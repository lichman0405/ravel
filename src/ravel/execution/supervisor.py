"""Unattended project supervisor.

Discovers projects that are not ended and not paused, drives each with a
`ProjectLoop`, and keeps driving until the project ends or the supervisor is
stopped. Recovery is from PostgreSQL and the per-role DSH runtimes are shared
through one `DshRuntimePool`.

    python scripts/run_supervisor.py
    python scripts/run_supervisor.py --poll-seconds 10
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
from ravel.dsh.agents import HarnessAgent, WorkerAgent
from ravel.dsh.pool import DshRuntimePool, create_pool
from ravel.execution.loop import ProjectLoop
from ravel.execution.node_runs import TemporalNodeRuns
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

    async def run(self) -> None:
        """Start the supervisor loop and run until signalled."""
        self._pool = create_pool(self.settings)
        execution = await TemporalNodeRuns.connect(self.database, self.settings)
        try:
            while not self._stop_event.is_set():
                await self._tick(execution)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(
                        self._stop_event.wait(), timeout=self.poll_seconds
                    )
        finally:
            await self._shutdown()

    def stop(self) -> None:
        """Ask the supervisor to stop on its next check."""
        self._stop_event.set()

    async def _tick(self, execution: TemporalNodeRuns) -> None:
        """Start project loops for newly active projects, reap finished ones."""
        active = self._active_projects()
        for project_id in active:
            if project_id not in self._tasks:
                task = asyncio.create_task(
                    self._drive_project(project_id, execution),
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

    async def _drive_project(
        self, project_id: str, execution: TemporalNodeRuns
    ) -> None:
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
        try:
            run = await ProjectLoop(
                database=self.database,
                project_id=project_id,
                master=master,
                review=review,
                execution=execution,
                compute_worker=compute_worker,
                experimental_worker=experimental_worker,
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
