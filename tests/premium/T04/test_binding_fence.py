"""T04 regression: a stale runtime binding cannot mutate the DAG or start work.

The supervisor's turn-level lease guard is not enough for a turn that is
already in flight. These tests call the deterministic entry points a tool
server holds — the same functions the MCP registry exposes — with a brief that
names an old owner/epoch, after a new owner has taken over.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text

from ravel.domain.enums import JobState, NodeStatus
from ravel.domain.execution import BackendJob
from ravel.domain.reconciliation import WorkflowLiveness
from ravel.domain.roles import AgentRole
from ravel.execution.leases import LeaseService
from ravel.execution.reconcile import ExecutionReconciler
from ravel.mcp.context import ToolContext
from ravel.mcp.scope import ToolScope
from ravel.mcp.tools.dag import add_dag_node
from ravel.mcp.tools.execution import start_execution
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import BackendJobRepository


def _brief(
    path: Path,
    project_id: str,
    role: AgentRole,
    *,
    owner: str | None,
    epoch: int | None,
    binding_id: str | None,
) -> Path:
    payload: dict[str, object] = {
        "project_id": project_id,
        "role": role.value,
    }
    if owner is not None:
        payload["owner_instance_id"] = owner
    if epoch is not None:
        payload["lease_epoch"] = epoch
    if binding_id is not None:
        payload["binding_id"] = binding_id
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _context(
    database: Database,
    project_id: str,
    role: AgentRole,
    brief: Path,
    *,
    execution: object | None = None,
) -> ToolContext:
    return ToolContext(
        scope=ToolScope(project_id=project_id, role=role, brief_path=brief),
        database=database,
        execution=execution,  # type: ignore[arg-type]
    )


def _expire(database: Database, project_id: str) -> None:
    with database.transaction() as session:
        session.execute(
            text(
                "UPDATE project_leases "
                "SET expires_at = now() - interval '1 second' "
                "WHERE project_id = :project_id"
            ),
            {"project_id": project_id},
        )


async def test_stale_binding_cannot_mutate_the_dag(
    database: Database, project, tmp_path: Path
) -> None:
    service = LeaseService(database)
    old = service.acquire(project.project_id, "supervisor-a")
    old_brief = _brief(
        tmp_path / "old.json",
        project.project_id,
        AgentRole.MASTER,
        owner="supervisor-a",
        epoch=old.epoch,
        binding_id="binding-a",
    )
    old_context = _context(database, project.project_id, AgentRole.MASTER, old_brief)

    first = await add_dag_node(old_context)(
        "RESEARCH", "Survey the literature.", "The plan needs a source survey."
    )
    assert first["node_id"]

    _expire(database, project.project_id)
    new = service.acquire(project.project_id, "supervisor-b")
    new_brief = _brief(
        tmp_path / "new.json",
        project.project_id,
        AgentRole.MASTER,
        owner="supervisor-b",
        epoch=new.epoch,
        binding_id="binding-b",
    )
    new_context = _context(database, project.project_id, AgentRole.MASTER, new_brief)

    with pytest.raises(PermissionError):
        await add_dag_node(old_context)(
            "RESEARCH", "A second survey.", "The stale owner must not add it."
        )

    second = await add_dag_node(new_context)(
        "RESEARCH", "A current survey.", "The new owner may add it."
    )
    assert second["node_id"] != first["node_id"]


async def test_stale_binding_cannot_start_execution(
    database: Database, project, prepare, tmp_path: Path
) -> None:
    class FakeExecution:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def start(self, node: object, *, actor_id: str | None = None) -> bool:
            self.calls.append(node)
            return True

    prepared = prepare()
    service = LeaseService(database)
    old = service.acquire(project.project_id, "supervisor-a")
    old_brief = _brief(
        tmp_path / "old-worker.json",
        project.project_id,
        AgentRole.COMPUTE_WORKER,
        owner="supervisor-a",
        epoch=old.epoch,
        binding_id="binding-a",
    )
    fake = FakeExecution()
    old_context = _context(
        database,
        project.project_id,
        AgentRole.COMPUTE_WORKER,
        old_brief,
        execution=fake,
    )

    _expire(database, project.project_id)
    new = service.acquire(project.project_id, "supervisor-b")

    with pytest.raises(PermissionError):
        await start_execution(old_context)(prepared.node_id)
    assert fake.calls == []
    new_brief = _brief(
        tmp_path / "new-worker.json",
        project.project_id,
        AgentRole.COMPUTE_WORKER,
        owner="supervisor-b",
        epoch=new.epoch,
        binding_id="binding-b",
    )
    new_context = _context(
        database,
        project.project_id,
        AgentRole.COMPUTE_WORKER,
        new_brief,
        execution=fake,
    )
    result = await start_execution(new_context)(prepared.node_id)
    assert result["started"] is True
    assert len(fake.calls) == 1


async def test_takeover_recovers_the_submitted_job_without_resubmitting(
    database: Database, project, prepare, tmp_path: Path
) -> None:
    """B recovers A's in-flight job; neither A nor B submits a second one."""
    prepared = prepare()
    service = LeaseService(database)
    old = service.acquire(project.project_id, "supervisor-a")
    old_brief = _brief(
        tmp_path / "old-recover.json",
        project.project_id,
        AgentRole.COMPUTE_WORKER,
        owner="supervisor-a",
        epoch=old.epoch,
        binding_id="binding-a",
    )

    with database.transaction() as session:
        DagRepository(session, project.project_id).transition_node(
            prepared.node_id, NodeStatus.RUNNING, actor_id="compute-worker"
        )
        version = ExecutionContractRepository(
            session, project.project_id
        ).for_node(prepared.node_id).version
        jobs = BackendJobRepository(session, project.project_id)
        job = jobs.start(
            BackendJob(
                project_id=project.project_id,
                node_id=prepared.node_id,
                attempt=1,
                execution_contract_ref=prepared.contract.contract_id,
                execution_contract_version=version,
                backend="mock-compute",
            )
        )
        jobs.record_state(
            job.job_id, JobState.RUNNING, backend_state=JobState.RUNNING.value
        )

    class FakeExecution:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def start(self, node: object, *, actor_id: str | None = None) -> bool:
            self.calls.append(node)
            return True

    fake = FakeExecution()
    old_context = _context(
        database,
        project.project_id,
        AgentRole.COMPUTE_WORKER,
        old_brief,
        execution=fake,
    )

    _expire(database, project.project_id)
    service.acquire(project.project_id, "supervisor-b")

    with pytest.raises(PermissionError):
        await start_execution(old_context)(prepared.node_id)
    assert fake.calls == []

    def job_count() -> int:
        with database.read_only() as session:
            return int(
                session.execute(
                    text(
                        "SELECT count(*) FROM backend_jobs "
                        "WHERE project_id = :project_id"
                    ),
                    {"project_id": project.project_id},
                ).scalar_one()
            )

    assert job_count() == 1

    class Probe:
        async def liveness(self, workflow_id: str) -> WorkflowLiveness:
            return WorkflowLiveness.FAILED

    records = await ExecutionReconciler(
        database=database, probe=Probe(), grace_seconds=0.0
    ).reconcile([project.project_id])
    assert len(records) == 1
    assert job_count() == 1


async def test_unbound_runtime_is_refused(
    database: Database, project, tmp_path: Path
) -> None:
    brief = _brief(
        tmp_path / "unbound.json",
        project.project_id,
        AgentRole.MASTER,
        owner=None,
        epoch=None,
        binding_id=None,
    )
    context = _context(database, project.project_id, AgentRole.MASTER, brief)
    with pytest.raises(PermissionError):
        await add_dag_node(context)(
            "RESEARCH", "Unbound.", "A runtime without a lease binding may not write."
        )
