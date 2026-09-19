"""Watching a project happen.

`docs/08` §6 gives the Gateway one socket, and this is it: a member of a
project can follow its event stream. The route is deliberately thin, because
everything that decides *what* a client sees — the anchor, the replay, the
cadence — is in `ravel.gateway.stream`, where it can be tested without a socket
in the way. What is left here is the three things only a route can do: work out
who is asking, accept, and stop when they leave.

**Authentication is the same function the HTTP routes call.** A WebSocket
cannot answer with a 401, so `deps.authenticate` and `deps.standing_in` are
written as plain functions and this module translates their refusal into a
close code. The alternative — reading the token here — would be a second
authorization path, and the security model does not survive having two.

**The token arrives in a header and not in the query string.** A token in a
URL is a token in every access log, every proxy log and every crash dump on the
way, and the client here is a terminal program that can set a header. `docs/08`
rules out the browser dashboard that could not, so the constraint costs
nothing.

**The handshake completes before the refusal is sent.** `accept` then `close`
rather than `close` on its own: a client that is refused at the handshake sees
a transport error, and a TUI cannot tell an expired token from a project that
does not exist from a server that is down — three situations whose only
sensible responses are log in again, pick another project, and try later. It
leaks nothing to accept first: no frame is sent before the decision, and the
close code is the whole of what the caller learns.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ravel.gateway.deps import GatewayState, authenticate, standing_in
from ravel.gateway.stream import EventFeed

router = APIRouter(prefix="/projects", tags=["events"])

#: Application-defined close codes. 4000-4999 is the range the WebSocket
#: protocol leaves to the application, and these mirror the HTTP statuses the
#: same refusal would have had so that a client can treat the two identically.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_NO_SUCH_PROJECT = 4404
CLOSE_BAD_REQUEST = 4400

#: How much of a backlog one connection will replay before it stops trying to
#: catch up and starts following. Not a limit on the replay — the feed pages
#: through the whole stream — but on what `after_seq` may claim.
MAX_AFTER_SEQ = 2**53

_BY_STATUS = {
    401: CLOSE_UNAUTHENTICATED,
    404: CLOSE_NO_SUCH_PROJECT,
}


def _bearer(header: str | None) -> str | None:
    """The token out of an `Authorization` header, or `None`.

    Spelled out rather than taken from `HTTPBearer`, which is an HTTP
    dependency and raises into a request lifecycle a socket does not have.
    """
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None


@router.websocket("/{project_id}/events/stream")
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
    """Follow a project's events, from `after_seq` to now and then onwards.

    Raises nothing to the caller: a refusal is a close code, and a client that
    goes away is a client that went away.
    """
    state: GatewayState = websocket.app.state.ravel
    try:
        caller = authenticate(state, _bearer(websocket.headers.get("authorization")))
        standing_in(state, project_id, caller)
    except HTTPException as refused:
        await websocket.accept()
        await websocket.close(code=_BY_STATUS.get(refused.status_code, CLOSE_BAD_REQUEST))
        return

    await websocket.accept()
    feed = EventFeed(
        database=state.database,
        project_id=project_id,
        after_seq=after_seq,
        cadence=state.cadence,
    )
    frames = feed.frames()
    try:
        async for frame in frames:
            await websocket.send_json(frame)
    except WebSocketDisconnect:
        # The ordinary way a stream ends: somebody closed their terminal.
        pass
    except RuntimeError:
        # `send` on a socket the peer has already closed. Starlette does not
        # report the disconnect until it tries to send, so for a client that
        # leaves during a quiet period this is the same event as above and not
        # an error worth propagating.
        pass
    finally:
        await frames.aclose()


__all__ = [
    "CLOSE_BAD_REQUEST",
    "CLOSE_NO_SUCH_PROJECT",
    "CLOSE_UNAUTHENTICATED",
    "MAX_AFTER_SEQ",
    "router",
]
