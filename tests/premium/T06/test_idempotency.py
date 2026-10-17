"""T06-AC3: a repeated submission returns the original Action, never a second one.

RAVEL has two layers of duplicate protection and the tests exercise both: the
unique `(project_id, idempotency_key)` row is what makes a repeated *call*
return the original, and the HPC submission key / mail Message-ID are the
identifiers the external systems can honour. A duplicate that reached the
external system twice would be a real second effect, so the claim flag — not
just the row count — is asserted.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session
from tests.integration.conftest import Prepared
from tests.premium.T06.support import create_mail, install_envelope

from ravel.actions.idempotency import compute_submission_key
from ravel.actions.service import ActionService
from ravel.budgets.service import BudgetService
from ravel.config import Settings
from ravel.domain.actions import ActionError
from ravel.domain.enums import ActionStatus
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database


def _service(session: Session, project: Project, settings: Settings) -> ActionService:
    return ActionService(session, project.project_id, settings=settings)


def test_a_repeated_mail_message_id_returns_the_original_action(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """The same Message-ID with different prose is still the same send."""
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")
        service = _service(session, project, integration_settings)
        first = create_mail(
            service,
            owner,
            message_id="<same-send@ravel.example>",
            subject="Original subject",
            estimate="0.60",
        )
        replay = create_mail(
            service,
            owner,
            message_id="<same-send@ravel.example>",
            subject="Rewritten subject",
            estimate="9.99",
        )

    assert replay.action_id == first.action_id
    assert replay.payload["subject"] == "Original subject", (
        "the replay returned the new content rather than the original; the "
        "external Message-ID names one send, and the caller must see what that "
        "send actually says"
    )
    assert replay.estimated_cny == "0.6"

    with database.read_only() as session:
        actions = _service(session, project, integration_settings).list()
        assert len(actions) == 1
        assert actions[0].idempotency_key == "mail:<same-send@ravel.example>"


def test_a_held_action_is_claimed_once_even_when_claimed_twice(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """The second claimant is told it lost instead of submitting again.

    A caller that re-claims after an uncertain response must receive
    `(action, False)` and touch nothing external; `attempts` staying at one is
    the record that no second attempt was started.
    """
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")
        BudgetService(
            session, project.project_id, settings=integration_settings
        ).configure(limit_cny="10.00", configured_by=owner)
        service = _service(session, project, integration_settings)
        action = create_mail(
            service, owner, message_id="<double-claim@ravel.example>"
        )
        service.approve(action.action_id, resolved_by=owner)

    with database.transaction() as session:
        service = _service(session, project, integration_settings)
        first, won = service.claim(action.action_id, claimed_by=owner)
        second, won_again = service.claim(action.action_id, claimed_by=owner)

    assert won is True
    assert first.status is ActionStatus.CLAIMED
    assert won_again is False, (
        "a second claim on a CLAIMED Action reported success; a caller would "
        "submit the mail a second time"
    )
    assert second.status is ActionStatus.CLAIMED
    assert len(second.attempts) == 1, (
        f"the duplicate claim started another attempt: {second.attempts}"
    )


def test_the_hpc_submission_key_is_the_attempt_identity(
    database: Database,
    project: Project,
    integration_settings: Settings,
    prepare: Any,
) -> None:
    """Same attempt replays to one Action; a real retry is a new attempt and key."""
    prepared: Prepared = prepare()
    actor = AgentRole.COMPUTE_WORKER.value
    expected_key = compute_submission_key(
        project_id=project.project_id,
        node_id=prepared.node_id,
        contract_version=prepared.contract.version,
        attempt=1,
    )

    with database.transaction() as session:
        service = _service(session, project, integration_settings)
        first = service.create(
            kind="COMPUTE_SUBMIT",
            payload={"attempt": 1},
            actor=actor,
            requesting_binding="runtime-binding-1",
            node_id=prepared.node_id,
            contract_id=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            estimated_cny="0",
        )
        first_replay = service.create(
            kind="COMPUTE_SUBMIT",
            payload={"attempt": 1},
            actor=actor,
            requesting_binding="runtime-binding-1",
            node_id=prepared.node_id,
            contract_id=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            estimated_cny="0",
        )
        retry = service.create(
            kind="COMPUTE_SUBMIT",
            payload={"attempt": 2},
            actor=actor,
            requesting_binding="runtime-binding-1",
            node_id=prepared.node_id,
            contract_id=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            estimated_cny="0",
        )

    assert first_replay.action_id == first.action_id
    assert first.idempotency_key == expected_key, (
        "the Action's stored key is not the HPC submission key this attempt "
        "computes; a restarted process would submit under a different identity"
    )
    assert first.payload["submission_key"] == expected_key
    assert retry.action_id != first.action_id
    assert retry.idempotency_key != first.idempotency_key
    assert retry.payload["submission_key"] != first.payload["submission_key"]


def test_a_hand_written_hpc_submission_key_is_refused(
    database: Database,
    project: Project,
    integration_settings: Settings,
    prepare: Any,
) -> None:
    """A key RAVEL did not compute cannot promise the same submission twice."""
    prepared: Prepared = prepare()
    with pytest.raises(ActionError, match="submission_key"), database.transaction() as session:
        _service(session, project, integration_settings).create(
            kind="COMPUTE_SUBMIT",
            payload={"attempt": 1, "submission_key": "sha256:" + "0" * 64},
            actor=AgentRole.COMPUTE_WORKER.value,
            requesting_binding="runtime-binding-1",
            node_id=prepared.node_id,
            contract_id=prepared.contract.contract_id,
            contract_version=prepared.contract.version,
            estimated_cny="0",
        )
