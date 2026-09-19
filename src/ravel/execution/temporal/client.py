"""Starting a node run, and telling one that something outside RAVEL happened.

The caller is Master. Nothing else starts a run, because starting one is a
decision about what work happens next, and that is the authority Master holds
exclusively.

Neither function here touches the DAG. Starting a run does not mark a node
READY, and the run's own activities move it; if this module could move a node
there would be two places that decide when work begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDReusePolicy
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.exceptions import TemporalError, WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from ravel.config import Settings
from ravel.execution.temporal.contracts import ExternalResult, RunInput, RunOutcome
from ravel.execution.temporal.worker import workflow_id_for
from ravel.execution.temporal.workflows import NodeRunWorkflow


class RunAlreadyStarted(RuntimeError):
    """A run of this node is already under way.

    Less an error than a fact the caller asked about: Master planning work
    needs to know whether a node is already running, rather than discover it by
    starting a second run.
    """


@dataclass
class NodeRunClient:
    """The handle Master starts node runs through."""

    settings: Settings
    client: Client

    @classmethod
    async def connect(cls, settings: Settings | None = None) -> NodeRunClient:
        """Connect using the converter both ends must agree on.

        A client without it and a worker with it would disagree about the
        payload encoding, and would fail at the first activity rather than at
        the first call.
        """
        resolved = settings or Settings()
        client = await Client.connect(
            resolved.temporal_host,
            namespace=resolved.temporal_namespace,
            data_converter=pydantic_data_converter,
        )
        return cls(settings=resolved, client=client)

    async def start_node_run(
        self,
        *,
        project_id: str,
        node_id: str,
        actor_id: str,
        execution_contract_version: int,
        attempt: int = 1,
    ) -> WorkflowHandle[NodeRunWorkflow, RunOutcome]:
        """Begin a run of one node under one version of its contract.

        The workflow id is derived from the node and the contract version, so a
        second start of the *same* run is refused by Temporal rather than
        producing a second run of the same work — and a node whose terms have
        been revised can run again, which is what answering an escalation with
        a revision means.

        Raises:
            RunAlreadyStarted: A run of this node under these terms is already
                under way.
        """
        order = RunInput(
            project_id=project_id,
            node_id=node_id,
            actor_id=actor_id,
            attempt=attempt,
            execution_contract_version=execution_contract_version,
        )
        try:
            return await self.client.start_workflow(
                NodeRunWorkflow.run,
                order,
                id=workflow_id_for(node_id, execution_contract_version),
                task_queue=self.settings.temporal_task_queue,
                # Refuse rather than allow: a node that has already run must not
                # silently run again because a caller retried a start. Running
                # work a second time is a decision, and it opens a new node or a
                # new contract version so that somebody has recorded it.
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except TemporalError as error:
            # Two exception types reach here for one condition, and only one of
            # them is an `RPCError`. The SDK raises `WorkflowAlreadyStartedError`
            # when it recognises ALREADY_EXISTS on a start — and that type
            # derives from `TemporalError`, not from `RPCError`, so catching
            # `RPCError` alone let it through and the promise below was never
            # kept: a caller that retried a start got a crash rather than the
            # `RunAlreadyStarted` this method documents. An RPC that failed for
            # some other reason still surfaces as itself.
            if isinstance(error, WorkflowAlreadyStartedError) or (
                isinstance(error, RPCError) and error.status is RPCStatusCode.ALREADY_EXISTS
            ):
                raise RunAlreadyStarted(
                    f"node {node_id} already has a run; a node runs once, and "
                    "running it again is a decision that opens new work"
                ) from error
            raise

    async def deliver_external_result(
        self,
        *,
        node_id: str,
        execution_contract_version: int,
        result: ExternalResult,
    ) -> None:
        """Tell a waiting run that what it was waiting for has happened.

        A signal, not an activity: the run is blocked in a durable wait and the
        signal is what ends it. The run then writes the result through an
        activity, which is what makes it survive the worker that received it.

        Addressed by the terms the run is executing, because that is what
        identifies the run: a node that has been revised and run again has more
        than one, and the one waiting is the one under the newest terms.
        """
        handle = self.client.get_workflow_handle(
            workflow_id_for(node_id, execution_contract_version),
            result_type=RunOutcome,
        )
        await handle.signal(NodeRunWorkflow.external_result, result)

    async def result(
        self,
        *,
        node_id: str,
        execution_contract_version: int,
        timeout: timedelta | None = None,
    ) -> RunOutcome:
        """Wait for a run to end and return how it ended."""
        handle = self.client.get_workflow_handle(
            workflow_id_for(node_id, execution_contract_version),
            result_type=RunOutcome,
        )
        return await handle.result(rpc_timeout=timeout)
