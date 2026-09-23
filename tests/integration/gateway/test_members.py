"""Opening a project, and who may be in it, over HTTP.

Two properties carry the weight here, and they are the two the item is about.

**A project's authority comes from a membership row that somebody wrote.** A
user is created by an operator at a terminal and never over HTTP — this module
asserts that by probing every route the application declares with a valid token
and reading the accounts back afterwards. What a request can do is grant a role
to an account that already exists, and the grant names who conferred it.

**A withdrawal takes effect on the next request.** The token is a fact about
who is asking and carries no role, so a member whose membership is withdrawn is
refused on their very next call with the credential they already hold. That is
asserted with a token minted *before* the withdrawal, because a test that
signed in afterwards would pass whether or not the rule held.

The refusals are asserted as three different statuses on purpose. A caller who
is not in the project does not learn that it exists (404); a caller who is in
it but may not manage members is told so (403); and a request that conflicts
with a membership that is in force is neither of those (409).
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import a_project, account, bearer, sign_in
from tests.support.routes import EXPECTED_ROUTE_FLOOR, effective_routes, probe_path

from ravel.domain.enums import UserRole
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.outbox import events_since
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.tables import UserRow

pytestmark = pytest.mark.integration


def open_project(client: TestClient, token: str, **body: Any) -> dict[str, Any]:
    """Open a project the way a client does, and return what came back.

    Raises:
        AssertionError: The project was not opened, which means the test's setup
            is wrong rather than the thing it is about to assert.
    """
    response = client.post(
        "/projects",
        headers=bearer(token),
        json={
            "title": "Catalyst screen",
            "objective": "Find a better dopant.",
        }
        | body,
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def members_of(client: TestClient, project_id: str, token: str) -> list[dict[str, Any]]:
    """The live members as the owner's screen reads them."""
    response = client.get(f"/projects/{project_id}/members", headers=bearer(token))
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()
    return listed


# ── Opening a project ───────────────────────────────────────────────────────


def test_a_caller_opens_a_project_and_owns_it(
    client: TestClient, database: Database
) -> None:
    """The one membership that needs no granter, and it is the creator's.

    A project whose creator was not made its owner is a project nobody can
    direct and nobody can repair, since every later membership is conferred by
    an owner. So the two are one transaction, and what the case asserts is that
    they arrived together: the project exists and its owner is the caller.
    """
    account(database, username="ada")
    pair = sign_in(client, "ada")

    created = open_project(client, pair["access_token"])
    ada = _user_id(database, "ada")

    assert created["role"] == UserRole.PROJECT_OWNER.value
    (owner,) = members_of(client, created["project_id"], pair["access_token"])
    assert owner["user_id"] == ada
    assert owner["username"] == "ada"
    assert owner["granted_by"] is None, (
        "the first membership is the one that may have no granter, and the row "
        "says so rather than naming somebody who was not there"
    )

    with database.read_only() as session:
        stored = MembershipRepository(session, created["project_id"]).owners()
    assert [membership.user_id for membership in stored] == [ada], (
        "the project was opened and its ownership is not in the database"
    )


def test_opening_a_project_needs_an_account_and_a_body(
    client: TestClient, database: Database
) -> None:
    """Not registration, and not a project without an objective.

    An account is created by an operator at a terminal; this route needs one
    already. And a project with no objective is one Master's first turn has
    nothing to plan from, so both fields are required rather than defaulted —
    `"Untitled"` would be a title RAVEL invented rather than one somebody chose.
    """
    anonymous = client.post(
        "/projects", json={"title": "Mine", "objective": "Something."}
    )
    assert anonymous.status_code == 401

    account(database, username="ada")
    headers = bearer(sign_in(client, "ada")["access_token"])
    for body in ({}, {"title": "Mine"}, {"objective": "Something."}):
        response = client.post("/projects", headers=headers, json=body)
        assert response.status_code == 422, body

    with database.read_only() as session:
        assert UserRepository(session).by_username("ada") is not None


# ── Reading who is in a project ─────────────────────────────────────────────


def test_an_owner_reads_the_live_members(
    client: TestClient, database: Database, project: Project
) -> None:
    """The list is the ones whose authority is in force, with names attached.

    Withdrawn rows are absent rather than flagged, because this is the list an
    owner manages: a membership that is not in force is not a member. Who *was*
    one is a question about history, and the event stream is where it is
    answered.

    The fixture's own owner is in the list and is not noise: it is the first
    membership, granted to nobody by nobody, and a roster that left it out
    would be a roster of somebody's opinion rather than of the project.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    lab = account(database, username="ben", role=UserRole.LAB_USER, project=project)
    pair = sign_in(client, "ada")

    listed = members_of(client, project.project_id, pair["access_token"])

    assert [member["username"] for member in listed] == ["owner", "ada", "ben"]
    assert [member["role"] for member in listed] == [
        UserRole.PROJECT_OWNER.value,
        UserRole.PROJECT_OWNER.value,
        UserRole.LAB_USER.value,
    ]
    # `account` grants as the project's creator, so the granter on this row is
    # the fixture's owner rather than the caller reading the list — which is
    # the point: the column records who conferred the role, not who is looking.
    assert listed[2]["granted_by"] == "owner", (
        "the list does not say who conferred the role"
    )
    assert listed[0]["granted_by"] is None, "the first membership has no granter"

    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            lab, revoked_by=_user_id(database, "ada")
        )

    assert [member["username"] for member in members_of(
        client, project.project_id, pair["access_token"]
    )] == ["owner", "ada"]


def test_a_member_who_may_not_manage_members_cannot_read_them(
    client: TestClient, database: Database, project: Project
) -> None:
    """A lab user reaches none of these routes, and the refusal says why."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="ben", role=UserRole.LAB_USER, project=project)
    pair = sign_in(client, "ben")

    response = client.get(
        f"/projects/{project.project_id}/members", headers=bearer(pair["access_token"])
    )

    assert response.status_code == 403
    assert "PROJECT_OWNER" in response.json()["detail"]


def test_a_foreign_project_and_a_missing_one_are_one_answer(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """Roster included: a caller who is not in a project does not learn that it
    has one, or how many people are in it."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")

    theirs = client.get(
        f"/projects/{other_project.project_id}/members",
        headers=bearer(pair["access_token"]),
    )
    nowhere = client.get("/projects/no-such-project/members", headers=bearer(pair["access_token"]))

    assert theirs.status_code == nowhere.status_code == 404
    assert theirs.json()["detail"].replace(other_project.project_id, "") == (
        nowhere.json()["detail"].replace("no-such-project", "")
    ), "one of the two refusals learned something to say"
    assert other_project.title not in theirs.text


# ── Adding somebody ─────────────────────────────────────────────────────────


def test_an_owner_adds_an_existing_account(
    client: TestClient, database: Database, project: Project
) -> None:
    """Named by username, because that is what the person adding them knows.

    The account has to exist first, and the second half is the one that says
    so: an owner cannot conjure a person, only give one a role.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="ben")
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/members",
        headers=headers,
        json={"username": "ben", "role": UserRole.LAB_USER.value},
    )

    assert response.status_code == 201, response.text
    assert response.json()["username"] == "ben"
    assert response.json()["granted_by"] == "ada"
    assert [member["username"] for member in members_of(
        client, project.project_id, pair["access_token"]
    )] == ["owner", "ada", "ben"]

    stranger = client.post(
        f"/projects/{project.project_id}/members",
        headers=headers,
        json={"username": "nobody-here", "role": UserRole.LAB_USER.value},
    )
    assert stranger.status_code == 404
    assert "nobody-here" in stranger.json()["detail"]


def test_an_owner_may_add_an_administrator(
    client: TestClient, database: Database, project: Project
) -> None:
    """The operational role is conferrable, and it confers nothing scientific.

    `ADMIN` outranks `PROJECT_OWNER` in the domain's ranking, which exists so
    that an administrator may *answer* an approval. Conferring it is a
    different question, and the check that governs it is `may_direct_project`:
    the owner parts with a standing they never had.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="root")
    pair = sign_in(client, "ada")

    response = client.post(
        f"/projects/{project.project_id}/members",
        headers=bearer(pair["access_token"]),
        json={"username": "root", "role": UserRole.ADMIN.value},
    )

    assert response.status_code == 201, response.text
    assert response.json()["role"] == UserRole.ADMIN.value


def test_a_user_who_already_holds_a_role_is_a_conflict(
    client: TestClient, database: Database, project: Project
) -> None:
    """409 rather than 403, because the caller may add people.

    What is wrong is not who is asking but what is asked: `ben` holds a role
    here, and one row may not stand for two grants of authority. The sentence
    names the way out — withdraw, then grant — so that both are on the record.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="ben", role=UserRole.LAB_USER, project=project)
    pair = sign_in(client, "ada")

    response = client.post(
        f"/projects/{project.project_id}/members",
        headers=bearer(pair["access_token"]),
        json={"username": "ben", "role": UserRole.PROJECT_OWNER.value},
    )

    assert response.status_code == 409, response.text
    assert "withdrawing it and granting" in response.json()["detail"]


def test_a_lab_user_cannot_add_anybody(
    client: TestClient, database: Database, project: Project
) -> None:
    """The escalation this route could otherwise be: a lab user granting a
    role, which is the one write in the system that manufactures authority."""
    account(database, username="ben", role=UserRole.LAB_USER, project=project)
    account(database, username="mallory")
    pair = sign_in(client, "ben")

    response = client.post(
        f"/projects/{project.project_id}/members",
        headers=bearer(pair["access_token"]),
        json={"username": "mallory", "role": UserRole.LAB_USER.value},
    )

    assert response.status_code == 403
    with database.read_only() as session:
        assert (
            MembershipRepository(session, project.project_id).for_user(
                _user_id(database, "mallory")
            )
            is None
        )


def test_an_administrator_cannot_add_anybody(
    client: TestClient, database: Database, project: Project
) -> None:
    """Administering a runtime is not managing a project's people.

    The case a rank comparison would get wrong: `ADMIN` outranks
    `PROJECT_OWNER`, so a check written as "at least as much authority as the
    role you are granting" would let this through.
    """
    account(database, username="root", role=UserRole.ADMIN, project=project)
    account(database, username="mallory")
    pair = sign_in(client, "root")

    response = client.post(
        f"/projects/{project.project_id}/members",
        headers=bearer(pair["access_token"]),
        json={"username": "mallory", "role": UserRole.LAB_USER.value},
    )

    assert response.status_code == 403
    assert "PROJECT_OWNER" in response.json()["detail"]


# ── Withdrawing a role ──────────────────────────────────────────────────────


def test_an_owner_withdraws_a_role_and_it_stops_working_at_once(
    client: TestClient, database: Database, project: Project
) -> None:
    """The token is not the authority, so nothing has to expire for this to bite.

    `ben` signs in *before* the withdrawal, so the credential used afterwards is
    one the Gateway has already accepted. It is refused on the next request
    because the membership is read per request from PostgreSQL — which is the
    whole reason authority is a row rather than a claim inside the token.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    ben = account(database, username="ben", role=UserRole.LAB_USER, project=project)
    ada = sign_in(client, "ada")
    token = sign_in(client, "ben")["access_token"]

    assert (
        client.get(
            f"/projects/{project.project_id}", headers=bearer(token)
        ).status_code
        == 200
    )

    response = client.post(
        f"/projects/{project.project_id}/members/{ben}/revoke",
        headers=bearer(ada["access_token"]),
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == UserRole.LAB_USER.value

    after = client.get(f"/projects/{project.project_id}", headers=bearer(token))
    assert after.status_code == 404, "a withdrawn member still holds their old authority"
    assert "ben" not in str(members_of(client, project.project_id, ada["access_token"]))


def test_the_last_owner_cannot_step_down(
    client: TestClient, database: Database
) -> None:
    """409, because the request conflicts with a fact rather than with the
    caller's authority: the caller *is* an owner, and the project would be left
    with nobody able to direct it and nothing in RAVEL able to restore one.

    A project of its own, because the fixture's has two owners and stepping
    down from a project somebody else still directs is a handover — which the
    repository suite exercises. This is the case where there is nobody left.
    """
    alone = a_project(database, title="On my own")
    ada = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=alone)
    pair = sign_in(client, "ada")

    response = client.post(
        f"/projects/{alone.project_id}/members/{ada}/revoke",
        headers=bearer(pair["access_token"]),
    )

    assert response.status_code == 409, response.text
    assert "last owner" in response.json()["detail"]
    assert [member["username"] for member in members_of(
        client, alone.project_id, pair["access_token"]
    )] == ["ada"], "the refusal did not leave the project with the owner it had"


def test_a_non_member_cannot_be_withdrawn(
    client: TestClient, database: Database, project: Project
) -> None:
    """The same 404 as a missing project, for the same reason: this caller does
    not learn who is in it, or whether the identifier names anybody at all."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    stranger = account(database, username="mallory")
    pair = sign_in(client, "ada")

    response = client.post(
        f"/projects/{project.project_id}/members/{stranger}/revoke",
        headers=bearer(pair["access_token"]),
    )

    assert response.status_code == 404


# ── The record ──────────────────────────────────────────────────────────────


def test_every_membership_change_is_on_the_record(
    client: TestClient, database: Database
) -> None:
    """Authority is where the separation of powers lives, so both directions of
    it are facts in the stream rather than columns somebody has to interpret.

    A grant names who conferred it and a withdrawal names who withdrew it,
    which is what makes "who gave this person their role, and when did it end"
    answerable from the record alone.
    """
    fresh = a_project(database, title="Managed by one person")
    ada = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=fresh)
    ben = account(database, username="ben")
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    client.post(
        f"/projects/{fresh.project_id}/members",
        headers=headers,
        json={"username": "ben", "role": UserRole.ADMIN.value},
    )
    client.post(
        f"/projects/{fresh.project_id}/members/{ben}/revoke", headers=headers
    )

    with database.read_only() as session:
        changes = [
            event
            for event in events_since(session, fresh.project_id)
            if event.event_type
            in (ProjectEventType.MEMBER_ADDED, ProjectEventType.MEMBER_REVOKED)
        ]

    # Ada opening the project, ben granted the operational role, that role
    # withdrawn — three facts, and the last two name the same membership.
    assert [event.event_type for event in changes] == [
        ProjectEventType.MEMBER_ADDED,
        ProjectEventType.MEMBER_ADDED,
        ProjectEventType.MEMBER_REVOKED,
    ]
    assert changes[0].payload == {
        "membership_id": changes[0].payload["membership_id"],
        "user_id": ada,
        "role": UserRole.PROJECT_OWNER.value,
    }
    assert changes[0].actor_type is ActorType.SYSTEM, (
        "the first membership was granted to nobody by nobody; the record says so"
    )
    assert changes[1].payload == {
        "membership_id": changes[2].payload["membership_id"],
        "user_id": ben,
        "role": UserRole.ADMIN.value,
    }
    assert [event.actor_id for event in changes[1:]] == [ada, ada], (
        "the record does not say who made the change"
    )


def test_no_route_creates_an_account(
    app: FastAPI, client: TestClient, database: Database, project: Project
) -> None:
    """There is no open registration, asserted by trying to find one.

    Every route is probed with an owner's token — and one with nobody's — and
    the accounts are counted afterwards. The route that adds a member is in the
    list and cannot pass this test by accident: it names an account that does
    not exist, so it refuses. An operator creates users at a terminal, through
    `scripts/create_account.py`; nothing over HTTP does.

    The floor is asserted before the probe for the reason
    `tests/support/routes.py` gives: a walk that found nothing would make this
    test pass without checking anything.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    routes = effective_routes(app)
    assert len(routes) >= EXPECTED_ROUTE_FLOOR, (
        f"enumerated only {len(routes)} routes, which means the walk is broken "
        "and this test is about to assert nothing"
    )
    before = _accounts(database)

    probed: list[str] = []
    for route in routes:
        if not route.is_http or "POST" not in route.methods:
            continue
        path = probe_path(
            route.path,
            project_id=project.project_id,
            # Absent on purpose. The project is real because membership is
            # checked before the route body runs; everything the body would
            # address is not, so a route that writes something aimed at a task
            # refuses instead — and a probe that named a real one would be
            # measuring what those routes do rather than what this one is about.
            node_id="a-node-that-does-not-exist",
            task_id="a-task-that-does-not-exist",
        )
        for sent in (headers, {}):
            client.request("POST", path, headers=sent, json={})
        probed.append(f"POST {path}")

    assert any(path.endswith("/members") for path in probed), probed
    assert _accounts(database) == before, (
        "a route created an account; registration is an operator's, at a terminal"
    )


def _accounts(database: Database) -> set[str]:
    """Every account's identifier, read from the database rather than the app."""
    with database.read_only() as session:
        return {row.user_id for row in session.query(UserRow).all()}


def _user_id(database: Database, username: str) -> str:
    """The identifier behind a username, read back the way a request finds them."""
    with database.read_only() as session:
        user = UserRepository(session).by_username(username)
        assert user is not None
        return user.user_id
