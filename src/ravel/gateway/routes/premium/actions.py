"""Actions on the versioned surface: read them, and answer one.

An Action is RAVEL's record of an externally visible side effect. Approving or
rejecting one is a human decision, and the decision is checked where the record
is written — by `ActionService`, against the approval's required role, the
caller's live membership, the stored approval snapshot, and the envelope's
budget. This module supplies only two things the service does not:

- the **content hash** the human is approving, required in the body and
  compared against the Action's stored hash before the service is called. A
  browser that showed one payload and approved another is the failure this
  catches, and it is why the hash is not optional.
- the **v1 projection**, through `Action.public_view()`.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from ravel.actions.repository import ActionRepository
from ravel.actions.service import ActionService
from ravel.domain.enums import ActionKind, ActionStatus
from ravel.gateway.deps import GatewayStateDep, PrincipalDep
from ravel.gateway.routes.premium.errors import problem
from ravel.gateway.routes.premium.support import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    paginate,
)

router = APIRouter(tags=["v1-actions"])


class ActionAnswer(BaseModel):
    """A human's answer, bound to the exact content they saw."""

    payload_hash: str = Field(
        pattern=r"^sha256:[0-9a-f]{64}$",
        description="The hash of the Action content this answer approves or rejects.",
    )
    note: str = Field(default="", max_length=2000)


@router.get(
    "/projects/{project_id}/actions",
    response_model=Page[dict[str, Any]],
    summary="Actions, newest first",
)
def list_actions(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
    action_status: Annotated[
        str | None,
        Query(alias="status", description="Only Actions in this state."),
    ] = None,
    kind: Annotated[
        str | None, Query(description="Only Actions of this kind.")
    ] = None,
) -> Page[dict[str, Any]]:
    """The project's Actions, newest first, projected through the public view.

    The public view is the schema's projection and not `model_dump`: the schema
    forbids additional properties, so an internal field leaking here would be a
    broken client rather than a harmless extra.
    """
    chosen_status = _enum(ActionStatus, action_status, "status")
    chosen_kind = _enum(ActionKind, kind, "kind")
    with state.database.read_only() as session:
        found = ActionRepository(session, standing.project_id).all()
    if chosen_status is not None:
        found = [action for action in found if action.status is chosen_status]
    if chosen_kind is not None:
        found = [action for action in found if action.kind is chosen_kind]
    page = paginate(
        found,
        key=lambda action: (action.created_at.isoformat(), action.action_id),
        limit=limit,
        cursor=cursor,
        descending=True,
    )
    return Page[dict[str, Any]](
        items=[action.public_view() for action in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.post(
    "/projects/{project_id}/actions/{action_id}/approve",
    response_model=dict[str, Any],
    summary="Approve the exact Action content named by payload_hash",
)
def approve(
    standing: PrincipalDep,
    state: GatewayStateDep,
    action_id: str,
    request: ActionAnswer,
) -> dict[str, Any]:
    """Record this caller's yes and release the Action to READY.

    Raises:
        HTTPException: 409 `STALE_VERSION` if the supplied hash does not
            describe the Action. The service's own refusals — a stale approval,
            a required role the caller does not hold, budget — travel as the
            codes `CONTRACTS.md` §8 names.
    """
    with state.database.transaction() as session:
        service = ActionService(session, standing.project_id, settings=state.settings)
        action = service.get(action_id)
        _verify_hash(action.payload_hash, request.payload_hash)
        approved = service.approve(
            action_id, resolved_by=standing.user_id, note=request.note
        )
    return approved.public_view()


@router.post(
    "/projects/{project_id}/actions/{action_id}/reject",
    response_model=dict[str, Any],
    summary="Reject the exact Action content named by payload_hash",
)
def reject(
    standing: PrincipalDep,
    state: GatewayStateDep,
    action_id: str,
    request: ActionAnswer,
) -> dict[str, Any]:
    """Record this caller's no and release anything the Action reserved."""
    with state.database.transaction() as session:
        service = ActionService(session, standing.project_id, settings=state.settings)
        action = service.get(action_id)
        _verify_hash(action.payload_hash, request.payload_hash)
        rejected = service.reject(
            action_id, resolved_by=standing.user_id, note=request.note
        )
    return rejected.public_view()


def _verify_hash(stored: str, supplied: str) -> None:
    """Refuse an answer that does not name the stored content.

    Both values are hashes, so comparing them and naming them in the refusal
    discloses nothing the caller did not already hold.
    """
    if stored != supplied:
        raise problem(
            status.HTTP_409_CONFLICT,
            "STALE_VERSION",
            "the payload_hash does not describe this Action's current content",
            details={"expected": stored, "supplied": supplied},
        )


def _enum[EnumType: StrEnum](
    enum_type: type[EnumType], value: str | None, field: str
) -> EnumType | None:
    """Parse an enum query parameter, or refuse it as the caller's mistake."""
    if value is None:
        return None
    try:
        return enum_type(value)
    except ValueError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{field} is not one of {[item.value for item in enum_type]}",
        ) from error


__all__ = ["ActionAnswer", "router"]
