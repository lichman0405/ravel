"""Shared machinery for the versioned API: pages, cursor codes, and snapshots.

The v1 surface is a *projection* of the same authoritative records the legacy
routes serve. Nothing here writes; the only writes in this package are the ones
that call the same services the legacy routes call, and each of those is named
at its route.

Two pieces are here rather than repeated per module:

- **Pagination.** Every list on the v1 surface takes the same `limit` and
  `cursor` and returns the same three fields. The cursor is a base64url encoding
  of the last row's ordering key — a *keyset* cursor, not an offset — so a row
  inserted between two pages cannot shift a later page into repeating or
  skipping a row. The ordering key is always a tuple of strings.
- **The snapshot.** `anchor_seq` and the aggregate it describes are read in one
  `REPEATABLE READ` transaction. Reading them in two statements under the
  default isolation would let an event commit between them, and a client that
  then replayed from `anchor_seq` would either miss that event or see it twice
  with a snapshot that already contains its effect.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException, status
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ravel.actions.repository import ActionRepository
from ravel.gateway.deps import GatewayState, Principal
from ravel.master.audit import ProjectAudit
from ravel.state.repositories.research import EvidenceRepository

#: What a list returns when the caller does not say.
DEFAULT_LIMIT = 50

#: The ceiling from `CONTRACTS.md` §8. A caller that asks for more is refused
#: rather than silently clamped: a clamp would make a client believe it had read
#: a whole collection when it had read a hundred rows of one.
MAX_LIMIT = 100


class Page[T](BaseModel):
    """One page of a list, with the cursor that reaches the next one."""

    items: list[T]
    next_cursor: str | None = None
    has_more: bool = False


def invalid_cursor() -> HTTPException:
    """A cursor this endpoint did not issue, refused as the caller's mistake."""
    return HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="the cursor is not one this endpoint issued",
    )


def encode_cursor(values: tuple[Any, ...]) -> str:
    """Encode an ordering key as an opaque, URL-safe cursor."""
    raw = json.dumps(list(values), separators=(",", ":"), ensure_ascii=True)
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(value: str) -> tuple[str, ...]:
    """Decode a cursor, or refuse it as malformed.

    Raises:
        HTTPException: 400. The cursor was not one this API issued. A cursor is
            not a credential and carries nothing secret; the refusal is about
            shape, so that ``cursor=last`` cannot be read as an offset.
    """
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
        parsed = json.loads(decoded)
    except (ValueError, binascii.Error, UnicodeError, json.JSONDecodeError) as error:
        raise invalid_cursor() from error
    if not isinstance(parsed, list) or not parsed:
        raise invalid_cursor()
    return tuple(str(part) for part in parsed)


def _normalise(values: tuple[Any, ...]) -> tuple[str, ...]:
    return tuple(str(value) for value in values)


def paginate[T](
    items: list[T],
    *,
    key: Callable[[T], tuple[Any, ...]],
    limit: int,
    cursor: str | None,
    descending: bool = False,
) -> Page[T]:
    """One keyset page of an already-collected list.

    The collection is read whole and sorted here because V0's lists are small
    enough that a second SQL query shape per resource would be more places to
    be wrong than rows saved. The *cursor* is still a real keyset: page two is
    selected by comparing the ordering keys, never by skipping a count, so an
    insert between the two requests cannot duplicate or drop an existing row.
    """
    ordered = sorted(items, key=lambda item: _normalise(key(item)), reverse=descending)
    if cursor is not None:
        boundary = decode_cursor(cursor)
        if descending:
            ordered = [item for item in ordered if _normalise(key(item)) < boundary]
        else:
            ordered = [item for item in ordered if _normalise(key(item)) > boundary]
    page = ordered[:limit]
    has_more = len(ordered) > limit
    return Page(
        items=page,
        next_cursor=encode_cursor(key(page[-1])) if has_more and page else None,
        has_more=has_more,
    )


#: How many events the snapshot's event count reports reading. The snapshot
#: does not include the events themselves — the stream is where they are read —
#: so only the count is kept, and the count is of the same transaction.
def _snapshot(session: Session, standing: Principal) -> dict[str, Any]:
    audit = ProjectAudit.assemble(session, standing.project_id)
    anchor = audit.events[-1].seq if audit.events else 0
    evidence = EvidenceRepository(session, standing.project_id).all()
    actions = ActionRepository(session, standing.project_id).all()
    return {
        "anchor_seq": int(anchor or 0),
        "project": audit.project.model_dump(mode="json"),
        "role": standing.role.value,
        "success_contract": (
            audit.success_contract.model_dump(mode="json")
            if audit.success_contract is not None
            else None
        ),
        "roadmap": [phase.model_dump(mode="json") for phase in audit.roadmap],
        "is_finished": audit.is_finished,
        "is_concludable": audit.is_concludable,
        "unfinished": [node.model_dump(mode="json") for node in audit.unfinished],
        "open_deviations": [
            deviation.model_dump(mode="json") for deviation in audit.open_deviations
        ],
        "counts": {
            "nodes": len(audit.nodes),
            "decisions": len(audit.decisions),
            "reviews": len(audit.reviews),
            "executions": len(audit.executions),
            "deviations": len(audit.deviations),
            "evidence": len(evidence),
            "actions": len(actions),
            "events": len(audit.events),
        },
    }


def read_snapshot(standing: Principal, state: GatewayState) -> dict[str, Any]:
    """The aggregate and its anchor, read from one consistent snapshot.

    `REPEATABLE READ` rather than the default: without it a change committed
    between the aggregate query and the anchor query would be described by one
    and not the other. The statement is the first in its transaction, which is
    where PostgreSQL requires transaction characteristics to be set.
    """
    with state.database.read_only() as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        return _snapshot(session, standing)


def snapshot_for_project(state: GatewayState, project_id: str) -> dict[str, Any]:
    """A snapshot addressed by project alone, for the stream's resync frame.

    The stream has already authenticated the caller against this project, so it
    holds a `Principal`; this helper exists for the one case where the snapshot
    is built from an identifier the socket already resolved. It reads the same
    projection as `read_snapshot`, in the same isolation, and differs only in
    not needing the role — a resync frame describes the project, not the
    caller's authority in it.
    """
    with state.database.read_only() as session:
        session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ"))
        audit = ProjectAudit.assemble(session, project_id)
        anchor = audit.events[-1].seq if audit.events else 0
        evidence = EvidenceRepository(session, project_id).all()
        actions = ActionRepository(session, project_id).all()
        return {
            "anchor_seq": int(anchor or 0),
            "project": audit.project.model_dump(mode="json"),
            "success_contract": (
                audit.success_contract.model_dump(mode="json")
                if audit.success_contract is not None
                else None
            ),
            "roadmap": [phase.model_dump(mode="json") for phase in audit.roadmap],
            "is_finished": audit.is_finished,
            "is_concludable": audit.is_concludable,
            "unfinished": [node.model_dump(mode="json") for node in audit.unfinished],
            "open_deviations": [
                deviation.model_dump(mode="json")
                for deviation in audit.open_deviations
            ],
            "counts": {
                "nodes": len(audit.nodes),
                "decisions": len(audit.decisions),
                "reviews": len(audit.reviews),
                "executions": len(audit.executions),
                "deviations": len(audit.deviations),
                "evidence": len(evidence),
                "actions": len(actions),
                "events": len(audit.events),
            },
        }


__all__ = [
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "Page",
    "decode_cursor",
    "encode_cursor",
    "invalid_cursor",
    "paginate",
    "read_snapshot",
    "snapshot_for_project",
]
