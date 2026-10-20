"""T09-AC2: an ACCEPTED Action holds a compute slot until its run ends.

`start_execution` records the Action and leaves it `ACCEPTED`; acceptance is
not completion. The slot the concurrency cap counts is released only when the
run reports an ending. These tests drive the real ActionService, the real
BudgetService and the real PostgreSQL rows — the only double is the Temporal
port, because starting a durable workflow is the external call this boundary
begins at.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.conftest import Prepared

from ravel.actions.repository import ActionRepository
from ravel.actions.service import ActionService
from ravel.domain.enums import ActionKind, ActionStatus, TerminationStatus
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.execution.leases import LeaseService
from ravel.execution.temporal.activities import _settle_node_action
from ravel.mcp.context import ToolContext
from ravel.mcp.scope import ToolScope
from ravel.mcp.tools.execution import start_execution
from ravel.state.database import Database


class FakeExecutionService:
    """The one external call: asking Temporal to start a durable run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def start(self, node: Any, *, actor_id: str | None = None) -> bool:
        self.calls.append(node.node_id)
        return True


def _context(
    database: Database,
    project: Project,
    tmp_path: Path,
    execution: FakeExecutionService,
) -> ToolContext:
    role = AgentRole.COMPUTE_WORKER
    lease = LeaseService(database).acquire(project.project_id, f"supervisor-{role.slug}")
    brief = tmp_path / f"brief-{role.slug}.json"
    brief.write_text(
        json.dumps(
            {
                "project_id": project.project_id,
                "role": role.value,
                "owner_instance_id": f"supervisor-{role.slug}",
                "lease_epoch": lease.epoch,
                "binding_id": f"binding-{role.slug}",
            }
        ),
        encoding="utf-8",
    )
    return ToolContext(
        scope=ToolScope(
            project_id=project.project_id, role=role, brief_path=brief
        ),
        database=database,
        execution=execution,  # type: ignore[arg-type]
    )


def _held(database: Database, project_id: str) -> int:
    with database.read_only() as session:
        return ActionRepository(session, project_id).held_for_concurrency(
            kind=ActionKind.COMPUTE_SUBMIT
        )


@pytest.mark.asyncio
async def test_an_accepted_action_holds_the_slot_until_the_run_ends(
    database: Database,
    project: Project,
    prepare: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAVEL_ACTION_COMPUTE_PER_PROJECT", "1")
    first: Prepared = prepare()
    second: Prepared = prepare()
    fake = FakeExecutionService()
    context = _context(database, project, tmp_path, fake)

    started = await start_execution(context)(first.node_id)
    assert started["started"] is True
    assert _held(database, project.project_id) == 1, (
        "the ACCEPTED Action did not occupy the project's one compute slot"
    )

    refused = await start_execution(context)(second.node_id)
    assert refused["started"] is False
    assert refused["refused"] is True
    assert "concurrent compute slots" in refused["reason"]
    assert fake.calls == [first.node_id], (
        "a start refused by the quota still asked the Execution Service for a run"
    )

    with database.transaction() as session:
        action = ActionService(session, project.project_id).live_for_node(first.node_id)
        assert action is not None
        action_id = action.action_id
        _settle_node_action(
            session,
            project_id=project.project_id,
            node_id=first.node_id,
            termination=TerminationStatus.COMPLETED,
        )

    with database.read_only() as session:
        assert ActionService(session, project.project_id).get(
            action_id
        ).status is ActionStatus.COMPLETED
    assert _held(database, project.project_id) == 0

    resumed = await start_execution(context)(second.node_id)
    assert resumed["started"] is True
    assert fake.calls == [first.node_id, second.node_id]


@pytest.mark.asyncio
async def test_a_failed_run_releases_the_slot_as_a_confirmed_failure(
    database: Database,
    project: Project,
    prepare: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAVEL_ACTION_COMPUTE_PER_PROJECT", "1")
    prepared: Prepared = prepare()
    context = _context(database, project, tmp_path, FakeExecutionService())

    assert (await start_execution(context)(prepared.node_id))["started"] is True

    with database.transaction() as session:
        action = ActionService(session, project.project_id).live_for_node(
            prepared.node_id
        )
        assert action is not None
        action_id = action.action_id
        _settle_node_action(
            session,
            project_id=project.project_id,
            node_id=prepared.node_id,
            termination=TerminationStatus.FAILED,
        )

    with database.read_only() as session:
        assert ActionService(session, project.project_id).get(
            action_id
        ).status is ActionStatus.FAILED_CONFIRMED
    assert _held(database, project.project_id) == 0


@pytest.mark.asyncio
async def test_a_timed_out_run_keeps_the_slot_until_it_is_reconciled(
    database: Database,
    project: Project,
    prepare: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAVEL_ACTION_COMPUTE_PER_PROJECT", "1")
    prepared: Prepared = prepare()
    context = _context(database, project, tmp_path, FakeExecutionService())

    assert (await start_execution(context)(prepared.node_id))["started"] is True

    with database.transaction() as session:
        action = ActionService(session, project.project_id).live_for_node(
            prepared.node_id
        )
        assert action is not None
        action_id = action.action_id
        _settle_node_action(
            session,
            project_id=project.project_id,
            node_id=prepared.node_id,
            termination=TerminationStatus.TIMED_OUT,
        )

    with database.read_only() as session:
        stored = ActionService(session, project.project_id).get(action_id)
        assert stored.status is ActionStatus.UNKNOWN
        assert "stopped is not known" in (stored.uncertainty or "")
    assert _held(database, project.project_id) == 1, (
        "a timed-out run whose cancellation may have been refused released the slot"
    )


def test_settlement_is_idempotent(
    database: Database,
    project: Project,
    prepare: Any,
) -> None:
    """A retried finish finds a terminal Action and leaves it alone."""
    prepared: Prepared = prepare()
    with database.transaction() as session:
        action = ActionService(session, project.project_id).create(
            kind=ActionKind.COMPUTE_SUBMIT,
            payload={"attempt": 1},
            actor=AgentRole.COMPUTE_WORKER.value,
            requesting_binding="binding-compute_worker",
            target_ref=f"node:{prepared.node_id}",
            node_id=prepared.node_id,
            contract_id=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            estimated_cny="0",
            cost_known=True,
        )
        action, claimed = ActionService(session, project.project_id).claim(
            action.action_id, claimed_by=AgentRole.COMPUTE_WORKER.value
        )
        assert claimed is True
        ActionService(session, project.project_id).record_accepted(action.action_id)
        action_id = action.action_id
        for _ in range(2):
            _settle_node_action(
                session,
                project_id=project.project_id,
                node_id=prepared.node_id,
                termination=TerminationStatus.COMPLETED,
            )

    with database.read_only() as session:
        assert ActionService(session, project.project_id).get(
            action_id
        ).status is ActionStatus.COMPLETED
    assert _held(database, project.project_id) == 0


def test_live_for_node_ignores_a_terminal_action(
    database: Database,
    project: Project,
    prepare: Any,
) -> None:
    """An older, already-settled Action is not the slot holder."""
    prepared: Prepared = prepare()
    with database.transaction() as session:
        service = ActionService(session, project.project_id)
        action = service.create(
            kind=ActionKind.COMPUTE_SUBMIT,
            payload={"attempt": 1},
            actor=AgentRole.COMPUTE_WORKER.value,
            requesting_binding="binding-compute_worker",
            target_ref=f"node:{prepared.node_id}",
            node_id=prepared.node_id,
            contract_id=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            estimated_cny="0",
            cost_known=True,
        )
        action, claimed = service.claim(
            action.action_id, claimed_by=AgentRole.COMPUTE_WORKER.value
        )
        assert claimed is True
        service.record_accepted(action.action_id)
        _settle_node_action(
            session,
            project_id=project.project_id,
            node_id=prepared.node_id,
            termination=TerminationStatus.COMPLETED,
        )
        assert service.live_for_node(prepared.node_id) is None
