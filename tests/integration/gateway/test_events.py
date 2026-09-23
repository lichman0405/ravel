"""The socket, and the two things only a socket can get wrong.

What the stream *says* is `test_stream.py`'s business. What is left for this
module is the part a feed cannot answer: who is allowed to open one, what a
refused caller is told, and that the token arrives somewhere it will not be
written down.

The refusals are close codes rather than HTTP statuses, because a WebSocket has
no status to give. They are the one place in the Gateway where a refusal is not
a 401 or a 403 or a 404, so the tests that matter most here are the ones
asserting the codes match what the same refusal would have been over HTTP — a
client that treats the two surfaces the same depends on it.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.integration.gateway.conftest import account, bearer, sign_in

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.project import Project
from ravel.gateway.app import create_app
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.routes.events import (
    CLOSE_NO_SUCH_PROJECT,
    CLOSE_UNAUTHENTICATED,
)
from ravel.gateway.stream import Cadence
from ravel.state.database import Database
from ravel.state.outbox import emit

pytestmark = pytest.mark.integration

#: What the socket looks for and how often it speaks, in the test's units. The
#: heartbeat is short enough that a test asserting "nothing came" finishes
#: rather than blocks, and the poll is short enough that an event committed a
#: line earlier is delivered by the next read.
FAST = Cadence(poll_seconds=0.01, heartbeat_seconds=0.05)

#: Where the stream stands once a test's own setup is behind it, which is what
#: the counts below are relative to; `test_stream.py` argues why starting from
#: the creation rather than from zero is the honest thing to do. Opening a
#: project writes two events — `PROJECT_CREATED` and the `MEMBER_ADDED` that
#: says who owns it — and every test here then adds one account through
#: `account`, which is a third. A test that followed the setup with a second
#: membership would need this to move, and if it ever does, the assertion that
#: fails names a sequence rather than passing quietly.
BORN = 3


@pytest.fixture
def streamer(
    database: Database, gateway_settings: Settings, tokens: TokenService
) -> Iterator[TestClient]:
    """A Gateway whose event stream runs at test speed.

    Built by the real `create_app` with one argument changed. Everything that
    decides what a caller may do — the routers, the dependencies, the refusal
    translation — is what a deployment runs; the cadence is the only thing this
    replaces, and it replaces it because a test cannot wait fifteen seconds for
    a heartbeat.
    """
    app = create_app(
        settings=gateway_settings,
        database=database,
        tokens=tokens,
        cadence=FAST,
    )
    with TestClient(app) as client:
        yield client


def _note(database: Database, project: Project, body: str) -> None:
    """Write one event, the way a worker in another process would."""
    with database.transaction() as session:
        emit(
            session,
            project_id=project.project_id,
            event_type=ProjectEventType.MASTER_CHECKPOINTED,
            actor_type=ActorType.AGENT,
            actor_id="master",
            payload={"note": body},
        )


def _refusal(socket: Any) -> int:
    """The close code a socket was refused with.

    A `WebSocketDisconnect` carries it. Anything else — a completed handshake,
    a frame where a close was expected — is a failure of the test's premise,
    so it is raised rather than returned as a sentinel: a helper that answered
    `-1` for "nothing was refused" would let a route that stopped refusing pass
    this suite.
    """
    with pytest.raises(WebSocketDisconnect) as refused:
        socket.receive_json()
    return int(refused.value.code)


# ── Following ───────────────────────────────────────────────────────────────


def test_an_owner_follows_the_stream_and_sees_it_move(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """The whole route, end to end: anchor, replay, and then live.

    The three phases are read in order and each is asserted, because they are
    three different mechanisms that happen to share a socket. The three events
    that were in the record before the connection opened arrive as replay —
    the project, its owner, and the account this test adds — and the note,
    written after the anchor was read, arrives because the feed noticed the
    record change.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(streamer, "ada")["access_token"])

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as socket:
        anchor = socket.receive_json()
        assert anchor["type"] == "anchor"
        assert anchor["project_id"] == project.project_id
        assert anchor["head_seq"] == BORN

        replayed = [socket.receive_json()["event_type"] for _ in range(BORN)]
        assert replayed == [
            ProjectEventType.PROJECT_CREATED.value,
            # The fixture's owner, then the account this test adds: authority
            # conferred is a fact the stream carries like any other.
            ProjectEventType.MEMBER_ADDED.value,
            ProjectEventType.MEMBER_ADDED.value,
        ]

        _note(database, project, "somebody moved a node")

        frame = socket.receive_json()

    assert frame["type"] == "event"
    assert frame["seq"] == BORN + 1
    assert frame["event_type"] == ProjectEventType.MASTER_CHECKPOINTED.value
    assert frame["payload"] == {"note": "somebody moved a node"}


def test_a_reconnecting_client_is_replayed_from_where_it_stopped(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """`after_seq` on the query string, which is `docs/08` §7's reconnect.

    A client that drops, reconnects and asks for everything after the head it
    was holding is sent what came next — the events it missed, and not the ones
    it already drew.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(streamer, "ada")["access_token"])
    for body in ("one", "two", "three"):
        _note(database, project, body)

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream?after_seq={BORN + 1}",
        headers=headers,
    ) as socket:
        assert socket.receive_json()["head_seq"] == BORN + 3
        third = socket.receive_json()
        fourth = socket.receive_json()

    assert [third["seq"], fourth["seq"]] == [BORN + 2, BORN + 3]
    assert [third["payload"], fourth["payload"]] == [{"note": "two"}, {"note": "three"}]


def test_the_stream_is_the_read_surface_every_member_shares(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """A lab user may watch. What they may not do is direct, and following the
    record is not directing it — the same split `control` draws between the
    routes that move a project and the ones that read it."""
    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(streamer, "lab")["access_token"])

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as socket:
        assert socket.receive_json()["type"] == "anchor"


# ── Refusals ────────────────────────────────────────────────────────────────


def test_no_token_is_refused_with_the_code_a_401_would_have_been(
    streamer: TestClient, project: Project
) -> None:
    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream"
    ) as socket:
        assert _refusal(socket) == CLOSE_UNAUTHENTICATED == 4401


def test_a_non_member_is_told_the_project_does_not_exist(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """404 and not 403, the same answer every other route gives and for the same
    reason: confirming the project exists is a fact about somebody else's
    research."""
    account(database, username="ada")
    headers = bearer(sign_in(streamer, "ada")["access_token"])

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as socket:
        assert _refusal(socket) == CLOSE_NO_SUCH_PROJECT == 4404


def test_a_token_forged_with_another_secret_is_refused(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """The socket checks the same signature the HTTP routes do, because it calls
    the same function. A second authorization path would be a second place for
    this to be wrong."""
    user_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    elsewhere = TokenService(secret="a-different-secret-of-sufficient-length", ttl_seconds=3600)
    forged, _grant = elsewhere.issue_access(user_id)

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=bearer(forged)
    ) as socket:
        assert _refusal(socket) == CLOSE_UNAUTHENTICATED


def test_a_token_in_the_query_string_is_not_a_credential(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """It has to be in the header, and this is the test that says so.

    A token in a URL is a token in every access log, proxy log and crash dump on
    the way. Accepting one here would mean that a client which *can* set a
    header has no reason to, and the leak would arrive as a convenience.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    token = sign_in(streamer, "ada")["access_token"]

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream?access_token={token}"
    ) as socket:
        assert _refusal(socket) == CLOSE_UNAUTHENTICATED


def test_a_malformed_authorization_header_is_refused(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """A header that is not a bearer token is not a bearer token. Read by hand
    rather than by `HTTPBearer`, so the parsing is this module's and is checked
    here rather than assumed."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream",
        headers={"Authorization": "Basic YWRhOnBhc3N3b3Jk"},
    ) as socket:
        assert _refusal(socket) == CLOSE_UNAUTHENTICATED


# ── The record is PostgreSQL, and the socket is a view of it ────────────────


def test_a_frame_carries_no_harness_session_identifier(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """`docs/09`: DSH is not exposed. What a member follows is domain events.

    Asserted against the frame a real change produces rather than against a
    hand-written payload, so that this fails if the frame grows a field — the
    top level is `as_wire`'s fixed set and nothing else can reach it.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(streamer, "ada")["access_token"])
    _note(database, project, "one")

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as socket:
        socket.receive_json()
        frame = socket.receive_json()

    assert not any("session" in key.lower() for key in frame)
    assert "session" not in str(frame["payload"]).lower()


def test_closing_the_socket_stops_the_feed(
    streamer: TestClient, database: Database, project: Project
) -> None:
    """A client that goes away takes its feed with it.

    The event written afterwards is never read, and the assertion is that the
    *next* connection starts from the record rather than from where the dead
    one had got to — the stream holds no state a disconnected client left
    behind.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(streamer, "ada")["access_token"])

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as socket:
        socket.receive_json()
    _note(database, project, "after they left")

    with streamer.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as socket:
        assert socket.receive_json()["head_seq"] == BORN + 1
        # From the beginning of the stream, including what the dead connection
        # had already been sent: a new connection is a new reader, and nothing
        # the old one had got to survives it.
        replayed = [socket.receive_json() for _ in range(BORN + 1)]

    assert [frame["event_type"] for frame in replayed] == [
        ProjectEventType.PROJECT_CREATED.value,
        ProjectEventType.MEMBER_ADDED.value,
        ProjectEventType.MEMBER_ADDED.value,
        ProjectEventType.MASTER_CHECKPOINTED.value,
    ]
    assert replayed[-1]["payload"] == {"note": "after they left"}
