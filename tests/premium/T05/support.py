"""Shared, deterministic scaffolding for the T05 retry tests.

The tests need three things the product does not provide: a clock they can move
without sleeping, ports that count the model turns they are handed, and a seat
whose `start` is the Worker's own DAG transition. They are here rather than in
each module because a second definition of "a loop wired to the durable budget"
is exactly the second place the wiring could drift from the supervisor's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, ProjectStatus
from ravel.execution.loop import ProjectLoop, Situation
from ravel.execution.retry_policy import (
    DEFAULT_COOLDOWNS,
    RetryPolicy,
    RetryWiring,
)
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry

#: An arbitrary but fixed instant. Never `utcnow()`: a test that compared an
#: attempt's cooldown against real time would pass or fail depending on how
#: long the rest of the suite took, and the cooldown arithmetic is the thing
#: being asserted.
T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    """A clock the test moves, so cooldowns cost nothing to observe."""

    moment: datetime = T0

    def now(self) -> datetime:
        return self.moment

    def advance(self, *, seconds: float = 0.0, days: float = 0.0) -> datetime:
        self.moment += timedelta(seconds=seconds, days=days)
        return self.moment


class CountingPort:
    """A Master or Review that takes every turn and writes nothing.

    Writing nothing is the failure mode the budget exists for: the condition
    that raised the question still holds on the next round, so an unbounded loop
    asks again. Counting the turns is how the tests see the budget.
    """

    def __init__(self) -> None:
        self.turns = 0

    async def act(self, situation: Situation) -> None:
        _ = situation
        self.turns += 1

    def close(self) -> None:  # pragma: no cover - mirrors the harness ports
        """Nothing to close: a counter holds no runtime."""


class CountingSeat:
    """An execution seat that records the turns it is handed and does nothing."""

    def __init__(self) -> None:
        self.started: list[str] = []
        self.turns: list[tuple[str, str]] = []

    async def start(self, node: DagNode) -> None:
        self.started.append(node.node_id)

    async def act(self, node: DagNode, situation: Situation) -> None:
        _ = situation
        self.turns.append((node.node_id, node.status.value))


class StartingSeat(CountingSeat):
    """A compute seat whose `start` reports the Worker's own DAG transition.

    The transition is what production's Worker does through its authorized
    tool, and it is what puts the node in RUNNING — the state a long job spends
    its time in. Done through the repository rather than by writing the row, so
    the situation the next round reads is one the system can actually reach.
    """

    def __init__(self, database: Database) -> None:
        super().__init__()
        self.database = database

    async def start(self, node: DagNode) -> None:
        self.started.append(node.node_id)
        with self.database.transaction() as session:
            DagRepository(session, node.project_id).transition_node(
                node.node_id, NodeStatus.RUNNING, actor_id="compute-worker"
            )


def wiring(
    database: Database,
    *,
    clock: FakeClock | None = None,
    cooldowns: tuple[float, ...] = DEFAULT_COOLDOWNS,
    timeout: float = 180.0,
    per_project: int = 2,
    global_requests: int = 6,
) -> RetryWiring:
    """The durable budget with the policy the test is about.

    The shipped cooldowns by default; a test that wants to run the nine
    attempts without waiting passes zeroes, which changes the waits and nothing
    else — the budget, the operator gate and the turn timeout are the same.
    """
    return RetryWiring.create(
        database,
        policy=RetryPolicy(
            cooldowns_seconds=cooldowns,
            turn_timeout_seconds=timeout,
            per_project_requests=per_project,
            global_requests=global_requests,
        ),
        clock=(clock or FakeClock()).now,
    )


def make_loop(
    database: Database,
    project_id: str,
    *,
    retry: RetryWiring | None = None,
    master: CountingPort | None = None,
    review: CountingPort | None = None,
    compute_worker: CountingSeat | None = None,
    experimental_worker: CountingSeat | None = None,
    research: CountingSeat | None = None,
    poll_seconds: float = 0.0,
    max_rounds: int = 10,
    max_stalled_rounds: int = 3,
    max_turns_per_question: int = 3,
    lease_guard: object | None = None,
) -> ProjectLoop:
    """One loop, with every port a counter unless the test supplied its own."""
    return ProjectLoop(
        database=database,
        project_id=project_id,
        master=master or CountingPort(),
        review=review or CountingPort(),
        compute_worker=compute_worker or CountingSeat(),
        experimental_worker=experimental_worker or CountingSeat(),
        research=research or CountingSeat(),
        poll_seconds=poll_seconds,
        max_rounds=max_rounds,
        max_stalled_rounds=max_stalled_rounds,
        max_turns_per_question=max_turns_per_question,
        lease_guard=lease_guard,  # type: ignore[arg-type]
        retry=retry,
    )


def transition_project(
    database: Database,
    project_id: str,
    *statuses: ProjectStatus,
    actor_id: str = "operator",
) -> ProjectStatus:
    """Move a project along its life cycle the way the services do."""
    with database.transaction() as session:
        registry = ProjectRegistry(session)
        project = registry.get(project_id)
        for status in statuses:
            project = registry.transition(
                project_id,
                status,
                actor_id=actor_id,
                reason="T05 test",
            )
        return project.status


def read_project(database: Database, project_id: str) -> ProjectStatus:
    """The project's authoritative status, read back from PostgreSQL."""
    with database.read_only() as session:
        return ProjectRegistry(session).get(project_id).status


def executable_everything() -> frozenset[NodeType]:
    """The seat set a loop holds in these tests: all three executing roles."""
    return frozenset(
        {NodeType.COMPUTATION, NodeType.EXPERIMENT, NodeType.RESEARCH}
    )
