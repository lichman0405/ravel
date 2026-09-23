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
from dataclasses import dataclass, field
from typing import Any

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from ravel.config import Settings
from ravel.execution.backends import BackendRegistry
from ravel.execution.temporal.activities import NodeRunActivities
from ravel.execution.temporal.workflows import NodeRunWorkflow
from ravel.preparation import (
    LabMaterializer,
    MaterializerRegistry,
    RaspaMaterializer,
)
from ravel.state.database import Database
from ravel.state.store import ArtifactStore, S3ArtifactStore

logger = logging.getLogger(__name__)


def materializers_for(settings: Settings) -> MaterializerRegistry:
    """The environments this deployment can build, from its settings.

    Two materializers — the one compute stack V0 prepares for, and the bench
    package — and a materializer is registered whether or not this deployment
    can actually use it. That is deliberate and it is the difference between
    two refusals Master answers differently: a host with no RASPA installed
    refuses a RASPA contract with `ENVIRONMENT_UNAVAILABLE` and a sentence
    naming the setting that would fix it, where leaving the materializer out
    would answer `UNSUPPORTED_ENVIRONMENT` — "RAVEL cannot build this" — which
    is false. RAVEL can; this machine cannot.

    The laboratory materializer needs nothing installed, which is not the same
    as having nothing to refuse: it reads the contract and the criteria, and a
    contract that does not state a procedure is refused by it however healthy
    the host is.
    """
    registry = MaterializerRegistry()
    registry.register(RaspaMaterializer(data_dir=settings.raspa_data_dir))
    registry.register(LabMaterializer())
    return registry


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
    #: The environments this process can build, and the store the bytes a
    #: contract names are read from. Both belong to the worker because
    #: preparation is an activity: it is the process that runs one that needs
    #: to be able to do the work, and a runtime built without them refuses
    #: every contract that names an environment rather than guessing.
    materializers: MaterializerRegistry = field(default_factory=MaterializerRegistry)
    store: ArtifactStore | None = None

    @classmethod
    def from_settings(
        cls,
        settings: Settings | None = None,
        *,
        registry: BackendRegistry | None = None,
        materializers: MaterializerRegistry | None = None,
        store: ArtifactStore | None = None,
    ) -> ExecutionRuntime:
        """Build a runtime from configuration.

        The store is built here rather than lazily because building one opens
        no connection: it constructs a client that is used on the first read.
        A test that wants a store of its own passes one, and gets it.
        """
        resolved = settings or Settings()
        return cls(
            settings=resolved,
            database=Database.from_settings(resolved),
            registry=registry if registry is not None else BackendRegistry(),
            materializers=(
                materializers
                if materializers is not None
                else materializers_for(resolved)
            ),
            store=store if store is not None else S3ArtifactStore(resolved),
        )

    @property
    def activities(self) -> NodeRunActivities:
        """The activity set, bound to this runtime."""
        return NodeRunActivities(
            settings=self.settings,
            database=self.database,
            registry=self.registry,
            materializers=self.materializers,
            store=self.store,
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
        activities.prepare_execution,
        activities.start_job,
        activities.check_job,
        activities.deliver_external_result,
        activities.abandon_job,
        activities.finish_node_run,
    ]


def workflow_id_for(node_id: str, execution_contract_version: int) -> str:
    """The workflow id one run of one node is started under.

    Derived from the node and the terms the run executes, rather than
    generated, so that starting the same run twice is refused by Temporal
    instead of producing two runs of one node — that refusal is what makes "one
    node, one Execution Record" true without a database constraint having to
    guess that a node cannot be re-run.

    **The version is in the id because a contract revision is a second run.**
    A Worker that stops because its terms refused something ends its run and
    waits at WAITING_DECISION; Master answering by revising the contract leaves
    the node in the plan, and the work then happens under the new terms. That
    is a run of the same node, and a node identified by itself alone could
    never have a second one — the first run's id would still be there, and
    Temporal would refuse the start of the run that is the whole point of the
    revision. Naming the terms in the id is also what the Execution Records
    already do: `finish_node_run` finds its own earlier write by
    `execution_contract_version`, which only means something if a node can have
    run under more than one.
    """
    return f"node-run:{node_id}:v{execution_contract_version}"
