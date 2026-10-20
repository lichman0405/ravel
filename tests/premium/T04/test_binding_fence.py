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

from ravel.domain.roles import AgentRole
from ravel.execution.leases import LeaseService
from ravel.mcp.context import ToolContext
from ravel.mcp.scope import ToolScope
from ravel.mcp.tools.dag import add_dag_node
from ravel.mcp.tools.execution import start_execution
from ravel.state.database import Database


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

    with pytest.raises(PermissionError):
        await start_execution(old_context)(prepared.node_id)
    assert fake.calls == []

    _expire(database, project.project_id)
    new = service.acquire(project.project_id, "supervisor-b")
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
