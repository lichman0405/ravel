"""T13-AC1: the project-scoped stream, its sequence, and a revocation.

Three properties, each read from the live application against PostgreSQL:

- events belong to one project and carry a sequence that only moves forward;
- a client that disconnects and resumes from the last sequence it processed
  receives everything after it, once, with no gap;
- a membership withdrawn while a socket is open closes that socket within the
  five seconds the task allows.

The events are appended through the outbox and the withdrawal through the
membership repository, which is what a worker and an owner's request do. The
assertions then read the same rows back with SQL.
"""

from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient
from tests.premium.T13.conftest import account, bearer, sign_in
from tests.premium.T13.support import (
    collect_until_heartbeats,
    emit_note,
    next_non_heartbeat,
    receive_until_close,
    stored_events,
)

from ravel.domain.enums import UserRole
from ravel.domain.events import ProjectEventType
from ravel.domain.project import Project
from ravel.gateway.routes.events import CLOSE_NO_SUCH_PROJECT
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository
from ravel.state.repositories.projects import ProjectRegistry

pytestmark = pytest.mark.integration


def test_the_stream_is_scoped_to_one_project_and_its_sequence_only_moves_forward(
    client: TestClient, database: Database, project: Project
) -> None:
    """One socket sees one project's events, in sequence order.

    The sibling project is written to while the socket is open. Its sequences
    start at one as well, so a feed that had lost its scope would have something
    visibly wrong to send rather than merely a missing field — the frame's
    `project_id` and the monotonic sequence are asserted together.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    assert [event.seq for event in stored_events(database, project.project_id)] == [1, 2, 3]

    with database.transaction() as session:
        elsewhere = ProjectRegistry(session).create(
            title="Somebody else's project",
            objective="Not this one.",
            created_by="someone",
        )

    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq=0", headers=headers
    ) as socket:
        anchor = socket.receive_json()
        assert anchor == {
            "type": "anchor",
            "project_id": project.project_id,
            "display_id": project.display_id,
            "status": project.status.value,
            "head_seq": 3,
        }

        repeated = [next_non_heartbeat(socket) for _ in range(3)]
        assert [frame["seq"] for frame in repeated] == [1, 2, 3]
        assert [frame["event_type"] for frame in repeated] == [
            ProjectEventType.PROJECT_CREATED.value,
            ProjectEventType.MEMBER_ADDED.value,
            ProjectEventType.MEMBER_ADDED.value,
        ]
        assert {frame["project_id"] for frame in repeated} == {project.project_id}

        emit_note(database, project.project_id, "four")
        emit_note(database, project.project_id, "five")
        live = [next_non_heartbeat(socket) for _ in range(2)]
        assert [frame["seq"] for frame in live] == [4, 5]
        assert [frame["payload"] for frame in live] == [
            {"note": "four"},
            {"note": "five"},
        ]

        # The other project moves while this socket is watching. It has its own
        # sequence starting at one, and none of it may appear here.
        emit_note(database, elsewhere.project_id, "not yours")
        emit_note(database, elsewhere.project_id, "still not yours")
        quiet = collect_until_heartbeats(socket, beats=2)

    assert all(frame["type"] == "heartbeat" for frame in quiet), quiet
    assert all(frame.get("project_id") != elsewhere.project_id for frame in quiet)
    assert [event.seq for event in stored_events(database, elsewhere.project_id)] == [1, 2, 3]


def test_a_reconnect_from_the_last_processed_seq_loses_nothing_and_repeats_nothing(
    client: TestClient, database: Database, project: Project
) -> None:
    """The sequence is the cursor; resuming after it is exclusive.

    Seven events happen around two connections. The first reads one through
    five and records that five was its last processed sequence. Two more commit
    while it is disconnected. The second connection asks for everything after
    five and is given six and seven — no event is missing, and none of one
    through five is sent again, so a client that deduplicates by `seq` has
    nothing left to discard.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    seen: list[int] = []
    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq=0", headers=headers
    ) as socket:
        assert socket.receive_json()["head_seq"] == 3
        seen.extend(frame["seq"] for frame in (next_non_heartbeat(socket) for _ in range(3)))

        emit_note(database, project.project_id, "four")
        emit_note(database, project.project_id, "five")
        seen.extend(frame["seq"] for frame in (next_non_heartbeat(socket) for _ in range(2)))

    assert seen == [1, 2, 3, 4, 5]
    last_processed = seen[-1]

    emit_note(database, project.project_id, "six")
    emit_note(database, project.project_id, "seven")

    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq={last_processed}",
        headers=headers,
    ) as socket:
        resumed = socket.receive_json()
        assert resumed["type"] == "anchor"
        assert resumed["head_seq"] == 7

        caught_up = [next_non_heartbeat(socket) for _ in range(2)]
        seen.extend(frame["seq"] for frame in caught_up)

    assert [frame["seq"] for frame in caught_up] == [6, 7]
    assert [frame["payload"] for frame in caught_up] == [
        {"note": "six"},
        {"note": "seven"},
    ]
    # The whole record, exactly once, in order: no gap and no duplicate.
    assert seen == [1, 2, 3, 4, 5, 6, 7]
    assert len(seen) == len(set(seen))
    assert [event.seq for event in stored_events(database, project.project_id)] == seen


def test_a_revoked_membership_closes_an_open_stream_within_five_seconds(
    client: TestClient, database: Database, project: Project
) -> None:
    """A connection is not a grant.

    The socket is opened by a lab user, given its replay, and then the owner
    withdraws that membership. The reauthorization watcher re-reads the same
    membership row every request reads, so the close follows from PostgreSQL
    rather than from a second policy. The code is the one the HTTP surface would
    have answered with: the project is now not one this caller may see.
    """
    lab_user_id = account(
        database, username="lab", role=UserRole.LAB_USER, project=project
    )
    headers = bearer(sign_in(client, "lab")["access_token"])

    started = time.monotonic()
    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq=0", headers=headers
    ) as socket:
        assert socket.receive_json()["head_seq"] == 3
        for _ in range(3):
            next_non_heartbeat(socket)

        revoked_at = time.monotonic()
        with database.transaction() as session:
            MembershipRepository(session, project.project_id).revoke(
                lab_user_id, revoked_by=project.created_by
            )
        code = receive_until_close(socket, timeout=5.0)
        closed_after = time.monotonic() - revoked_at

    assert code == CLOSE_NO_SUCH_PROJECT == 4404
    assert closed_after < 5.0
    assert time.monotonic() - started < 5.0

    with database.read_only() as session:
        repository = MembershipRepository(session, project.project_id)
        assert repository.for_user(lab_user_id) is None
        history = repository.history_for_user(lab_user_id)
    assert [membership.revoked_by for membership in history] == [project.created_by]
    assert ledger_has_revocation(database, project.project_id)


def ledger_has_revocation(database: Database, project_id: str) -> bool:
    """Whether the withdrawal is in the project's own event record."""
    return any(
        event.event_type is ProjectEventType.MEMBER_REVOKED
        for event in stored_events(database, project_id)
    )
