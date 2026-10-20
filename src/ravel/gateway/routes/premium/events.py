"""The versioned event stream.

The same socket the legacy route opens, with three differences that
`CONTRACTS.md` §8 requires and the legacy route was not written to provide.

**The path.** `/v1/projects/{id}/events` is the versioned name; the legacy
`/events/stream` remains, unchanged, for the TUI that already uses it.

**A declared retention window.** The window's size is a deployment decision —
`create_app(event_retention=...)` — and defaults to unbounded, which is V0's
actual storage behaviour and the legacy socket's behaviour. When a window is
declared, a cursor older than it is answered with a `resync_required` frame
carrying a fresh snapshot, not with an anchor and silence. The snapshot and its
`anchor_seq` come from the same function the snapshot HTTP route uses, so the
two cannot disagree about what the client is looking at.

**A machine-readable refusal code in the frame.** The frame's `code` is
`RESYNC_REQUIRED`, the same vocabulary the HTTP envelope uses, so a generated
client handles one set of codes rather than two.

Authorization is not different, and is deliberately not reimplemented as a
policy: the handshake and the periodic recheck call `authenticate` and
`standing_in`, the functions every HTTP route calls, so a revoked membership
closes this socket within the same cap the legacy one honours — five seconds,
enforced by the settings validator. The close codes are the legacy constants
for the same reason: a client that already understands 4401 and 4404 should not
learn a second set for the versioned path.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ravel.gateway.auth.policy import normalize_origin
from ravel.gateway.deps import (
    GatewayState,
    Principal,
    authenticate,
    standing_in,
)
from ravel.gateway.routes.events import (
    CLOSE_BAD_REQUEST,
    CLOSE_FORBIDDEN,
    CLOSE_INTERNAL,
    CLOSE_NO_SUCH_PROJECT,
    CLOSE_UNAUTHENTICATED,
    MAX_AFTER_SEQ,
)
from ravel.gateway.routes.premium.support import snapshot_for_project
from ravel.gateway.stream import EventFeed, Snapshot

logger = logging.getLogger(__name__)

router = APIRouter(tags=["v1-events"])

_BY_STATUS = {
    401: CLOSE_UNAUTHENTICATED,
    403: CLOSE_FORBIDDEN,
    404: CLOSE_NO_SUCH_PROJECT,
}


def _bearer(header: str | None) -> str | None:
    """The token out of an `Authorization` header, or `None`."""
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


def _origin_allowed(websocket: WebSocket, state: GatewayState) -> bool:
    """Whether this handshake came from an origin the deployment named.

    No `Origin` means the client is not a browser and the bearer token is the
    whole of its claim; a present one must normalize into the exact set. This
    is the legacy rule, restated here rather than reaching into the legacy
    module's private helper.
    """
    presented = websocket.headers.get("origin")
    if presented is None:
        return True
    normalized = normalize_origin(presented)
    return normalized is not None and normalized in state.allowed_origins


def _standing(state: GatewayState, token: str | None, project_id: str) -> Principal:
    """The two checks every request makes, in one place for the watcher."""
    caller = authenticate(state, token)
    return standing_in(state, project_id, caller)


async def _watch_authority(
    websocket: WebSocket, state: GatewayState, project_id: str, token: str | None
) -> None:
    """Close the socket when the membership behind it stops being valid.

    The interval is the same one the legacy socket uses, and the settings
    validator caps it at five seconds. An unexpected failure is an outage, not
    a revocation, and is answered with the protocol's internal-error code so a
    client retries rather than concluding its access was withdrawn.
    """
    interval = max(0.001, state.stream_reauthorize_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_standing, state, token, project_id)
        except HTTPException as refused:
            await websocket.close(
                code=_BY_STATUS.get(refused.status_code, CLOSE_BAD_REQUEST)
            )
            return
        except Exception:
            logger.exception(
                "v1 event stream reauthorization failed for project %s", project_id
            )
            await websocket.close(code=CLOSE_INTERNAL)
            return


@router.websocket("/projects/{project_id}/events")
async def stream(
    websocket: WebSocket,
    project_id: str,
    after_seq: Annotated[
        int,
        Query(
            ge=0,
            le=MAX_AFTER_SEQ,
            description="The last sequence the client rendered; the stream resumes after it.",
        ),
    ] = 0,
) -> None:
    """Follow the project from `after_seq`, replaying, following, and resyncing.

    The stream does not accept a token in the query string: a token in a URL
    is a token in every log on the way. The client sets `Authorization`, as the
    TUI does.
    """
    state: GatewayState = websocket.app.state.ravel
    retention: int | None = getattr(websocket.app.state, "event_retention", None)

    if not _origin_allowed(websocket, state):
        await websocket.accept()
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    token = _bearer(websocket.headers.get("authorization"))
    try:
        standing = await asyncio.to_thread(_standing, state, token, project_id)
    except HTTPException as refused:
        await websocket.accept()
        await websocket.close(
            code=_BY_STATUS.get(refused.status_code, CLOSE_BAD_REQUEST)
        )
        return

    await websocket.accept()
    feed = EventFeed(
        database=state.database,
        project_id=project_id,
        after_seq=after_seq,
        cadence=state.cadence,
        retention=retention,
        snapshot=(
            None
            if retention is None
            else _snapshot_provider(state, standing)
        ),
    )
    frames = feed.frames()
    watcher = asyncio.create_task(
        _watch_authority(websocket, state, project_id, token)
    )
    try:
        async for frame in frames:
            await websocket.send_json(frame)
    except WebSocketDisconnect:
        # The ordinary way a stream ends: somebody closed their terminal.
        pass
    except RuntimeError:
        # `send` on a socket the peer already closed. Starlette reports the
        # disconnect on the next send, so for a quiet stream this is the same
        # event as above.
        pass
    finally:
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await frames.aclose()


def _snapshot_provider(state: GatewayState, standing: Principal) -> Snapshot:
    """A zero-argument callable producing the same snapshot the HTTP route does.

    It is built here rather than passed as `snapshot_for_project` directly so
    the authenticated standing is fixed by the route, not by a frame.
    """

    def provide() -> dict[str, Any]:
        return snapshot_for_project(state, standing)

    return provide


__all__ = ["router"]
