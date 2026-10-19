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

**A connection is not a grant.** The handshake is one check; a member removed
an hour later must stop receiving events then, not when their access token
expires. A watcher task re-runs the same two functions every
`gateway_stream_reauthorize_seconds` — capped at five seconds by
`gateway.auth.policy` — and closes the socket with the same code the handshake
would have used. Reusing `authenticate` and `standing_in` is the point: the
periodic check reads PostgreSQL exactly as a new request does, so revocation
is not a second rule that could drift from the first.

**A browser origin is compared exactly.** The terminal client sends no
`Origin`, and one that is absent is allowed because the bearer token is the
credential. A request that presents one is from something a browser started,
and that browser is allowed only if its origin is in the configured set. The
check is on the handshake, before authentication, because a cross-origin page
should not get to spend a stolen token even far enough to learn that it is
expired.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect

from ravel.gateway.auth.policy import normalize_origin
from ravel.gateway.deps import Caller, GatewayState, authenticate, standing_in
from ravel.gateway.stream import EventFeed

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["events"])

#: Application-defined close codes. 4000-4999 is the range the WebSocket
#: protocol leaves to the application, and these mirror the HTTP statuses the
#: same refusal would have had so that a client can treat the two identically.
CLOSE_UNAUTHENTICATED = 4401
CLOSE_NO_SUCH_PROJECT = 4404
CLOSE_BAD_REQUEST = 4400
CLOSE_FORBIDDEN = 4403
#: Not an application code: 1011 is the protocol's own "internal error", and a
#: watcher that could not reach the database must not report that as a revoked
#: membership. Telling a client its authority was withdrawn when the truth is
#: "this server could not check" would make an outage look like a decision.
CLOSE_INTERNAL = 1011

#: How much of a backlog one connection will replay before it stops trying to
#: catch up and starts following. Not a limit on the replay — the feed pages
#: through the whole stream — but on what `after_seq` may claim.
MAX_AFTER_SEQ = 2**53

_BY_STATUS = {
    401: CLOSE_UNAUTHENTICATED,
    403: CLOSE_FORBIDDEN,
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


def _origin_allowed(websocket: WebSocket, state: GatewayState) -> bool:
    """Whether this handshake came from an origin the deployment named.

    No header means the client is not a browser — the TUI — and the bearer
    token is the whole of its claim. A header that is present has to normalize
    to a member of the exact set. Both outcomes are deliberate: refusing an
    absent Origin would break the only client that exists, and accepting a
    malformed one would compare a value the caller controls against a set and
    call the result a decision.
    """
    presented = websocket.headers.get("origin")
    if presented is None:
        return True
    normalized = normalize_origin(presented)
    return normalized is not None and normalized in state.allowed_origins


def _standing(state: GatewayState, token: str | None, project_id: str) -> Caller:
    """The two checks every request makes, in one place for the watcher."""
    caller = authenticate(state, token)
    standing_in(state, project_id, caller)
    return caller


async def _watch_authority(
    websocket: WebSocket, state: GatewayState, project_id: str, token: str | None
) -> None:
    """Keep asking whether this connection is still allowed to be open.

    Runs until the socket ends or a check refuses. Every database call is made
    in a worker thread, for the same reason the stream's own reads are: this
    process serves other requests while a socket is open, and a synchronous
    query on the event loop would stall them.

    An unexpected failure is not treated as a refusal. It is logged without the
    token and answered with the protocol's internal-error code, which tells the
    client to try again rather than to sign in again or pick another project.
    """
    interval = max(0.001, state.stream_reauthorize_seconds)
    while True:
        await asyncio.sleep(interval)
        try:
            await asyncio.to_thread(_standing, state, token, project_id)
        except HTTPException as refused:
            await websocket.close(code=_BY_STATUS.get(refused.status_code, CLOSE_BAD_REQUEST))
            return
        except Exception:
            logger.exception("event stream reauthorization failed for project %s", project_id)
            await websocket.close(code=CLOSE_INTERNAL)
            return


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
    if not _origin_allowed(websocket, state):
        await websocket.accept()
        await websocket.close(code=CLOSE_FORBIDDEN)
        return

    token = _bearer(websocket.headers.get("authorization"))
    try:
        await asyncio.to_thread(_standing, state, token, project_id)
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
    watcher = asyncio.create_task(_watch_authority(websocket, state, project_id, token))
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
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher
        await frames.aclose()


__all__ = [
    "CLOSE_BAD_REQUEST",
    "CLOSE_FORBIDDEN",
    "CLOSE_INTERNAL",
    "CLOSE_NO_SUCH_PROJECT",
    "CLOSE_UNAUTHENTICATED",
    "MAX_AFTER_SEQ",
    "router",
]
