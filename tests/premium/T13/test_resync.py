"""T13-AC2: a cursor older than the retained window resynchronizes.

An empty replay is indistinguishable from being up to date, so the versioned
stream refuses to answer an out-of-window cursor with silence. It sends a
`resync_required` frame carrying `RESYNC_REQUIRED` and a fresh snapshot, then
follows from that snapshot's anchor. The boundary is asserted too: a cursor the
window still covers replays normally, because a feed that resynchronized
everything would satisfy the letter of the first test and lose the resume
property entirely.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from tests.premium.T13.conftest import RETENTION, account, bearer, sign_in
from tests.premium.T13.support import emit_note, next_non_heartbeat, stored_events

from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.gateway.stream import RESYNC_CODE, RESYNC_FRAME
from ravel.state.database import Database

pytestmark = pytest.mark.integration


def test_a_cursor_older_than_the_window_gets_a_snapshot_and_a_code_never_silence(
    client: TestClient, database: Database, project: Project
) -> None:
    """The old cursor is answered with what the client needs, not an empty page.

    The snapshot in the frame is compared field for field with the snapshot the
    HTTP route returns for the same project and caller. If the two disagreed, a
    client that resynchronized over the socket and one that refreshed over HTTP
    would render different projects from the same sequence.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    for body in ("four", "five", "six", "seven"):
        emit_note(database, project.project_id, body)

    events = stored_events(database, project.project_id)
    assert [event.seq for event in events] == [1, 2, 3, 4, 5, 6, 7]
    assert RETENTION < 7 - 0, "the fixture must put sequence zero outside the window"

    http_snapshot = client.get(
        f"/v1/projects/{project.project_id}/snapshot", headers=headers
    )
    assert http_snapshot.status_code == 200, http_snapshot.text
    snapshot_body = http_snapshot.json()
    assert snapshot_body["anchor_seq"] == 7

    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq=0", headers=headers
    ) as socket:
        frame = socket.receive_json()

        assert frame["type"] == RESYNC_FRAME
        assert frame["type"] != "anchor"
        assert frame["code"] == RESYNC_CODE == "RESYNC_REQUIRED"
        assert frame["project_id"] == project.project_id
        assert frame["requested_after_seq"] == 0
        assert frame["head_seq"] == 7
        assert frame["anchor_seq"] == snapshot_body["anchor_seq"]
        fresh = frame["snapshot"]
        assert fresh["anchor_seq"] == 7
        assert fresh["project"] == snapshot_body["project"]
        assert fresh["counts"] == snapshot_body["counts"]
        assert fresh["counts"]["events"] == 7
        assert fresh["counts"]["nodes"] == 0

        # Following continues from the snapshot's anchor: the next event to
        # commit arrives, and nothing the snapshot already describes is replayed.
        emitted = emit_note(database, project.project_id, "eight")
        following = next_non_heartbeat(socket)

    assert emitted.seq == 8
    assert following["type"] == "event"
    assert following["seq"] == 8
    assert following["project_id"] == project.project_id


def test_a_cursor_inside_the_window_still_replays_normally(
    client: TestClient, database: Database, project: Project
) -> None:
    """The newest `RETENTION` sequences are replayed, and the boundary is inclusive.

    The client asks for everything after `head - RETENTION`. That is exactly the
    oldest position the window promises, so it gets an anchor and the four
    events it has not seen — not a resynchronization. This is what keeps the
    resync path from swallowing the ordinary reconnect.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    for body in ("four", "five", "six", "seven"):
        emit_note(database, project.project_id, body)

    head = 7
    after_seq = head - RETENTION
    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq={after_seq}",
        headers=headers,
    ) as socket:
        anchor = socket.receive_json()
        assert anchor["type"] == "anchor"
        assert anchor["head_seq"] == head

        replayed = [next_non_heartbeat(socket) for _ in range(RETENTION)]

    assert [frame["type"] for frame in replayed] == ["event"] * RETENTION
    assert [frame["seq"] for frame in replayed] == [4, 5, 6, 7]
    assert [frame["payload"] for frame in replayed] == [
        {"note": "four"},
        {"note": "five"},
        {"note": "six"},
        {"note": "seven"},
    ]
