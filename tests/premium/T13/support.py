"""Small helpers shared by the T13 cases.

Nothing here writes around a service. Events are appended through the same
`ravel.state.outbox.emit` a worker uses, so a case that asserts the stream saw a
row is asserting about the outbox the product writes, not a fixture's private
copy. Memberships are granted and withdrawn through `MembershipRepository`, so
a revocation carries the `MEMBER_REVOKED` event production writes with it.

The socket helpers exist because a stream is not a request: a test has to skip
heartbeats, collect a burst, or wait for a close, and three copies of "read
until the thing I expect" would be three places for a timeout to differ.
"""

from __future__ import annotations

import signal
from typing import Any

from starlette.testclient import WebSocketTestSession
from starlette.websockets import WebSocketDisconnect

from ravel.domain.events import ActorType, ProjectEvent, ProjectEventType
from ravel.state.database import Database
from ravel.state.outbox import emit, events_since, last_event_seq

#: `CONTRACTS.md` §8's versioned surface. Hardcoded on purpose: deriving this
#: from the application would make the contract test agree with whatever the
#: application happened to publish, including a route that was dropped by
#: accident.
EXPECTED_V1: dict[str, set[str]] = {
    "/v1/auth/login": {"post"},
    "/v1/auth/logout": {"post"},
    "/v1/auth/refresh": {"post"},
    "/v1/projects": {"get", "post"},
    "/v1/projects/{project_id}/actions": {"get"},
    "/v1/projects/{project_id}/actions/{action_id}/approve": {"post"},
    "/v1/projects/{project_id}/actions/{action_id}/reject": {"post"},
    "/v1/projects/{project_id}/authority-envelope": {"get", "post"},
    "/v1/projects/{project_id}/computations": {"get"},
    "/v1/projects/{project_id}/computations/{execution_id}": {"get"},
    "/v1/projects/{project_id}/conversation": {"get", "post"},
    "/v1/projects/{project_id}/dag": {"get"},
    "/v1/projects/{project_id}/evidence": {"get"},
    "/v1/projects/{project_id}/evidence/{evidence_id}": {"get"},
    "/v1/projects/{project_id}/lab/tasks": {"get"},
    "/v1/projects/{project_id}/lab/tasks/{task_id}": {"get"},
    "/v1/projects/{project_id}/lab/tasks/{task_id}/deviations": {"post"},
    "/v1/projects/{project_id}/lab/tasks/{task_id}/handover/documents/{document}": {"get"},
    "/v1/projects/{project_id}/lab/tasks/{task_id}/sign-off": {"post"},
    "/v1/projects/{project_id}/lab/tasks/{task_id}/uploads": {"post"},
    "/v1/projects/{project_id}/mail": {"get", "post"},
    "/v1/projects/{project_id}/pause": {"post"},
    "/v1/projects/{project_id}/resume": {"post"},
    "/v1/projects/{project_id}/snapshot": {"get"},
    "/v1/projects/{project_id}/sources": {"get"},
    "/v1/projects/{project_id}/sources/{source_id}": {"get"},
}

#: The one versioned route OpenAPI cannot describe.
EXPECTED_V1_SOCKETS = frozenset({"/v1/projects/{project_id}/events"})


def emit_note(database: Database, project_id: str, body: str) -> ProjectEvent:
    """Append one `MASTER_CHECKPOINTED` event, the way another process would."""
    with database.transaction() as session:
        return emit(
            session,
            project_id=project_id,
            event_type=ProjectEventType.MASTER_CHECKPOINTED,
            actor_type=ActorType.AGENT,
            actor_id="master",
            payload={"note": body},
        )


def stored_events(database: Database, project_id: str) -> list[ProjectEvent]:
    """Every event this project has, oldest first, read from PostgreSQL."""
    with database.read_only() as session:
        return events_since(session, project_id, after_seq=0)


def stored_head(database: Database, project_id: str) -> int:
    """The highest sequence actually stored for this project."""
    with database.read_only() as session:
        return last_event_seq(session, project_id)


def next_non_heartbeat(socket: WebSocketTestSession) -> dict[str, Any]:
    """The next frame that is not a heartbeat."""
    while True:
        frame = socket.receive_json()
        if frame.get("type") != "heartbeat":
            return dict(frame)


def collect_until_heartbeats(
    socket: WebSocketTestSession, beats: int = 2
) -> list[dict[str, Any]]:
    """Frames up to and including `beats` consecutive heartbeats.

    A stream that has nothing to say still has something to send. Waiting for a
    heartbeat is how a test establishes a *quiet* interval without sleeping for
    an arbitrary fraction of a second: the heartbeat is the server's own
    statement that it looked and found nothing.
    """
    frames: list[dict[str, Any]] = []
    heard = 0
    while heard < beats:
        frame = dict(socket.receive_json())
        frames.append(frame)
        heard = heard + 1 if frame.get("type") == "heartbeat" else 0
    return frames


def _expire(_signum: int, _frame: Any) -> None:
    raise TimeoutError("the event stream did not close within the test's allowance")


def receive_until_close(socket: WebSocketTestSession, timeout: float = 5.0) -> int:
    """Read frames until the server closes, returning the close code.

    Heartbeats and any events that land before the close are consumed and
    discarded; the close code is the whole answer the caller asked for. The
    interval is enforced with `SIGALRM` where the platform has it — the gate
    runs on Linux — so a route that stopped closing fails the case instead of
    hanging it. Where the signal is unavailable the loop simply waits, which is
    the same behaviour as `receive_json`.
    """
    armed = hasattr(signal, "SIGALRM")
    previous: Any = None
    if armed:
        previous = signal.signal(signal.SIGALRM, _expire)
        signal.setitimer(signal.ITIMER_REAL, timeout)
    try:
        while True:
            try:
                socket.receive_json()
            except WebSocketDisconnect as closed:
                return int(closed.code)
    finally:
        if armed:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)
