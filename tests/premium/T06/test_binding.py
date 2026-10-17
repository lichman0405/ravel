"""T06-AC2: an approval covers exact content, and only live people can execute.

The approval snapshot is the record of what a human said yes to. These tests
change each fact it names, one at a time, and require the pre-execution check to
refuse the Action — and they check the positive case too, because a check that
refuses everything proves nothing.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from tests.integration.conftest import Prepared
from tests.premium.T06.support import install_envelope, mail_payload

from ravel.actions.service import ActionService
from ravel.budgets.service import BudgetService
from ravel.config import Settings
from ravel.domain.actions import BudgetExceeded, StaleApproval
from ravel.domain.clock import utcnow
from ravel.domain.contracts import ExecutionContract
from ravel.domain.enums import (
    ActionStatus,
    ApprovalStatus,
    BudgetReservationStatus,
    UserRole,
)
from ravel.domain.identity import User
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.identity import (
    MembershipRepository,
    UserRepository,
)
from ravel.state.tables import ActionRow, ApprovalRequestRow


def _approved_mail(
    database: Database,
    project: Project,
    settings: Settings,
    *,
    message_id: str = "<binding@ravel.example>",
    estimate: str = "0.60",
    expires_at: Any = None,
) -> tuple[str, str]:
    """Create a mail Action, approve it, and return `(action_id, approval_id)`."""
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")
        budget = BudgetService(session, project.project_id, settings=settings)
        if budget.account("default") is None:
            budget.configure(limit_cny="10.00", configured_by=owner)
        service = ActionService(session, project.project_id, settings=settings)
        action = service.create(
            kind="MAIL_SEND",
            payload=mail_payload(message_id),
            actor=owner,
            requesting_binding="runtime-binding-1",
            target_ref="mailbox:primary",
            estimated_cny=estimate,
            expires_at=expires_at,
        )
        assert action.status is ActionStatus.AWAITING_APPROVAL
        assert action.required_approval_ref is not None
        ready = service.approve(action.action_id, resolved_by=owner)
        assert ready.status is ActionStatus.READY
        return action.action_id, action.required_approval_ref


def _tamper(session: Session, approval_id: str, key: str, value: object) -> None:
    """Rewrite one field of the stored approval snapshot, as a database bug would."""
    row = session.get(ApprovalRequestRow, approval_id)
    assert row is not None
    payload = dict(row.payload)
    payload[key] = value
    row.payload = payload
    session.flush()


def test_an_untampered_approval_still_claims(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """The control: the positive path has to work, or the refusals mean nothing."""
    action_id, _approval_id = _approved_mail(database, project, integration_settings)

    with database.transaction() as session:
        action, claimed = ActionService(
            session, project.project_id, settings=integration_settings
        ).claim(action_id, claimed_by=project.created_by)

    assert claimed is True
    assert action.status is ActionStatus.CLAIMED


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("action_id", "action_rewritten"),
        ("payload_hash", "sha256:" + "f" * 64),
        ("to", ["attacker@example.org"]),
        ("cc", ["cc@example.org"]),
        ("bcc", ["bcc@example.org"]),
        ("attachment_hashes", ["sha256:" + "a" * 64]),
        ("target_ref", "mailbox:attacker"),
        ("contract_version", 7),
        ("envelope_version", 99),
        ("cost_upper_bound_cny", "9.99"),
        ("expires_at", "2000-01-01T00:00:00.000000+00:00"),
    ],
    ids=[
        "action-id",
        "payload-hash",
        "to",
        "cc",
        "bcc",
        "attachments",
        "target",
        "contract",
        "envelope",
        "cost",
        "expiry",
    ],
)
def test_a_changed_approval_snapshot_blocks_execution(
    database: Database,
    project: Project,
    integration_settings: Settings,
    field: str,
    replacement: object,
) -> None:
    """Every fact the approval names is checked again before the claim."""
    action_id, approval_id = _approved_mail(
        database, project, integration_settings
    )

    with database.transaction() as session:
        _tamper(session, approval_id, field, replacement)

    with pytest.raises(StaleApproval), database.transaction() as session:
        ActionService(
            session, project.project_id, settings=integration_settings
        ).claim(action_id, claimed_by=project.created_by)

    with database.read_only() as session:
        remaining = ActionService(
            session, project.project_id, settings=integration_settings
        ).get(action_id)
        assert remaining.status is ActionStatus.READY, (
            "the refused claim left the Action in a non-executable state it did "
            "not start in"
        )


def test_action_content_cannot_be_rewritten_after_insert(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """The row-level guard makes "edit the Action instead" impossible too.

    A service check alone would leave the stored content editable by anything
    that can reach SQL; the identity trigger is what makes the approval bind
    the record rather than a copy of it.
    """
    action_id, _approval_id = _approved_mail(database, project, integration_settings)

    with pytest.raises(IntegrityError), database.transaction() as session:
        session.execute(
            update(ActionRow)
            .where(ActionRow.action_id == action_id)
            .values(payload=mail_payload("<rewritten@ravel.example>"))
        )

    with database.read_only() as session:
        action = ActionService(
            session, project.project_id, settings=integration_settings
        ).get(action_id)
        assert action.payload["message_id"] == "<binding@ravel.example>"


def test_a_new_envelope_version_invalidates_the_approval(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """An Action approved under v1 may not run once v2 exists."""
    action_id, _approval_id = _approved_mail(database, project, integration_settings)

    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")

    with pytest.raises(StaleApproval, match="AuthorityEnvelope"), database.transaction() as session:
        ActionService(
            session, project.project_id, settings=integration_settings
        ).claim(action_id, claimed_by=project.created_by)


def test_a_new_execution_contract_version_invalidates_the_approval(
    database: Database,
    project: Project,
    integration_settings: Settings,
    prepare: Any,
) -> None:
    """A compute Action is bound to the frozen contract a human approved."""
    prepared: Prepared = prepare()
    owner = project.created_by
    with database.transaction() as session:
        install_envelope(session, project.project_id, "COMPUTE_SUBMIT")
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
            estimated_cny="0",
        )
        service.approve(action.action_id, resolved_by=owner)

    with database.transaction() as session:
        superseding = ExecutionContract(
            project_id=prepared.contract.project_id,
            node_id=prepared.contract.node_id,
            version=prepared.contract.version + 1,
            objective=prepared.contract.objective,
            procedure=prepared.contract.procedure,
            inputs=prepared.contract.inputs,
            allowed_actions=prepared.contract.allowed_actions,
            parameter_targets=dict(prepared.contract.parameter_targets),
            allowed_ranges=dict(prepared.contract.allowed_ranges),
            allowed_retries=prepared.contract.allowed_retries,
            allowed_substitutions=prepared.contract.allowed_substitutions,
            required_outputs=prepared.contract.required_outputs,
            resource_limits=dict(prepared.contract.resource_limits),
            execution_requirements=dict(prepared.contract.execution_requirements),
        )
        repository = ExecutionContractRepository(session, project.project_id)
        repository.add(superseding)
        repository.freeze(superseding.contract_id)

    with pytest.raises(StaleApproval, match="contract"), database.transaction() as session:
        ActionService(
            session, project.project_id, settings=integration_settings
        ).claim(action.action_id, claimed_by=AgentRole.COMPUTE_WORKER.value)


def test_a_lowered_budget_after_approval_blocks_the_claim(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """The cost bound was approved against the budget as it stood; a lower limit refuses."""
    action_id, _approval_id = _approved_mail(database, project, integration_settings)

    with database.transaction() as session:
        BudgetService(session, project.project_id, settings=integration_settings).configure(
            limit_cny="0.10", configured_by=project.created_by
        )

    with pytest.raises(BudgetExceeded), database.transaction() as session:
        ActionService(
            session, project.project_id, settings=integration_settings
        ).claim(action_id, claimed_by=project.created_by)


def test_a_revoked_approver_can_no_longer_authorize_execution(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """Revocation takes effect on the next claim, not at token expiry.

    The Action was validly approved. A second owner then withdraws the first
    owner's membership. The agent that requested the Action is still a
    legitimate claimant, but the human whose yes it carries is no longer a
    member, so the execution is refused.
    """
    owner = project.created_by
    with database.transaction() as session:
        coowner: User = UserRepository(session).create(
            username="coowner", password_hash="x"
        )
        MembershipRepository(session, project.project_id).grant(
            user_id=coowner.user_id,
            role=UserRole.PROJECT_OWNER,
            granted_by=owner,
        )

    with database.transaction() as session:
        install_envelope(session, project.project_id, "MAIL_SEND")
        BudgetService(session, project.project_id, settings=integration_settings).configure(
            limit_cny="10.00", configured_by=owner
        )
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        action = service.create(
            kind="MAIL_SEND",
            payload=mail_payload("<revoked-approver@ravel.example>"),
            actor=AgentRole.MASTER.value,
            requesting_binding="runtime-binding-1",
            target_ref="mailbox:primary",
            estimated_cny="0.60",
        )
        service.approve(action.action_id, resolved_by=owner)

    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            owner, revoked_by=coowner.user_id
        )

    with (
        pytest.raises(PermissionError, match="no longer a member"),
        database.transaction() as session,
    ):
        ActionService(
            session, project.project_id, settings=integration_settings
        ).claim(action.action_id, claimed_by=AgentRole.MASTER.value)


def test_an_elapsed_window_expires_the_action_and_releases_its_budget(
    database: Database,
    project: Project,
    integration_settings: Settings,
) -> None:
    """A window that closes releases the promise; the human's yes stays on record.

    The approval answer is a historical fact and is not rewritten to EXPIRED.
    What stops the work is the Action's own window: the claim is refused, so
    the stale answer cannot authorize anything after the fact.
    """
    expires_at = utcnow() + timedelta(seconds=30)
    action_id, approval_id = _approved_mail(
        database,
        project,
        integration_settings,
        message_id="<elapsed@ravel.example>",
        expires_at=expires_at,
    )

    with database.transaction() as session:
        expired = ActionService(
            session, project.project_id, settings=integration_settings
        ).expire_due(at=expires_at + timedelta(seconds=1))
        assert [action.action_id for action in expired] == [action_id]

    with database.read_only() as session:
        service = ActionService(
            session, project.project_id, settings=integration_settings
        )
        action = service.get(action_id)
        approval = service.approvals.get(approval_id=approval_id)
        reservation = service.budgets.reservations.for_action(action_id)

    assert action.status is ActionStatus.EXPIRED
    assert approval.status is ApprovalStatus.APPROVED, (
        "the human's answer was rewritten after the fact; the Action window "
        "closed, but what the person said did not change"
    )
    assert reservation is not None
    assert reservation.status is BudgetReservationStatus.RELEASED

    with database.transaction() as session:
        late, claimed = ActionService(
            session, project.project_id, settings=integration_settings
        ).claim(action_id, claimed_by=project.created_by)
    assert claimed is False, "an expired Action was handed out for execution"
    assert late.status is ActionStatus.EXPIRED
