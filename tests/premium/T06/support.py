"""Small builders shared by the T06 tests.

Nothing here writes around a service: envelopes go through
`AuthorityEnvelopeRepository`, budgets through `BudgetService.configure`, and
every Action through `ActionService.create`. The helpers only remove repetition,
so a test that fails is about the rule it names rather than about fixture
setup.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ravel.actions.service import ActionService
from ravel.domain.contracts import ApprovalRequirement, AuthorityEnvelope, BudgetLimits
from ravel.domain.enums import UserRole
from ravel.state.repositories.contracts import AuthorityEnvelopeRepository


def install_envelope(
    session: Session,
    project_id: str,
    *actions: str,
    role: UserRole = UserRole.PROJECT_OWNER,
    limits: BudgetLimits | None = None,
    version: int | None = None,
) -> AuthorityEnvelope:
    """Write an AuthorityEnvelope that requires `role` approval for `actions`."""
    repository = AuthorityEnvelopeRepository(session, project_id)
    envelope = AuthorityEnvelope(
        project_id=project_id,
        version=version if version is not None else repository.next_version(),
        requires_approval=tuple(
            ApprovalRequirement(action=action, required_role=role) for action in actions
        ),
        budget_time_limits=limits or BudgetLimits(),
    )
    return repository.add_version(envelope)


def mail_payload(
    message_id: str,
    *,
    to: tuple[str, ...] = ("pi@example.org",),
    subject: str = "Draft for review",
) -> dict[str, Any]:
    """The body of a MAIL_SEND Action, with the fixed Message-ID it is keyed by."""
    return {
        "message_id": message_id,
        "to": list(to),
        "cc": [],
        "bcc": [],
        "subject": subject,
        "body": "Please find the draft attached.",
    }


def create_mail(
    service: ActionService,
    actor: str,
    *,
    message_id: str = "<draft-1@ravel.example>",
    estimate: str | None = "0.60",
    cost_known: bool = True,
    subject: str = "Draft for review",
    to: tuple[str, ...] = ("pi@example.org",),
    target_ref: str = "mailbox:primary",
) -> Any:
    """Propose a mail Action through the service, the way a Master call does."""
    return service.create(
        kind="MAIL_SEND",
        payload=mail_payload(message_id, to=to, subject=subject),
        actor=actor,
        requesting_binding="runtime-binding-1",
        target_ref=target_ref,
        estimated_cny=estimate,
        cost_known=cost_known,
    )
