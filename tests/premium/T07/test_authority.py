"""Who may act, and what a credential is allowed to say.

Four boundaries, each asserted at the surface a caller actually reaches:

- a token is a user identifier and nothing else — no role, no project — so a
  member of one project cannot read or write another, and a claim injected into
  a correctly-signed token changes nothing;
- authority is read from PostgreSQL on every request and every stream check, so
  a withdrawn membership is effective immediately and an open socket stops;
- a deactivated account is refused at login, on a live access token, and on
  refresh, with the decision recorded in an append-only audit row;
- no route lets an owner change the Scientific DAG, which is Master's.

The stream cases use the production reauthorization watcher with a short
interval (`conftest.STREAM_REAUTHORIZE_SECONDS`), not a different code path.
The five-second allowance is asserted as a bound on top of that.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest
from fastapi.routing import APIRoute
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from starlette.websockets import WebSocketDisconnect
from tests.integration.gateway.conftest import account, bearer, sign_in
from tests.premium.T07.conftest import PASSWORD, SECRET, deactivate, revoke

from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.routes.events import CLOSE_NO_SUCH_PROJECT, CLOSE_UNAUTHENTICATED
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository, UserRepository

#: The documented ceiling for a revoked member's stream to stop.
STREAM_BOUND_SECONDS = 5.0


def _wait_for_close(socket: Any, *, seconds: float = STREAM_BOUND_SECONDS) -> tuple[int, float]:
    """Read frames until the server closes, and report the code and the delay.

    The heartbeat keeps frames arriving, so a close is observed rather than
    inferred from a read that would otherwise block until the suite timed out.
    """
    started = time.monotonic()
    while time.monotonic() - started < seconds:
        try:
            socket.receive_json()
        except WebSocketDisconnect as refused:
            return int(refused.code), time.monotonic() - started
    raise AssertionError(f"the stream was still open {seconds:g}s after the authority changed")


# ── Project scope ───────────────────────────────────────────────────────────


def test_a_member_of_one_project_cannot_read_or_write_another(
    client: Any, database: Database, project: Project, other_project: Project
) -> None:
    """A foreign project is answered as though it did not exist.

    The upload probe is the one that matters most: the authorization dependency
    runs before the route body, so the refusal happens before a byte of the
    request is read or a store is asked to keep anything.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="bob", role=UserRole.PROJECT_OWNER, project=other_project)
    ada = bearer(sign_in(client, "ada")["access_token"])
    bob = bearer(sign_in(client, "bob")["access_token"])

    # A project this caller has no membership in, in either direction.
    for headers, foreign in ((ada, other_project), (bob, project)):
        read = client.get(f"/projects/{foreign.project_id}/artifacts", headers=headers)
        assert read.status_code == 404, read.text
        assert foreign.project_id in read.json()["detail"]

        write = client.post(
            f"/projects/{foreign.project_id}/artifacts",
            params={"name": "Exfiltration"},
            content=b"should never be read",
            headers=headers,
        )
        assert write.status_code == 404, write.text

        # And the same for a project that does not exist: one answer, so a
        # caller cannot use the difference to enumerate projects.
        missing = client.get(
            "/projects/prj_does_not_exist/artifacts", headers=headers
        )
        assert missing.status_code == 404, missing.text

    # The caller's own project still answers, so the refusal above is about
    # scope rather than about a route that never works.
    own = client.get(f"/projects/{project.project_id}/artifacts", headers=ada)
    assert own.status_code == 200, own.text


# ── Live authority ──────────────────────────────────────────────────────────


def test_a_revoked_membership_stops_the_next_request_and_an_open_stream(
    client: Any, database: Database, project: Project
) -> None:
    """The membership row is the authority, not the token and not the socket.

    The socket is opened while the membership is live, the row is withdrawn from
    another connection, and the server's own reauthorization watcher has to
    notice. The access token is unchanged throughout, which is the point: an
    access token is not revocable and does not need to be.
    """
    ada_id = account(database, username="ada", role=UserRole.LAB_USER, project=project)
    access = sign_in(client, "ada")["access_token"]
    headers = bearer(access)

    with client.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as socket:
        assert socket.receive_json()["type"] == "anchor"
        revoke(database, project, ada_id, by=project.created_by)
        code, elapsed = _wait_for_close(socket)

    assert code == CLOSE_NO_SUCH_PROJECT, code
    assert elapsed <= STREAM_BOUND_SECONDS, elapsed

    refused = client.get(f"/projects/{project.project_id}/artifacts", headers=headers)
    assert refused.status_code == 404, refused.text

    with database.read_only() as session:
        assert MembershipRepository(session, project.project_id).for_user(ada_id) is None
        # The withdrawal is history, not erasure: the row remains with a
        # revocation recorded beside it.
        history = MembershipRepository(session, project.project_id).history_for_user(ada_id)
        assert len(history) == 1 and history[0].revoked_at is not None

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["memberships"] == []


def test_a_deactivated_account_is_refused_at_every_door_and_the_decision_is_audited(
    client: Any, database: Database, project: Project
) -> None:
    """Login, access, and refresh all stop, and the stop is attributable.

    `users` is append-only, so deactivation is a row in `account_deactivations`.
    The audit must be readable afterwards and must not itself be editable; the
    account row is compared before and after to prove the two facts are kept
    apart.
    """
    ada_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")
    access_headers = bearer(pair["access_token"])

    with database.read_only() as session:
        before = UserRepository(session).get(ada_id)
    assert before.is_active is True

    deactivate(database, ada_id, by="operator:alice", reason="security review")

    with database.transaction() as session:
        audit = session.execute(
            text(
                "SELECT user_id, deactivated_by, reason, deactivated_at "
                "FROM account_deactivations WHERE user_id = :user_id"
            ),
            {"user_id": ada_id},
        ).mappings().one()
    assert audit["deactivated_by"] == "operator:alice"
    assert audit["reason"] == "security review"
    assert audit["deactivated_at"] is not None

    with database.read_only() as session:
        after = UserRepository(session).get(ada_id)
    assert after.is_active is False
    with database.read_only() as session:
        raw = session.execute(
            text("SELECT is_active FROM users WHERE user_id = :user_id"),
            {"user_id": ada_id},
        ).scalar_one()
    assert raw is True, "the account row was rewritten instead of audited beside"

    # A live access token is refused on the next request.
    refused = client.get("/auth/me", headers=access_headers)
    assert refused.status_code == 401, refused.text
    assert "not active" in refused.json()["detail"]

    # Login refuses the correct password.
    login = client.post("/auth/login", json={"username": "ada", "password": PASSWORD})
    assert login.status_code == 401, login.text

    # A refresh grant issued before deactivation is refused.
    refresh = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    assert refresh.status_code == 401, refresh.text

    # The audit row cannot be edited or deleted, and it survives both attempts.
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.execute(
            text(
                "UPDATE account_deactivations SET reason = 'rewritten' "
                "WHERE user_id = :user_id"
            ),
            {"user_id": ada_id},
        )
    with pytest.raises(IntegrityError), database.transaction() as session:
        session.execute(
            text("DELETE FROM account_deactivations WHERE user_id = :user_id"),
            {"user_id": ada_id},
        )

    with database.read_only() as session:
        assert session.execute(
            text(
                "SELECT reason FROM account_deactivations WHERE user_id = :user_id"
            ),
            {"user_id": ada_id},
        ).scalar_one() == "security review"


def test_deactivation_closes_an_open_stream_with_the_unauthenticated_code(
    client: Any, database: Database, project: Project
) -> None:
    """The socket's periodic check reads the account, not only the membership."""
    ada_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    access = sign_in(client, "ada")["access_token"]

    with client.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=bearer(access)
    ) as socket:
        assert socket.receive_json()["type"] == "anchor"
        deactivate(database, ada_id, by="operator:bob")
        code, elapsed = _wait_for_close(socket)

    assert code == CLOSE_UNAUTHENTICATED, code
    assert elapsed <= STREAM_BOUND_SECONDS, elapsed


# ── Forged and over-claiming identity ──────────────────────────────────────


def _claims(user_id: str, **extra: Any) -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "sub": user_id,
        "jti": "a-token-id",
        "typ": "access",
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=1)).timestamp()),
        **extra,
    }


def test_tokens_that_the_service_did_not_sign_are_refused(
    client: Any, database: Database, project: Project, tokens: TokenService
) -> None:
    """Wrong key, no signature, tampered bytes, expired, wrong kind, no user."""
    user_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)

    forged_secret = jwt.encode(_claims(user_id), "a-different-secret", algorithm="HS256")
    unsigned = jwt.encode(_claims(user_id), key="", algorithm="none")
    genuine, _ = tokens.issue_access(user_id)
    header, payload, signature = genuine.split(".")
    tampered = f"{header}.{payload}.{signature[:-2]}{'aa' if signature[-2:] != 'aa' else 'bb'}"
    expired, _ = tokens.issue_access(
        user_id, now=datetime.now(UTC) - timedelta(hours=3)
    )
    refresh_kind = jwt.encode(_claims(user_id, typ="refresh"), SECRET, algorithm="HS256")
    no_such_user, _ = tokens.issue_access("usr_thisuserdoesnotexist")

    for label, token in (
        ("forged secret", forged_secret),
        ("alg none", unsigned),
        ("tampered", tampered),
        ("expired", expired),
        ("wrong kind", refresh_kind),
        ("unknown user", no_such_user),
    ):
        response = client.get("/auth/me", headers=bearer(token))
        assert response.status_code in {401, 404}, (
            f"{label}: {response.status_code} {response.text}"
        )
        assert "access_token" not in response.text, label

    # The genuine token proves the probes were otherwise well formed.
    assert client.get("/auth/me", headers=bearer(genuine)).status_code == 200


def test_claims_outside_the_token_contract_confer_nothing(
    client: Any, database: Database, project: Project, other_project: Project
) -> None:
    """A correctly-signed token with `role` and `project_id` added.

    The signature is genuine and the extra claims are well formed, so every
    check that could accidentally read them would pass. The token is a user
    identifier: the role and the project are read from PostgreSQL on every
    request, and the injected values have to make no difference.
    """
    lab_id = account(database, username="lab", role=UserRole.LAB_USER, project=project)
    token = jwt.encode(
        _claims(
            lab_id,
            role=UserRole.PROJECT_OWNER.value,
            project_id=other_project.project_id,
            scope="admin",
            is_admin=True,
        ),
        SECRET,
        algorithm="HS256",
    )
    headers = bearer(token)

    me = client.get("/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    assert me.json()["memberships"] == [
        {"project_id": project.project_id, "role": UserRole.LAB_USER.value}
    ]

    # The claimed project is not one this user may reach.
    foreign = client.get(f"/projects/{other_project.project_id}/artifacts", headers=headers)
    assert foreign.status_code == 404, foreign.text

    # The claimed owner role does not satisfy the owner-only control surface.
    paused = client.post(f"/projects/{project.project_id}/pause", headers=headers)
    assert paused.status_code == 403, paused.text

    # The membership they actually hold still works.
    own = client.get(f"/projects/{project.project_id}/artifacts", headers=headers)
    assert own.status_code == 200, own.text


# ── The DAG is Master's ────────────────────────────────────────────────────


def test_no_route_lets_an_owner_change_the_dag(
    client: Any, database: Database, project: Project, app: Any
) -> None:
    """Structural and behavioural checks, in that order.

    The structural half fails at the point of the mistake: no mutating method
    is served on a path that names the DAG. The behavioural half probes the
    paths a mutation would plausibly use, then compares the whole graph — not a
    count — so a cancelled or re-bound node would be caught as well as an added
    one. The exhaustive walk over every route lives in `tests/e2e/test_tui.py`,
    which has a live server; this is the same claim held at the unit boundary.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    mutating = {"POST", "PUT", "PATCH", "DELETE"}
    offenders = [
        (route.path, sorted(method for method in (route.methods or set()) & mutating))
        for route in app.routes
        if isinstance(route, APIRoute) and "dag" in route.path.split("/")
    ]
    offenders = [entry for entry in offenders if entry[1]]
    assert offenders == [], f"mutating routes on the DAG: {offenders}"

    before = client.get(f"/projects/{project.project_id}/dag", headers=headers)
    assert before.status_code == 200, before.text

    probes = (
        ("POST", f"/projects/{project.project_id}/dag"),
        ("POST", f"/projects/{project.project_id}/dag/nodes"),
        ("PUT", f"/projects/{project.project_id}/dag/nodes/node_1"),
        ("PATCH", f"/projects/{project.project_id}/dag/nodes/node_1"),
        ("DELETE", f"/projects/{project.project_id}/dag/nodes/node_1"),
        ("POST", f"/projects/{project.project_id}/nodes"),
        ("PATCH", f"/projects/{project.project_id}/nodes/node_1"),
    )
    for method, path in probes:
        response = client.request(method, path, headers=headers, json={})
        assert response.status_code in {404, 405}, f"{method} {path}: {response.status_code}"

    after = client.get(f"/projects/{project.project_id}/dag", headers=headers)
    assert after.json() == before.json(), "a user-reachable route changed the Scientific DAG"
