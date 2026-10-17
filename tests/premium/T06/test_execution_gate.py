"""T06-AC1/AC4: the existing Worker entrance goes through the Action gate.

`start_execution` predates Actions, and the contracts say it must not remain a
way round approval, budget or lease. These tests call the real tool handler
against the real database, with the Execution Service replaced by a test double
because Temporal is the *external* submission in this chain: the double records
whether a run was asked for, and nothing below it is doubled.

The double is what makes the important assertion possible at all — a refusal
must leave it untouched. If the gate refused after asking the outside world for
a run, the Action would be a report rather than a control.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from tests.integration.conftest import Prepared
from tests.premium.T06.support import install_envelope

from ravel.actions.service import ActionService
from ravel.budgets.service import BudgetService
from ravel.domain.contracts import BudgetLimits
from ravel.domain.enums import ActionKind, ActionStatus, NodeStatus
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.execution.leases import LeaseService
from ravel.mcp.context import ToolContext
from ravel.mcp.scope import ToolScope
from ravel.mcp.tools.execution import start_execution
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository


class FakeExecutionService:
    """A test double for the one external call: asking Temporal to start.

    The real Execution Service starts a durable workflow. What the Action gate
    has to guarantee is that the call is made exactly once, only after the
    claim, and never on a refusal — so this double records the nodes it was
    asked for and answers on command.
    """

    def __init__(
        self,
        *,
        started: bool = True,
        raises: BaseException | None = None,
    ) -> None:
        self.started = started
        self.raises = raises
        self.calls: list[str] = []

    async def start(self, node: Any, *, actor_id: str | None = None) -> bool:
        self.calls.append(node.node_id)
        if self.raises is not None:
            raise self.raises
        return self.started


def _brief(
    path: Path,
    project_id: str,
    role: AgentRole,
    *,
    owner: str,
    epoch: int,
    binding_id: str,
) -> Path:
    payload = {
        "project_id": project_id,
        "role": role.value,
        "owner_instance_id": owner,
        "lease_epoch": epoch,
        "binding_id": binding_id,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _context(
    database: Database,
    project: Project,
    role: AgentRole,
    tmp_path: Path,
    execution: FakeExecutionService,
) -> ToolContext:
    lease = LeaseService(database).acquire(project.project_id, f"supervisor-{role.slug}")
    brief = _brief(
        tmp_path / f"brief-{role.slug}.json",
        project.project_id,
        role,
        owner=f"supervisor-{role.slug}",
        epoch=lease.epoch,
        binding_id=f"binding-{role.slug}",
    )
    return ToolContext(
        scope=ToolScope(project_id=project.project_id, role=role, brief_path=brief),
        database=database,
        execution=execution,  # type: ignore[arg-type]
    )


async def test_a_cleared_node_starts_through_one_claimed_action(
    database: Database,
    project: Project,
    integration_settings: Any,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """The start creates the Action, claims it once, then asks for the run."""
    prepared: Prepared = prepare()
    fake = FakeExecutionService()
    context = _context(database, project, AgentRole.COMPUTE_WORKER, tmp_path, fake)

    answer = await start_execution(context)(prepared.node_id)

    assert answer["started"] is True
    assert answer["refused"] is False
    assert answer["already_under_way"] is False
    assert fake.calls == [prepared.node_id], (
        "the gate admitted the start but the run was not asked for"
    )

    with database.read_only() as session:
        actions = ActionService(
            session, project.project_id, settings=integration_settings
        ).list()
    assert len(actions) == 1
    action = actions[0]
    assert action.kind is ActionKind.COMPUTE_SUBMIT
    assert action.status is ActionStatus.ACCEPTED, (
        "the Action did not record that the external system accepted the run"
    )
    assert action.node_id == prepared.node_id
    assert action.contract_version == prepared.contract.version
    assert action.requesting_binding == "binding-compute-worker"
    assert len(action.attempts) == 1
    assert action.budget_reservation_ref, "the run had no budget promise behind it"


async def test_a_repeated_start_is_the_same_run(
    database: Database,
    project: Project,
    integration_settings: Any,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """A second start reports the same run and does not submit a second one."""
    prepared: Prepared = prepare()
    fake = FakeExecutionService()
    context = _context(database, project, AgentRole.COMPUTE_WORKER, tmp_path, fake)
    tool = start_execution(context)

    first = await tool(prepared.node_id)
    again = await tool(prepared.node_id)

    assert first["started"] is True
    assert again["refused"] is False
    assert again["started"] is False
    assert again["already_under_way"] is True, (
        "the second start reported a new submission rather than the existing run"
    )
    assert fake.calls == [prepared.node_id], (
        "the second start reached the Execution Service again; the duplicate "
        "Action claim did not close the entrance"
    )

    with database.read_only() as session:
        actions = ActionService(
            session, project.project_id, settings=integration_settings
        ).list()
    assert len(actions) == 1
    assert len(actions[0].attempts) == 1, "a second claim was recorded for the same run"


async def test_a_required_approval_blocks_the_start_until_a_human_answers(
    database: Database,
    project: Project,
    integration_settings: Any,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """The Worker's own entrance cannot skip an approval the Envelope requires."""
    prepared: Prepared = prepare()
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "COMPUTE_SUBMIT")
        BudgetService(
            session, project.project_id, settings=integration_settings
        ).configure(limit_cny="10.00", configured_by=owner)
    fake = FakeExecutionService()
    context = _context(database, project, AgentRole.COMPUTE_WORKER, tmp_path, fake)
    tool = start_execution(context)

    refused = await tool(prepared.node_id)

    assert refused["refused"] is True
    assert refused["started"] is False
    assert "approval" in refused["reason"].lower()
    assert fake.calls == [], "an unapproved start reached the Execution Service"

    with database.read_only() as session:
        waiting = ActionService(
            session, project.project_id, settings=integration_settings
        ).list()
    assert len(waiting) == 1
    assert waiting[0].status is ActionStatus.AWAITING_APPROVAL
    assert waiting[0].required_approval_ref

    with database.transaction() as session:
        ActionService(
            session, project.project_id, settings=integration_settings
        ).approve(waiting[0].action_id, resolved_by=owner)

    approved = await tool(prepared.node_id)

    assert approved["started"] is True
    assert fake.calls == [prepared.node_id]


async def test_a_resource_cap_blocks_the_start_before_any_submission(
    database: Database,
    project: Project,
    integration_settings: Any,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """The V0 per-job CPU ceiling is checked at the existing entrance too."""
    prepared: Prepared = prepare(resource_limits={"cpu_hours": "2"})
    fake = FakeExecutionService()
    context = _context(database, project, AgentRole.COMPUTE_WORKER, tmp_path, fake)

    answer = await start_execution(context)(prepared.node_id)

    assert answer["refused"] is True
    assert answer["started"] is False
    assert "cpu_hours" in answer["reason"]
    assert fake.calls == []

    with database.read_only() as session:
        assert (
            ActionService(
                session, project.project_id, settings=integration_settings
            ).list()
            == []
        ), "a refused resource request left an Action behind"


async def test_an_envelope_can_tighten_the_deployment_resource_default(
    database: Database,
    project: Project,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """A stricter Envelope is intersected with the defaults, never widened by them."""
    prepared: Prepared = prepare(resource_limits={"cpu_hours": "0.5"})
    with database.transaction() as session:
        install_envelope(
            session,
            project.project_id,
            limits=BudgetLimits(max_compute_hours=0.25),
        )
    fake = FakeExecutionService()
    context = _context(database, project, AgentRole.COMPUTE_WORKER, tmp_path, fake)

    answer = await start_execution(context)(prepared.node_id)

    assert answer["refused"] is True
    assert "cpu_hours" in answer["reason"]
    assert fake.calls == []


async def test_an_unknown_external_result_blocks_a_second_submission(
    database: Database,
    project: Project,
    integration_settings: Any,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """When the start raises, the Action is UNKNOWN and is not submitted again."""
    prepared: Prepared = prepare()
    fake = FakeExecutionService(raises=RuntimeError("temporal connection lost"))
    context = _context(database, project, AgentRole.COMPUTE_WORKER, tmp_path, fake)
    tool = start_execution(context)

    with pytest.raises(RuntimeError, match="connection lost"):
        await tool(prepared.node_id)

    with database.read_only() as session:
        action = ActionService(
            session, project.project_id, settings=integration_settings
        ).list()[0]
    assert action.status is ActionStatus.UNKNOWN, (
        "an uncertain submission was recorded as a known outcome"
    )
    assert action.uncertainty and "connection lost" in action.uncertainty

    again = await tool(prepared.node_id)

    assert again["refused"] is True
    assert "cannot execute again" in again["reason"]
    assert fake.calls == [prepared.node_id], (
        "the uncertain Action was submitted a second time; UNKNOWN is held "
        "until it is reconciled"
    )


async def test_a_stale_binding_is_refused_before_the_action_layer_is_reached(
    database: Database,
    project: Project,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """T04's fence stays the first check; the gate does not move it."""
    prepared: Prepared = prepare()
    lease = LeaseService(database).acquire(project.project_id, "supervisor-old")
    brief = _brief(
        tmp_path / "stale.json",
        project.project_id,
        AgentRole.COMPUTE_WORKER,
        owner="supervisor-old",
        epoch=lease.epoch,
        binding_id="binding-old",
    )
    with database.transaction() as session:
        session.execute(
            text(
                "UPDATE project_leases SET expires_at = now() - interval '1 second' "
                "WHERE project_id = :project_id"
            ),
            {"project_id": project.project_id},
        )
    LeaseService(database).acquire(project.project_id, "supervisor-new")
    fake = FakeExecutionService()
    context = ToolContext(
        scope=ToolScope(
            project_id=project.project_id,
            role=AgentRole.COMPUTE_WORKER,
            brief_path=brief,
        ),
        database=database,
        execution=fake,  # type: ignore[arg-type]
    )

    with pytest.raises(PermissionError, match="stale"):
        await start_execution(context)(prepared.node_id)

    assert fake.calls == []
    with database.read_only() as session:
        assert ActionService(session, project.project_id).list() == [], (
            "a stale binding reached the Action layer and left a row"
        )


async def test_the_start_leaves_the_node_where_the_dag_put_it(
    database: Database,
    project: Project,
    prepare: Any,
    tmp_path: Path,
) -> None:
    """Recording an Action is not a scientific move; the node stays READY."""
    prepared: Prepared = prepare()
    fake = FakeExecutionService()
    context = _context(database, project, AgentRole.COMPUTE_WORKER, tmp_path, fake)

    await start_execution(context)(prepared.node_id)

    with database.read_only() as session:
        status = (
            DagRepository(session, project.project_id).node(prepared.node_id).status
        )
    assert status is NodeStatus.READY, (
        "the Action layer moved the DAG; starting a run is the Execution "
        "Service's transition to write, not this gate's"
    )
