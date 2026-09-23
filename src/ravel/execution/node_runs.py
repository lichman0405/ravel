"""Starting a node's run, and telling one that what it waited for has happened.

**Who starts a run.** The Worker Agent whose node it is — that is Phase 10H's
chain, and it is why `ProjectLoop` holds no execution power at all:

    Supervisor → Worker Agent → authorized Worker tool → Execution Service
    → Temporal → MockComputeBackend

The Worker's tool handler calls `ExecutionService.start`; this module is what
carries that call to Temporal. `TemporalNodeRuns` is the deployment's own
handle, held by a process that already has a Temporal client; the service is
what a caller holds when it may start a run but must not hold a client of its
own — a Worker's tool server, which the harness spawns per session and which
connects on the first call that needs one.

**The second thing this module does is deliver, and it is the same shape.**
A run waiting on something outside RAVEL is blocked in a durable wait and is
never polled: `check_job` does not run again until the wait ends, so nothing
RAVEL learns by looking will reach it. What ends the wait is a signal, and the
callers that send one are not the deployment — a laboratory user uploading the
data they were asked for, or reporting that the bench and the plan disagree,
reaches the run through the Gateway, which holds no Temporal client of its own.
So the delivery goes through the same lazily-connected service as the start,
and for the same reason: a Gateway that only serves reads should not open a
connection to anything.

Nothing here decides *whether* a node may run. That is the DAG's answer
(`DagNode.can_enter_running`), checked by the tool handler before it gets here,
because a service that also judged would be a second place where "this node may
run" is decided.

**A refusal is not an error.** The caller reads the DAG, sees a node READY, and
starts it; the node becomes RUNNING when the run's own activity writes the
transition, which is a moment later. A caller that came round again in between
would see READY again, and a second start would be refused by Temporal — with
the node running perfectly well. So a refusal is read for what it is: the work
is under way, which is what the caller wanted, and `start` says so rather than
raising.

**A run is identified by the node and by the terms it executes.** The version
travels with the start because the id Temporal will refuse a duplicate of is
made of both, and this module is where the reading happens: the caller hands it
a node, and which contract governs that node is a fact PostgreSQL holds. Two
starts of the same node under the same version are one run; a node whose
contract Master revised runs again under the new version, which is what
answering an escalation with a revision means. That run's attempts are numbered
from one, because they are the attempts of the work *that version* describes.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.execution.temporal.client import NodeRunClient, RunAlreadyStarted
from ravel.execution.temporal.contracts import ExternalResult
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository

logger = logging.getLogger(__name__)

__all__ = ["ExecutionService", "ExternalResultPort", "TemporalNodeRuns"]


@runtime_checkable
class ExternalResultPort(Protocol):
    """Telling a run that what it was waiting for has happened.

    A protocol rather than `ExecutionService` itself, so that the caller that
    needs this — the Gateway, on a route a laboratory user reaches — can be
    handed something that does not connect to Temporal at all. That is not only
    a testing convenience: the delivery is one sentence to a run that may not
    exist, and a composition that could not serve an upload without a Temporal
    connection would make the laboratory surface depend on the scheduler being
    up when the *record* of an upload is what the caller actually needs.

    `deliver` is addressed by the terms the run executes rather than by the job,
    because that is what identifies a run: a node whose terms Master revised
    runs again, and the one waiting is the one under the newest terms.
    """

    async def deliver(
        self,
        *,
        node_id: str,
        execution_contract_version: int,
        result: ExternalResult,
    ) -> None:
        """Signal the run of this node under these terms.

        Raises:
            Exception: Whatever the transport raises. Callers decide what a
                failure to reach a run means for them; this port does not
                swallow it, because a signal that silently went nowhere is the
                one outcome a caller cannot tell from success.
        """
        ...


@dataclass
class TemporalNodeRuns:
    """The deployment's `ExecutionPort`, over Temporal."""

    database: Database
    client: NodeRunClient
    #: Recorded on every run this port starts, so a reader can tell which role
    #: began a node from the actor on the run itself.
    actor_id: str = "scheduler"

    @classmethod
    async def connect(
        cls, database: Database, settings: Settings | None = None
    ) -> TemporalNodeRuns:
        """Connect a client, with the converter both ends must agree on."""
        return cls(database=database, client=await NodeRunClient.connect(settings))

    async def start(self, node: DagNode, *, actor_id: str | None = None) -> bool:
        """Begin this node's run, or say that one is already under way.

        Returns:
            True if this call started the run, False if the node already had
            one under the same terms.
        """
        try:
            await self.client.start_node_run(
                project_id=node.project_id,
                node_id=node.node_id,
                actor_id=actor_id or self.actor_id,
                execution_contract_version=self.contract_version(node),
            )
        except RunAlreadyStarted:
            logger.info(
                "node %s already has a run under way; leaving it alone",
                node.display_id,
            )
            return False
        logger.info(
            "started a run of node %s as %s", node.display_id, actor_id or self.actor_id
        )
        return True

    async def deliver(
        self,
        *,
        node_id: str,
        execution_contract_version: int,
        result: ExternalResult,
    ) -> None:
        """Tell the run of this node, under these terms, that something happened.

        The version is the caller's to state rather than read here, and that is
        the difference between this method and `start`. A caller starting work
        is asking "what do the terms currently say", which is a fact this
        module reads. A caller delivering is answering a question that was put
        to it *by a particular run* — a handover RAVEL made under one version of
        a contract — and re-reading the newest version would send the answer to
        a run that never asked, or to none at all.
        """
        await self.client.deliver_external_result(
            node_id=node_id,
            execution_contract_version=execution_contract_version,
            result=result,
        )

    def contract_version(self, node: DagNode) -> int:
        """Which version of the node's contract this run executes.

        **The newest**, rather than the version the node was bound to: the
        node's `execution_contract_ref` is fixed at binding so a result cannot
        be measured against terms chosen afterwards, while what the Worker may
        *do* is the newest contract — which is exactly the version Master's
        revision created, and the one a run has to be started under for that
        revision to have answered anything.

        Read here rather than chosen by the caller because it is a fact about
        the project, and the loop that asks for a run does not hold it.
        """
        with self.database.read_only() as session:
            return (
                ExecutionContractRepository(session, node.project_id)
                .for_node(node.node_id)
                .version
            )


@dataclass
class ExecutionService:
    """Reaching a node's run, for a caller that holds no Temporal client.

    A Worker acts through tools, and its tools run in a server the harness
    spawns for one session. That process is not the deployment: it should not
    need a Temporal connection to answer a question about a contract, and most
    of what a Worker asks never reaches execution at all. So the client is made
    on the first call that needs one, and a session that only ever reads its
    contract never opens a connection.

    The Gateway holds one of these too, for `deliver`: the route a laboratory
    user uploads through is a route that usually has nothing waiting at the
    other end, and the cost of a connection nobody needed would be paid on
    every request.

    The connection is made once and kept, because a Worker's turns are spread
    over the life of its task and reconnecting per call would pay setup on
    every retry. Closing is the owner's: a process that made a connection is
    the one that knows when it is done with it.
    """

    database: Database
    #: The deployment's settings, or `None` to read them from the environment.
    #: A test hands its own in, because it is the settings that name the task
    #: queue the run must be started on for that test's worker to pick it up.
    settings: Settings | None = None
    _runs: TemporalNodeRuns | None = field(default=None, init=False, repr=False)
    _connecting: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def start(self, node: DagNode, *, actor_id: str) -> bool:
        """Begin this node's run.

        Returns:
            True if this call started the run, False if one was already under
            way under the same terms.
        """
        return await (await self.port()).start(node, actor_id=actor_id)

    async def deliver(
        self,
        *,
        node_id: str,
        execution_contract_version: int,
        result: ExternalResult,
    ) -> None:
        """Tell a waiting run that something outside RAVEL happened.

        Connects on the first call, like `start`, so that a Gateway serving a
        project with nothing in anybody's hands never reaches Temporal at all.
        """
        await (await self.port()).deliver(
            node_id=node_id,
            execution_contract_version=execution_contract_version,
            result=result,
        )

    async def port(self) -> TemporalNodeRuns:
        """The port, connecting on first use.

        The lock is for the case a caller runs two of a Worker's turns at once:
        without it both would connect, and the second client would replace the
        first without ever being closed.
        """
        if self._runs is None:
            async with self._connecting:
                if self._runs is None:
                    self._runs = await TemporalNodeRuns.connect(self.database, self.settings)
        return self._runs

    async def close(self) -> None:
        """Give up the connection, if this service ever opened one.

        Nothing is torn down, because there is nothing to tear down: the
        Temporal client this SDK hands back has no shutdown of its own, and its
        channel closes with the process. What this releases is the reference —
        the port is made again on the next call that needs one — which is what
        a caller that is finishing with a Worker's scope actually wants.
        """
        self._runs = None
