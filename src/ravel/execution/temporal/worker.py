"""The process that runs workflows and activities.

One worker polls one task queue for one deployment. It is the thing that gets
killed in the restart test, and killing it must change nothing about the
project: the workflow is in Temporal's history and the job is in PostgreSQL, so
a replacement worker picks up exactly where the dead one stopped. Anything this
process held only in memory would be the thing that got lost, which is why
nothing that matters is held only in memory.

**The sandbox is on.** Temporal imports workflow code inside a restricted
importer to catch the things a workflow must not do — a random number, a file
read, a direct call to `time.time()`. The dependencies that do that work at
import time are passed through explicitly in the workflow module rather than
trusted wholesale.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from ravel.config import Settings
from ravel.execution.backends import BackendRegistry
from ravel.execution.temporal.activities import NodeRunActivities
from ravel.execution.temporal.workflows import NodeRunWorkflow
from ravel.state.database import Database

logger = logging.getLogger(__name__)


@dataclass
class ExecutionRuntime:
    """Everything a worker process needs, built once.

    Holds the pieces rather than constructing them per call: an engine per
    activity invocation would open a connection per call, and a backend
    registry rebuilt per activity would forget whatever a mock was told.
    """

    settings: Settings
    database: Database
    registry: BackendRegistry

    @classmethod
    def from_settings(
        cls, settings: Settings | None = None, *, registry: BackendRegistry | None = None
    ) -> ExecutionRuntime:
        """Build a runtime from configuration."""
        resolved = settings or Settings()
        return cls(
            settings=resolved,
            database=Database.from_settings(resolved),
            registry=registry if registry is not None else BackendRegistry(),
        )

    @property
    def activities(self) -> NodeRunActivities:
        """The activity set, bound to this runtime."""
        return NodeRunActivities(
            settings=self.settings, database=self.database, registry=self.registry
        )

    async def connect(self) -> Client:
        """A Temporal client using the converter both ends must agree on.

        The pydantic converter is not the default, which means a client without
        it and a worker with it would disagree about the payload encoding and
        fail at the first activity rather than at the first call. Both are built
        from this method for that reason.
        """
        return await Client.connect(
            self.settings.temporal_host,
            namespace=self.settings.temporal_namespace,
            data_converter=pydantic_data_converter,
        )

    async def run_worker(self, *, stop: asyncio.Event | None = None) -> None:
        """Poll the task queue until asked to stop.

        `stop` exists for the restart test, which kills a worker mid-activity
        and needs the kill to happen on purpose rather than by sending a signal
        to a process the test does not own.
        """
        client = await self.connect()
        activities = self.activities
        worker = Worker(
            client,
            task_queue=self.settings.temporal_task_queue,
            workflows=[NodeRunWorkflow],
            activities=node_run_activities(activities),
        )
        logger.info(
            "worker polling %r on %s",
            self.settings.temporal_task_queue,
            self.settings.temporal_host,
        )
        if stop is None:
            await worker.run()
            return
        async with worker:
            await stop.wait()


async def run_worker_until_signalled(
    settings: Settings | None = None, *, registry: BackendRegistry | None = None
) -> None:
    """Run a worker until the process is interrupted.

    The entry point a deployment uses; the tests build their own runtime so
    they can register mock backends and stop it on purpose.
    """
    runtime = ExecutionRuntime.from_settings(settings, registry=registry)
    try:
        await runtime.run_worker()
    finally:
        runtime.database.dispose()


def node_run_activities(activities: NodeRunActivities) -> list[Callable[..., Any]]:
    """The activity set a worker registers for this runtime.

    One list, used by the worker and by the test that checks every name a
    workflow asks for is a name a worker answers to. Temporal registers an
    activity under its function name, so a typo in an `execute_activity("...")`
    string is a runtime failure rather than an import error, and this is where
    that typo is caught.
    """
    return [
        activities.begin_node_run,
        activities.start_job,
        activities.check_job,
        activities.deliver_external_result,
        activities.abandon_job,
        activities.finish_node_run,
    ]


def workflow_id_for(node_id: str) -> str:
    """The workflow id one node's run is started under.

    Derived from the node rather than generated, so that starting a run twice
    is refused by Temporal instead of producing two runs of one node. That
    refusal is what makes "one node, one Execution Record" true without a
    database constraint having to guess that a node cannot be re-run.
    """
    return f"node-run:{node_id}"
