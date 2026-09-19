"""Login, the authority a token does and does not carry, and rotation.

The properties worth asserting here are the ones that decide whether the rest
of the Gateway's authorization is meaningful:

- a token says *who*, and nothing else, so a stolen one cannot be aimed at a
  project its owner was never a member of;
- a wrong username and a wrong password are one answer, so the endpoint is not
  an oracle for which accounts exist;
- a refresh grant is single-use, and presenting one twice cuts the chain.

The last of those is `RefreshTokenRepository`'s rule and is tested against the
repository in `tests/integration/state/test_refresh_tokens.py`. What is added
here is that the *route* lets that refusal through as one answer and does not
report which of the four causes it was.
"""

from __future__ import annotations

import jwt
import pytest
from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import (
    PASSWORD,
    account,
    bearer,
    deactivated_account,
    sign_in,
)

from ravel.domain.enums import UserRole
from ravel.domain.ids import new_id
from ravel.domain.project import Project
from ravel.gateway.auth.tokens import TokenService
from ravel.state.database import Database

pytestmark = pytest.mark.integration


# ── Logging in ──────────────────────────────────────────────────────────────


def test_a_password_and_username_exchange_for_tokens(
    client: TestClient, database: Database
) -> None:
    """The pair a client keeps, and who it says they are."""
    account(database, username="ada")

    pair = sign_in(client, "ada")

    assert pair["token_type"] == "bearer"
    assert pair["username"] == "ada"
    assert pair["user_id"]
    assert pair["expires_in"] > 0
    assert pair["access_token"] and pair["refresh_token"]


def test_a_wrong_password_is_refused(client: TestClient, database: Database) -> None:
    account(database, username="ada")

    response = client.post(
        "/auth/login", json={"username": "ada", "password": "not the password"}
    )

    assert response.status_code == 401
    assert "WWW-Authenticate" in response.headers


def test_an_unknown_account_and_a_wrong_password_read_the_same(
    client: TestClient, database: Database
) -> None:
    """One answer for both, so the endpoint does not enumerate accounts.

    Compared in full — status, body, and headers — because a difference in any
    of them is the difference an attacker reads. Timing is equalised in the
    route by verifying against a stand-in hash when no account matches; that
    is not asserted here, because a timing assertion in CI is a flaky test
    rather than a proof.
    """
    account(database, username="ada")

    wrong_password = client.post(
        "/auth/login", json={"username": "ada", "password": "not the password"}
    )
    unknown_account = client.post(
        "/auth/login", json={"username": "nobody", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_account.status_code == 401
    assert wrong_password.json() == unknown_account.json()
    assert wrong_password.headers["www-authenticate"] == (
        unknown_account.headers["www-authenticate"]
    )


def test_a_deactivated_account_cannot_log_in(client: TestClient, database: Database) -> None:
    """An account can exist and still not be one anybody may use.

    The password here is correct, so what refuses the login is `is_active`
    alone — which is the point of checking it separately from the hash.
    """
    deactivated_account(database, user_id=new_id(), username="retired")

    response = client.post("/auth/login", json={"username": "retired", "password": PASSWORD})

    assert response.status_code == 401


# ── What the token is for ───────────────────────────────────────────────────


def test_a_token_identifies_a_user_and_carries_no_authority(
    database: Database, tokens: TokenService, project: Project
) -> None:
    """The claim `docs/09` makes twice, asserted against the token itself.

    The token has a subject and an expiry and no project and no role, which is
    what makes a stolen token useless against a project its owner was never in.
    Read here by decoding the claims rather than by trusting the docstring,
    because the property is about what is *in* the token.
    """
    user_id = account(
        database, username="ada", role=UserRole.PROJECT_OWNER, project=project
    )

    token, _grant = tokens.issue_access(user_id)

    claims = jwt.decode(token, options={"verify_signature": False})
    assert claims["sub"] == user_id
    assert set(claims) == {"sub", "jti", "typ", "iat", "exp"}, (
        "an access token grew a claim that carries authority"
    )


def test_a_token_signed_with_another_secret_is_refused(
    client: TestClient, database: Database
) -> None:
    """The signature is checked, so a forged token is not a way in."""
    account(database, username="ada")
    forger = TokenService(secret="a-different-secret-of-sufficient-length", ttl_seconds=60)
    forged, _grant = forger.issue_access("some-user-id")

    response = client.get("/auth/me", headers=bearer(forged))

    assert response.status_code == 401


def test_a_malformed_or_absent_token_is_refused(client: TestClient) -> None:
    """Three ways of not having a token, all answered the same way."""
    assert client.get("/auth/me").status_code == 401
    assert client.get("/auth/me", headers=bearer("")).status_code == 401
    assert client.get("/auth/me", headers=bearer("not.a.token")).status_code == 401


def test_a_token_for_a_deleted_account_is_refused(
    client: TestClient, database: Database, tokens: TokenService
) -> None:
    """A token can outlive the account's usefulness, so the row is re-read.

    The token was validly issued and has not expired. What refuses it is that
    the account it names is not active, checked on every request rather than
    only at login — otherwise a token would remain a way in for as long as it
    had left to run.
    """
    user_id = new_id()
    deactivated_account(database, user_id=user_id, username="retired")
    token, _grant = tokens.issue_access(user_id)

    assert client.get("/auth/me", headers=bearer(token)).status_code == 401


# ── Who am I ────────────────────────────────────────────────────────────────


def test_me_reports_the_memberships_a_user_holds(
    client: TestClient, database: Database, project: Project
) -> None:
    """The one place a user's own authority is enumerated."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)

    pair = sign_in(client, "ada")
    response = client.get("/auth/me", headers=bearer(pair["access_token"]))

    assert response.status_code == 200
    body = response.json()
    assert body["username"] == "ada"
    assert body["memberships"] == [
        {"project_id": project.project_id, "role": "PROJECT_OWNER"}
    ]


def test_a_user_with_no_membership_sees_no_projects(
    client: TestClient, database: Database
) -> None:
    """Authenticating is not the same as belonging anywhere."""
    account(database, username="ada")

    pair = sign_in(client, "ada")
    response = client.get("/auth/me", headers=bearer(pair["access_token"]))

    assert response.status_code == 200
    assert response.json()["memberships"] == []


# ── Rotation ────────────────────────────────────────────────────────────────


def test_refreshing_returns_a_new_pair_and_spends_the_old_grant(
    client: TestClient, database: Database
) -> None:
    """The exchange a client makes every hour rather than logging in again."""
    account(database, username="ada")
    pair = sign_in(client, "ada")

    response = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})

    assert response.status_code == 200
    rotated = response.json()
    assert rotated["refresh_token"] != pair["refresh_token"]
    assert rotated["username"] == "ada"
    assert client.get("/auth/me", headers=bearer(rotated["access_token"])).status_code == 200


def test_an_unknown_refresh_grant_is_refused(client: TestClient) -> None:
    response = client.post("/auth/refresh", json={"refresh_token": "never-issued"})

    assert response.status_code == 401


def test_replaying_a_refresh_grant_is_refused_and_cuts_the_chain(
    client: TestClient, database: Database
) -> None:
    """Two parties holding one secret is the case rotation exists for.

    The refusal is the same 401 as any other, so the thief is not told the
    theft was noticed — and the successor they might be holding is dead too.
    """
    account(database, username="ada")
    pair = sign_in(client, "ada")
    rotated = client.post(
        "/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    ).json()

    replayed = client.post("/auth/refresh", json={"refresh_token": pair["refresh_token"]})
    successor = client.post("/auth/refresh", json={"refresh_token": rotated["refresh_token"]})

    assert replayed.status_code == 401
    assert successor.status_code == 401, "the chain outlived the detection"
    assert replayed.json() == successor.json()


def test_logging_out_cuts_the_chain_and_is_idempotent(
    client: TestClient, database: Database
) -> None:
    """Logging out twice is not an error; neither is logging out a token nobody issued."""
    account(database, username="ada")
    pair = sign_in(client, "ada")

    first = client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]})
    second = client.post("/auth/logout", json={"refresh_token": pair["refresh_token"]})
    invented = client.post("/auth/logout", json={"refresh_token": "never-issued"})

    assert first.status_code == second.status_code == invented.status_code == 200
    assert client.post(
        "/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    ).status_code == 401
