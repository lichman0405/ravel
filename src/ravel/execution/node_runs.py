"""Starting a node's run, as the project loop's execution port.

`ProjectLoop` sequences a project without executing anything; this is what it
calls when the next thing to happen is that a node runs. The work itself
happens in Temporal, in `NodeRunWorkflow`, and the durable layer already owns
everything about it — the retry policy, the long waits, the activity that talks
to a backend. What is added here is one method and one decision: that starting a
run is not an error when a run of that node already exists.

That is not a defensive special case. The loop reads the DAG, sees a node READY,
and starts it; the node becomes RUNNING when the run's own activity writes the
transition, which is a moment later. A loop that came round again in between
would see READY again, and a second start would be refused by Temporal — with
the node running perfectly well. So a refusal is read for what it is: the work
is under way, which is what the caller wanted.

**A run is identified by the node and by the terms it executes.** The version
travels with the start because the id Temporal will refuse a duplicate of is
made of both, and the port is where the reading happens: the loop hands this
class a node, and which contract governs that node is a fact PostgreSQL holds.
Two starts of the same node under the same version are one run; a node whose
contract Master revised runs again under the new version, which is what
answering an escalation with a revision means. That run's attempts are numbered
from one, because they are the attempts of the work *that version* describes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.execution.temporal.client import NodeRunClient, RunAlreadyStarted
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository

logger = logging.getLogger(__name__)

__all__ = ["TemporalNodeRuns"]


@dataclass
class TemporalNodeRuns:
    """The loop's `ExecutionPort`, over Temporal."""

    database: Database
    client: NodeRunClient
    #: Recorded on every run this port starts, so a reader can tell a node the
    #: scheduler began from one a human did.
    actor_id: str = "scheduler"

    @classmethod
    async def connect(
        cls, database: Database, settings: Settings | None = None
    ) -> TemporalNodeRuns:
        """Connect a client, with the converter both ends must agree on."""
        return cls(database=database, client=await NodeRunClient.connect(settings))

    async def start(self, node: DagNode, *, actor_id: str | None = None) -> None:
        """Begin this node's run, or note that one is already under way."""
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
            return
        logger.info("started a run of node %s", node.display_id)

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
