"""T13-AC2: authorization scopes the snapshot, the replay, and the error shape.

A project identifier in a path is a request, not an authority. Every route here
resolves the caller's live membership for the project in the path and answers
404 for a project they are not in — the same answer whether the project exists
or not. An expired token fails before any project is read, which is the same
order the HTTP and socket surfaces use.

The error envelope is asserted alongside, because isolation that leaks through
an error message is not isolation: the body carries a stable code, the request
identifier, and no submitted input.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect
from tests.premium.T13.conftest import SECRET, account, bearer, sign_in

from ravel.domain.clock import utcnow
from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.routes.events import (
    CLOSE_NO_SUCH_PROJECT,
    CLOSE_UNAUTHENTICATED,
)
from ravel.state.database import Database

pytestmark = pytest.mark.integration


def test_a_token_for_one_project_cannot_read_another_projects_snapshot(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """Membership is read from PostgreSQL for the project in the path.

    The same token reads its own snapshot and is refused the other project's.
    The refusal does not say whether the other project exists: 404 with the same
    sentence an absent identifier gets.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    own = client.get(f"/v1/projects/{project.project_id}/snapshot", headers=headers)
    assert own.status_code == 200, own.text
    assert own.json()["project"]["project_id"] == project.project_id

    foreign = client.get(
        f"/v1/projects/{other_project.project_id}/snapshot", headers=headers
    )
    assert foreign.status_code == 404, foreign.text
    body = foreign.json()
    assert set(body) == {"code", "message", "request_id", "details"}
    assert body["code"] == "NOT_FOUND"
    assert body["details"] == {}
    assert "snapshot" not in body and "counts" not in body

    absent = client.get("/v1/projects/prj_does_not_exist/snapshot", headers=headers)
    assert absent.status_code == foreign.status_code == 404
    assert absent.json()["message"] == body["message"].replace(
        other_project.project_id, "prj_does_not_exist"
    )

    dag = client.get(f"/v1/projects/{other_project.project_id}/dag", headers=headers)
    assert dag.status_code == 404, dag.text
    assert dag.json()["code"] == "NOT_FOUND"
    assert "items" not in dag.json()

    # The legacy path keeps the answer it had before `/v1` existed. A TUI that
    # has not moved yet sees the refusal shape it already handles.
    legacy = client.get(f"/projects/{other_project.project_id}/dag", headers=headers)
    assert legacy.status_code == 404
    assert set(legacy.json()) == {"detail"}


def test_an_expired_token_cannot_read_a_snapshot_or_open_a_replay(
    client: TestClient, database: Database, project: Project
) -> None:
    """Expiry is checked before the project is, on both transports.

    The token is genuinely signed by the same service and genuinely expired; it
    is not a malformed string wearing expiry's clothes. Both refusals are the
    same "sign in again" answer, and neither reveals which check failed.
    """
    user_id = account(
        database, username="ada", role=UserRole.PROJECT_OWNER, project=project
    )
    expired, grant = TokenService(secret=SECRET, ttl_seconds=60).issue_access(
        user_id, now=utcnow() - timedelta(hours=1)
    )
    assert grant.expires_at < utcnow()

    response = client.get(
        f"/v1/projects/{project.project_id}/snapshot", headers=bearer(expired)
    )
    assert response.status_code == 401, response.text
    body = response.json()
    assert body["code"] == "UNAUTHENTICATED"
    assert body["request_id"]
    assert response.headers["X-Request-ID"] == body["request_id"]
    assert "snapshot" not in body
    assert "expired" not in body["message"].lower()

    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq=0",
        headers=bearer(expired),
    ) as socket, pytest.raises(WebSocketDisconnect) as refused:
        socket.receive_json()
    assert refused.value.code == CLOSE_UNAUTHENTICATED == 4401

    # The same account's live token still works, so the refusal was the expiry
    # rather than the account or the project.
    valid = bearer(sign_in(client, "ada")["access_token"])
    assert client.get(
        f"/v1/projects/{project.project_id}/snapshot", headers=valid
    ).status_code == 200


def test_a_non_member_cannot_replay_a_projects_stream(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """Neither an existing project nor an invented one is a replay target.

    The two attempts produce the same close code, so the socket does not become
    an oracle for which projects exist. The caller is a member of one project
    and asks for two it is not in.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    for target in (other_project.project_id, "prj_does_not_exist"):
        with client.websocket_connect(
            f"/v1/projects/{target}/events?after_seq=0", headers=headers
        ) as socket, pytest.raises(WebSocketDisconnect) as refused:
            socket.receive_json()
        assert refused.value.code == CLOSE_NO_SUCH_PROJECT == 4404

    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq=0", headers=headers
    ) as socket:
        assert socket.receive_json()["type"] == "anchor"


def test_a_v1_error_is_stable_and_does_not_echo_submitted_input(
    client: TestClient, project: Project
) -> None:
    """The envelope a generated client switches on, with the caller's trace id.

    The login body deliberately carries a password-shaped value in the wrong
    field. Pydantic's raw error would quote it; the v1 envelope does not, and
    that is the difference between an error body and a credential in a log.
    """
    supplied = "hunter2-should-never-be-echoed"
    response = client.post(
        "/v1/auth/login",
        json={"username": "ada", "password": 7, "secret_note": supplied},
        headers={"X-Request-ID": "trace-t13-0001"},
    )
    assert response.status_code == 422, response.text
    assert response.headers["X-Request-ID"] == "trace-t13-0001"
    body = response.json()
    assert set(body) == {"code", "message", "request_id", "details"}
    assert body["code"] == "INVALID_REQUEST"
    assert body["request_id"] == "trace-t13-0001"
    assert supplied not in response.text
    assert "hunter2" not in response.text
    errors = body["details"]["errors"]
    assert errors
    assert all(set(error) == {"loc", "message", "type"} for error in errors)
    assert all("input" not in error and "ctx" not in error for error in errors)

    unsafe = "bad id with spaces"
    unsafe_response = client.post(
        "/v1/auth/login",
        json={},
        headers={"X-Request-ID": unsafe},
    )
    assert unsafe_response.status_code == 422
    generated = unsafe_response.headers["X-Request-ID"]
    assert " " not in generated
    assert generated != unsafe
    assert len(generated) == 32
