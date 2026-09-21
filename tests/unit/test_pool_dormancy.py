"""Pool dormancy: an idle scope's runtime is reaped and recovered on demand.

The harness process itself is replaced here, because what is under test is
RAVEL's bookkeeping around it — which runtime a session is on, what a reap
does to that, and whether the next turn recovers. A real runtime is exercised
in `tests/dsh`.
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar

import pytest

from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.dsh import pool as pool_module
from ravel.dsh.agents import WorkerAgent
from ravel.dsh.pool import DshRuntimePool
from ravel.dsh.runtime import TurnOutcome
from ravel.execution.loop import Situation

SCOPE = ("p-1", AgentRole.COMPUTE_WORKER)


class _FakeRuntime:
    """As much of `RoleRuntime` as the pool and a session touch."""

    created: ClassVar[list[_FakeRuntime]] = []

    def __init__(self, project_id: str, role: AgentRole) -> None:
        self.project_id = project_id
        self.role = role
        self.work_dir = f"/tmp/{project_id}/{role.slug}"
        self.turns = 0
        self.last_used_at = datetime.now(UTC)
        self._lock = threading.Lock()
        self._closed = False
        self.sessions: list[str] = []
        _FakeRuntime.created.append(self)

    @property
    def is_closed(self) -> bool:
        return self._closed

    @classmethod
    def create(
        cls,
        _settings: Settings,
        project_id: str,
        role: AgentRole,
        _brief: dict[str, Any] | None = None,
    ) -> _FakeRuntime:
        return cls(project_id, role)

    def new_session_id(self, _task_id: str | None = None) -> str:
        return f"session-{len(_FakeRuntime.created)}-{len(self.sessions)}"

    def run_turn(self, session_id: str, _prompt: str, **_kwargs: Any) -> TurnOutcome:
        assert not self._closed, "a turn ran on a closed runtime"
        self.sessions.append(session_id)
        self.turns += 1
        self.last_used_at = datetime.now(UTC)
        return TurnOutcome(
            session_id=session_id,
            response="nothing to report",
            finish_reason="completed",
            events=(),
        )

    def close(self) -> None:
        self._closed = True


@pytest.fixture
def pool(tmp_path, monkeypatch: pytest.MonkeyPatch) -> DshRuntimePool:
    _FakeRuntime.created = []
    monkeypatch.setattr(pool_module, "RoleRuntime", _FakeRuntime)
    return DshRuntimePool(settings=Settings(runtime_dir=tmp_path / "runtime"))


def _node() -> DagNode:
    return DagNode.create(
        project_id="p-1",
        node_type=NodeType.COMPUTATION,
        objective="relax the cell",
        created_by="u1",
    ).model_copy(update={"status": NodeStatus.WAITING_EXTERNAL})


def _situation() -> Situation:
    return Situation(
        project=Project(title="screen", objective="test", created_by="u1"),
        nodes=(),
        pre_run={},
        open_deviations=(),
    )


async def test_idle_worker_scope_is_reaped_and_recovers_on_the_next_turn(
    pool: DshRuntimePool,
) -> None:
    worker = WorkerAgent(pool=pool, project_id="p-1", role=AgentRole.COMPUTE_WORKER)
    node, situation = _node(), _situation()

    await worker.act(node, situation)
    first_runtime = pool.live_runtime(*SCOPE)
    assert first_runtime is not None
    first_session = worker.session_id
    assert first_session is not None
    assert pool.binding_for(first_session) is not None

    # The Worker has nothing to do for a while — a backend is running, or the
    # lab is working — and the supervisor's tick reaps its idle runtime.
    first_runtime.last_used_at = datetime.now(UTC) - timedelta(hours=1)
    assert pool.reap_idle() == 1
    assert pool.live_runtime(*SCOPE) is None
    assert pool.binding_for(first_session) is None, (
        "a binding outlived the runtime that could serve it"
    )

    # The same agent object takes the next turn. It must recover on its own:
    # a new runtime, a session the new process minted, and a binding for it.
    await worker.act(node, situation)
    second_runtime = pool.live_runtime(*SCOPE)
    assert second_runtime is not None
    assert second_runtime is not first_runtime
    assert worker.session_id != first_session
    recovered_session = worker.session_id
    assert recovered_session is not None
    assert pool.binding_for(recovered_session) is not None
    assert worker.turns == 2


def test_acquiring_a_scope_reaps_idle_ones(pool: DshRuntimePool) -> None:
    """`runtime()` reaps first, which is what keeps a pool from only growing."""
    stale = pool.runtime(*SCOPE)
    stale.last_used_at = datetime.now(UTC) - timedelta(hours=1)

    fresh = pool.runtime("p-2", AgentRole.EXPERIMENTAL_WORKER)

    assert pool.live_runtime(*SCOPE) is None
    assert pool.stats().live_runtimes == 1
    assert fresh is not stale


def test_reap_leaves_a_runtime_that_is_mid_turn_alone(
    pool: DshRuntimePool,
) -> None:
    """A turn holds its runtime's lock, so a reap cannot close it underneath."""
    runtime = pool.runtime(*SCOPE)
    runtime.last_used_at = datetime.now(UTC) - timedelta(hours=1)

    with runtime._lock:
        assert pool.reap_idle() == 0
        assert pool.live_runtime(*SCOPE) is runtime

    # The turn ends: the same runtime is idle now, and eligible on the next reap.
    assert pool.reap_idle() == 1
    assert pool.live_runtime(*SCOPE) is None


def test_reap_is_a_no_op_when_nothing_is_idle(pool: DshRuntimePool) -> None:
    pool.runtime(*SCOPE)

    assert pool.reap_idle() == 0
    assert pool.stats().live_runtimes == 1


def test_turns_survive_the_reaping_of_the_runtime_that_served_them(
    pool: DshRuntimePool,
) -> None:
    """The work a seat has done is a fact about the seat, not about its runtime.

    Reaping is routine and silent, and it throws away the only in-memory trace
    of a turn. An unattended run is long enough for that to matter: a seat that
    worked early and has been idle since would otherwise be indistinguishable
    from one that was never asked to do anything — which is exactly the
    question `test_p10_17` asks of all five seats.
    """
    runtime = pool.runtime(*SCOPE)
    runtime.run_turn("s-1", "a prompt")

    runtime.last_used_at = datetime.now(UTC) - timedelta(hours=1)
    assert pool.reap_idle() == 1
    assert pool.live_runtime(*SCOPE) is None

    assert pool.turns_served(*SCOPE) == 1, (
        "a seat that took a turn and was reaped for being idle now reports no "
        "turn at all, so a long run cannot tell it from a seat never reached"
    )
    assert pool.stats().total_turns == 1


def test_turns_accumulate_across_every_runtime_a_scope_has_had(
    pool: DshRuntimePool,
) -> None:
    """The count is per scope, and a scope outlives the processes that serve it."""
    first = pool.runtime(*SCOPE)
    first.run_turn("s-1", "a prompt")
    first.run_turn("s-1", "another prompt")
    first.close()

    second = pool.runtime(*SCOPE)
    assert second is not first
    second.run_turn("s-2", "a third prompt")

    assert pool.turns_served(*SCOPE) == 3, "a turn was counted twice or lost"
    assert pool.stats().total_turns == 3

    # And what a scope did is not undone by the project ending: the count is
    # read after the supervisor stops, when every runtime has been closed.
    assert pool.close_project(SCOPE[0]) == 1
    assert pool.turns_served(*SCOPE) == 3
