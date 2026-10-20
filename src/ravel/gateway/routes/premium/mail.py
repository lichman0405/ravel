"""Mail on the versioned surface: what was proposed, and nothing that sends.

There is no route here that sends. A mail send is an Action — it leaves the
process, cannot be taken back, and may not be attempted twice — so the only
write is a draft that *creates* one, and delivery belongs to a worker that
claims it after every check the Action layer makes. `CONTRACTS.md` §8 says
approve/send go through Actions; this module is what that sentence looks like
as routes.

The Message-ID is derived from a client-chosen identifier rather than accepted
from the caller. That is the same duplicate rule as the conversation: a client
that retries a draft it is not sure landed sends the same `client_message_id`
and gets the same Action back, because the Action's mail idempotency key is its
Message-ID. A caller-supplied Message-ID would also work, and would let two
retries of one draft name two different sends — which is the failure the rule
exists to prevent.

**An uncertain send is shown as uncertain.** `Action.public_view` projects the
internal `UNKNOWN` state of a mail Action as `UNKNOWN_DELIVERY`, and this
module marks it for manual reconciliation instead of letting a client read it
as success or as a clean failure. Reconciling one is a separate, audited act;
V0 exposes the record and its state, not a button that guesses.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Any

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, Field

from ravel.actions.repository import ActionRepository
from ravel.actions.service import ActionService
from ravel.domain.enums import ActionKind
from ravel.gateway.deps import DirectorDep, GatewayStateDep, PrincipalDep
from ravel.gateway.routes.premium.support import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    paginate,
)

router = APIRouter(tags=["v1-mail"])

#: Who the Gateway says requested a draft Action. A binding is a durable
#: explanation of where a side effect came from; "the v1 mail route" is a truer
#: one than the user's identifier, which is already the actor.
MAIL_BINDING = "gateway:v1:mail"

#: The domain a derived Message-ID uses. RAVEL does not own this domain and
#: does not need to: the value is an identifier for correlating retries, and
#: SMTP rewrites or preserves it as a header — what matters is that the same
#: draft always derives the same one.
MESSAGE_ID_DOMAIN = "ravel.invalid"

UNKNOWN_DELIVERY = "UNKNOWN_DELIVERY"


class MailDraft(BaseModel):
    """What a person wants to send, and the identifier their client retries with."""

    to: list[str] = Field(min_length=1, max_length=100)
    cc: list[str] = Field(default_factory=list, max_length=100)
    bcc: list[str] = Field(default_factory=list, max_length=100)
    subject: str = Field(min_length=1, max_length=1000)
    body: str = Field(min_length=1, max_length=200_000)
    client_message_id: str = Field(
        min_length=1,
        max_length=200,
        description="Chosen once by the client; a retry with the same value is the same draft.",
    )


class MailView(BaseModel):
    """One mail Action, its content, and whether a human must reconcile it."""

    action: dict[str, Any]
    message_id: str
    to: list[str]
    cc: list[str]
    bcc: list[str]
    subject: str
    body: str
    requires_manual_review: bool


@router.get(
    "/projects/{project_id}/mail",
    response_model=Page[MailView],
    summary="Mail Actions, newest first",
)
def list_mail(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
) -> Page[MailView]:
    """Every mail Action in this project, whatever its state.

    Reads all of them rather than only drafts: an owner opening this screen
    after a send is asking what happened to it, and a list that hid accepted or
    uncertain sends would answer the wrong question. The state is in the
    projection so the screen can group them.
    """
    with state.database.read_only() as session:
        found = [
            action
            for action in ActionRepository(session, standing.project_id).all()
            if action.kind is ActionKind.MAIL_SEND
        ]
    page = paginate(
        found,
        key=lambda action: (action.created_at.isoformat(), action.action_id),
        limit=limit,
        cursor=cursor,
        descending=True,
    )
    return Page[MailView](
        items=[_view(action.public_view(), action.payload) for action in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "/projects/{project_id}/mail",
    status_code=status.HTTP_201_CREATED,
    response_model=MailView,
    summary="Draft a mail send as an Action",
)
def draft(
    standing: DirectorDep, state: GatewayStateDep, request: MailDraft
) -> MailView:
    """Create the Action for one message, once per `client_message_id`.

    The Action may come back `AWAITING_APPROVAL` or `READY`, depending on the
    envelope in force; both mean "recorded, not sent". A second call with the
    same client identifier returns the original Action in whatever state it has
    reached, because its Message-ID — and therefore its idempotency key — is
    the same.
    """
    message_id = _message_id(
        standing.project_id, standing.user_id, request.client_message_id
    )
    payload: dict[str, Any] = {
        "message_id": message_id,
        "to": request.to,
        "cc": request.cc,
        "bcc": request.bcc,
        "subject": request.subject,
        "body": request.body,
    }
    with state.database.transaction() as session:
        action = ActionService(
            session, standing.project_id, settings=state.settings
        ).create(
            kind=ActionKind.MAIL_SEND,
            payload=payload,
            actor=standing.user_id,
            requesting_binding=MAIL_BINDING,
            target_ref=f"mail:{message_id}",
        )
        return _view(action.public_view(), action.payload)


def _message_id(project_id: str, user_id: str, client_message_id: str) -> str:
    """The Message-ID one client message always derives.

    The same construction as the conversation's turn identifier and for the
    same reason: the identifier a client chooses must never become a visible
    one, and the project and user belong in the input so two people choosing
    "1" are drafting two messages.
    """
    material = "\x1f".join((project_id, user_id, client_message_id))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"<draft-{digest[:32]}@{MESSAGE_ID_DOMAIN}>"


def _strings(payload: dict[str, Any], key: str) -> list[str]:
    """A recipient list from a stored payload, tolerating an older shape."""
    value = payload.get(key, [])
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [str(item) for item in value]


def _text(payload: dict[str, Any], key: str) -> str:
    """A text field from a stored payload, or the empty string."""
    value = payload.get(key, "")
    return value if isinstance(value, str) else ""


def _view(action: dict[str, Any], payload: dict[str, Any]) -> MailView:
    """Project the Action's mail content beside its public lifecycle."""
    return MailView(
        action=action,
        message_id=_text(payload, "message_id"),
        to=_strings(payload, "to"),
        cc=_strings(payload, "cc"),
        bcc=_strings(payload, "bcc"),
        subject=_text(payload, "subject"),
        body=_text(payload, "body"),
        requires_manual_review=action["status"] == UNKNOWN_DELIVERY,
    )


__all__ = ["MAIL_BINDING", "MailDraft", "MailView", "router"]
