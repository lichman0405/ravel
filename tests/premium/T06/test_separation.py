"""T06-AC4: Action execution does not become a second door into the DAG.

The four roles that are not Master may request work, but they cannot add or
cancel nodes, and running an Action to completion leaves the node's status and
its frozen contracts exactly as they were. The Direct-SQL check is the floor
underneath the role checks: even a caller that skips the repository cannot
rewrite criteria that were frozen.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from tests.integration.conftest import Prepared
from tests.premium.T06.support import install_envelope

from ravel.actions.service import ActionService
from ravel.budgets.service import BudgetService
from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.domain.enums import ActionStatus, NodeStatus, NodeType
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.tables import AcceptanceContractRow

NON_MASTER_ROLES = (
    AgentRole.RESEARCH,
    AgentRole.REVIEW,
    AgentRole.COMPUTE_WORKER,
    AgentRole.EXPERIMENTAL_WORKER,
)


@pytest.mark.parametrize("role", NON_MASTER_ROLES, ids=lambda role: role.value)
def test_a_non_master_role_cannot_add_a_dag_node(
    database: Database,
    project: Project,
    role: AgentRole,
) -> None:
    with database.transaction() as session:
        repository = DagRepository(session, project.project_id)
        before = len(repository.nodes())
        node = DagNode.create(
            project_id=project.project_id,
            node_type=NodeType.COMPUTATION,
            objective="Try to slip a node in.",
            created_by=role.value,
        )
        with pytest.raises(PermissionError, match="only master"):
            repository.add_node(node, role=role, decision_ref="dec-sneaky")
        assert len(repository.nodes()) == before, (
            "the refused add left a node behind"
        )


@pytest.mark.parametrize("role", NON_MASTER_ROLES, ids=lambda role: role.value)
def test_a_non_master_role_cannot_cancel_a_node(
    database: Database,
    project: Project,
    prepare: Any,
    role: AgentRole,
) -> None:
    prepared: Prepared = prepare()
    with database.transaction() as session:
        repository = DagRepository(session, project.project_id)
        with pytest.raises(PermissionError, match="only master"):
            repository.cancel_node(
                prepared.node_id, role=role, decision_ref="dec-cancel"
            )
        assert repository.node(prepared.node_id).status is NodeStatus.READY


def test_a_compute_action_lifecycle_leaves_the_dag_and_frozen_criteria_alone(
    database: Database,
    project: Project,
    integration_settings: Settings,
    prepare: Any,
) -> None:
    """A Worker completes a run; none of that is a scientific decision.

    Starting work, finishing it, and reporting a known cost are reports about
    an activity. The node's status, its acceptance criteria, and its execution
    contract stay exactly as Master froze them.
    """
    prepared: Prepared = prepare()
    assert prepared.acceptance is not None
    owner = project.created_by
    criteria_before = tuple(
        criterion.statement for criterion in prepared.acceptance.criteria
    )

    with database.transaction() as session:
        install_envelope(session, project.project_id, "COMPUTE_SUBMIT")
        BudgetService(session, project.project_id, settings=integration_settings).configure(
            limit_cny="10.00", configured_by=owner
        )
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        action = service.create(
            kind="COMPUTE_SUBMIT",
            payload={"attempt": 1},
            actor=AgentRole.COMPUTE_WORKER.value,
            requesting_binding="runtime-binding-1",
            node_id=prepared.node_id,
            contract_id=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            estimated_cny="2.50",
        )
        service.approve(action.action_id, resolved_by=owner)

    with database.transaction() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        claimed, won = service.claim(
            action.action_id, claimed_by=AgentRole.COMPUTE_WORKER.value
        )
        assert won is True
        service.record_accepted(
            action.action_id, external_ref="slurm:job-42"
        )
        finished = service.complete(
            action.action_id, actual_cny="2.50", external_ref="slurm:job-42"
        )
    assert claimed.status is ActionStatus.CLAIMED
    assert finished.status is ActionStatus.COMPLETED

    with database.read_only() as session:
        node_status = DagRepository(session, project.project_id).node(
            prepared.node_id
        ).status
        acceptance = AcceptanceContractRepository(
            session, project.project_id
        ).for_node(prepared.node_id)
        contract = ExecutionContractRepository(
            session, project.project_id
        ).for_node(prepared.node_id)
        criteria_after = tuple(
            criterion.statement for criterion in acceptance.criteria
        )

    assert node_status is NodeStatus.READY, (
        "executing an Action moved the Scientific DAG; only Master may do that"
    )
    assert acceptance.contract_id == prepared.acceptance.contract_id
    assert acceptance.is_frozen
    assert criteria_after == criteria_before
    assert contract.contract_id == prepared.contract.contract_id
    assert contract.version == prepared.contract.version
    assert contract.is_frozen


def test_frozen_criteria_cannot_be_edited_even_directly(
    database: Database,
    project: Project,
    prepare: Any,
) -> None:
    """The database refuses the shortcut, not just the role-gated path."""
    prepared: Prepared = prepare()
    assert prepared.acceptance is not None

    with pytest.raises(IntegrityError), database.transaction() as session:
        session.execute(
            update(AcceptanceContractRow)
            .where(
                AcceptanceContractRow.contract_id
                == prepared.acceptance.contract_id
            )
            .values(
                criteria=[
                    {
                        "statement": "Anything the runner wants.",
                        "provenance": "USER_REQUIREMENT",
                    }
                ]
            )
        )

    expected = tuple(
        criterion.statement for criterion in prepared.acceptance.criteria
    )
    with database.read_only() as session:
        unchanged = AcceptanceContractRepository(
            session, project.project_id
        ).for_node(prepared.node_id)
        actual = tuple(criterion.statement for criterion in unchanged.criteria)
    assert actual == expected
