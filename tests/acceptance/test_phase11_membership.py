"""P11-08: who may become a person, and who may hand out authority.

The item is a list of sentences about a boundary, and the boundary is worth
walking end to end because every piece that implements it looks reasonable on
its own:

    Server Operator → create User
    PROJECT_OWNER → create Project
    PROJECT_OWNER → add an existing LAB_USER / ADMIN
    不要增加 open registration
    ADMIN 是 runtime/admin role
    不是 scientific authority
    不是 platform superuser
    不能删除最后一个 PROJECT_OWNER
    membership mutation 必须 audit
    role 必须从 DB re-read
    不依赖旧 token 中缓存权限

(The two lines of the last requirement, and the three of the admin one, are one
sentence each in the directive, split here at their commas so that no line is a
paraphrase.)

Two suites below this one cover the parts. `tests/integration/gateway/
test_members.py` probes every route the application declares for a way to make
an account, and `tests/integration/state/test_identity.py` asserts what the
repository refuses. What neither does is run the *operator's own script* against
the database the Gateway is serving, over a real socket: a person created at a
terminal, a project opened over HTTP by that person, a role conferred on a third
person, and a withdrawal that stops working on a token issued before it. That is
this module — one story per claim, rather than one assertion per function.

**The only substitution is where the operator's script reads its settings.**
`scripts/create_account.py` builds its own `Database` from `Settings()`, which in
a test would be the deployment's — and a test that provisioned a user into a
deployment is a test nobody may run twice. Everything else is the operator's
real path: the argument parsing, the single transaction, the repositories, and
the Argon2id hash the account is stored with. The engine it opens is closed
afterwards, because production's script is the only thing running and a test's
is not.

**A role is read from the row on every request.** That is what makes the last
case here the important one: a token minted while somebody held `LAB_USER` is
still a genuine credential after their membership is withdrawn — `/auth/me`
keeps answering — and it reaches nothing, because the authority it never carried
is a row that has since been revoked. Re-granting the same person a different
role changes what that same token can see, without a second login.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, ClassVar

import httpx
import pytest
from scripts import create_account
from tests.e2e.conftest import LiveGateway
from tests.integration.gateway.conftest import PASSWORD, bearer
from tests.support.routes import EXPECTED_ROUTE_FLOOR, effective_routes, probe_path

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.domain.events import ActorType, ProjectEvent, ProjectEventType
from ravel.domain.identity import ProjectMembership
from ravel.state.database import Database, create_db_engine
from ravel.state.outbox import events_since
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.tables import UserRow

pytestmark = [pytest.mark.phase11, pytest.mark.acceptance]

#: The title and objective every project in this module is opened with. Named
#: rather than repeated, because two of the cases open two projects and a
#: difference between them would be a difference nothing asserts.
TITLE = "Catalyst screen"
OBJECTIVE = "Find a dopant that raises conductivity by 15%."


class _RememberedDatabase(Database):
    """The operator's `Database`, kept so the case can close the pool it opened.

    A subclass rather than a patch of `from_settings`, because the engine, the
    session factory and the transaction are all still the real ones — the only
    thing added is a reference for the fixture's `finally` to dispose.
    """

    #: What the last call to `from_settings` built, or `None` before one.
    opened: ClassVar[Database | None] = None

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> _RememberedDatabase:
        # Built rather than delegated so that what comes back is this class
        # rather than the base one: `Database.from_settings` is annotated with
        # its own name, and a subclass that returned it would be a subclass
        # nothing can hold a reference to.
        made = cls(create_db_engine(settings))
        _RememberedDatabase.opened = made
        return made


@pytest.fixture
def operator(
    monkeypatch: pytest.MonkeyPatch, integration_settings: Settings
) -> Iterator[Callable[[list[str]], int]]:
    """`scripts/create_account.py`, pointed at this case's database.

    The script's own `main`, so what a case calls is what an operator types —
    flags, refusals and all. See the module docstring for the one thing
    substituted and why it has to be.
    """
    monkeypatch.setattr(create_account, "Settings", lambda: integration_settings)
    monkeypatch.setattr(create_account, "Database", _RememberedDatabase)
    _RememberedDatabase.opened = None
    yield create_account.main
    if _RememberedDatabase.opened is not None:
        _RememberedDatabase.opened.dispose()


# ── The operator's script ───────────────────────────────────────────────────


def provisioned(operator: Callable[[list[str]], int], *argv: str) -> None:
    """Run the operator's script, insisting it provisioned what was asked.

    Raises:
        AssertionError: The script refused, which means the case's setup is
            wrong rather than the thing it is about to assert.
    """
    assert operator(list(argv)) == 0, f"the operator's script refused: {' '.join(argv)}"


def an_owner(operator: Callable[[list[str]], int], username: str, *, title: str = TITLE) -> None:
    """An account with a project of its own, as an operator would make one."""
    provisioned(
        operator,
        "--username",
        username,
        "--password",
        PASSWORD,
        "--new-project",
        "--title",
        title,
        "--objective",
        OBJECTIVE,
    )


def an_account(operator: Callable[[list[str]], int], username: str) -> None:
    """An account with no project and no membership, as an operator would make one."""
    provisioned(operator, "--username", username, "--password", PASSWORD, "--no-membership")


# ── Talking to the Gateway ──────────────────────────────────────────────────


def a_client(live: LiveGateway) -> httpx.AsyncClient:
    """An HTTP client for the running application, over its real socket."""
    return httpx.AsyncClient(base_url=live.url, timeout=20.0)


async def signed_in(
    client: httpx.AsyncClient, username: str, password: str = PASSWORD
) -> dict[str, str]:
    """Log in the way a client does, and return the header it then sends.

    Raises:
        AssertionError: The login was refused, which means the case's setup is
            wrong rather than the thing it is about to assert.
    """
    response = await client.post(
        "/auth/login", json={"username": username, "password": password}
    )
    assert response.status_code == 200, response.text
    return bearer(response.json()["access_token"])


async def opened(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    """Open a project, returning what came back.

    Raises:
        AssertionError: The project was not opened.
    """
    response = await client.post(
        "/projects", headers=headers, json={"title": TITLE, "objective": OBJECTIVE}
    )
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def only_project(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    """The one project a caller is in, as its identifier."""
    listed = (await client.get("/projects", headers=headers)).json()
    assert len(listed) == 1, f"expected one project, got {listed}"
    return str(listed[0]["project_id"])


async def members_of(
    client: httpx.AsyncClient, project_id: str, headers: dict[str, str]
) -> list[dict[str, Any]]:
    """The live members as somebody who may read them sees them.

    Raises:
        AssertionError: The read was refused, which is the subject of another
            case rather than of this call.
    """
    response = await client.get(f"/projects/{project_id}/members", headers=headers)
    assert response.status_code == 200, response.text
    listed: list[dict[str, Any]] = response.json()
    return listed


async def grant(
    client: httpx.AsyncClient,
    project_id: str,
    headers: dict[str, str],
    username: str,
    role: UserRole,
) -> httpx.Response:
    """Ask to add somebody, and return the answer unasserted.

    Unasserted because half the cases here are about the refusal, and a helper
    that insisted on 201 would make the other half unreadable.
    """
    return await client.post(
        f"/projects/{project_id}/members",
        headers=headers,
        json={"username": username, "role": role.value},
    )


async def revoke(
    client: httpx.AsyncClient, project_id: str, headers: dict[str, str], user_id: str
) -> httpx.Response:
    """Ask to withdraw somebody's role, and return the answer unasserted."""
    return await client.post(
        f"/projects/{project_id}/members/{user_id}/revoke", headers=headers
    )


# ── Reading the authoritative state ─────────────────────────────────────────


def user_id_of(database: Database, username: str) -> str:
    """The identifier behind a username, read the way a request finds them."""
    with database.read_only() as session:
        found = UserRepository(session).by_username(username)
    assert found is not None, f"no account named {username!r}"
    return found.user_id


def accounts(database: Database) -> set[str]:
    """Every account in the database, by identifier."""
    with database.read_only() as session:
        return {row.user_id for row in session.query(UserRow).all()}


def live_in(database: Database, project_id: str) -> list[ProjectMembership]:
    """The memberships in force in a project, read from PostgreSQL."""
    with database.read_only() as session:
        return MembershipRepository(session, project_id).active()


def owners_of(database: Database, project_id: str) -> list[ProjectMembership]:
    """The project's live owners, read from PostgreSQL."""
    with database.read_only() as session:
        return MembershipRepository(session, project_id).owners()


def held_by(database: Database, project_id: str, user_id: str) -> ProjectMembership:
    """The live membership one user holds in one project.

    Raises:
        AssertionError: They hold none, which is a fact the case asserting it
            wants to see as a failure rather than as a `None`.
    """
    with database.read_only() as session:
        found = MembershipRepository(session, project_id).for_user(user_id)
    assert found is not None, f"{user_id} holds no live membership in {project_id}"
    return found


def stream_of(database: Database, project_id: str) -> list[ProjectEvent]:
    """A project's whole event stream, oldest first."""
    with database.read_only() as session:
        return list(events_since(session, project_id))


def membership_events(database: Database, project_id: str) -> list[ProjectEvent]:
    """Every grant and withdrawal in a project, in the order they happened."""
    return [
        event
        for event in stream_of(database, project_id)
        if event.event_type
        in (ProjectEventType.MEMBER_ADDED, ProjectEventType.MEMBER_REVOKED)
    ]


# ── A person is made at a terminal ──────────────────────────────────────────


async def test_p11_08_a_person_is_made_at_a_terminal_and_not_over_http(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """The first line of the item, and the claim underneath it.

    RAVEL's Gateway authenticates a person and cannot make one. So the account
    exists because an operator ran the script on the host, and the two halves
    are asserted against each other: the script's account can log in to the
    running Gateway, and no route of that Gateway can make another. The probe is
    made with the strongest credential in the system — an owner's token — and is
    read back out of PostgreSQL afterwards rather than trusted to a status code,
    because the question is whether a row appeared and not what a route said.

    The second run of the script is the other half of the same rule: a username
    is spent once, so an operator cannot answer "make me another one" by
    overwriting somebody who already exists.
    """
    an_owner(operator, "ada")
    ada = user_id_of(database, "ada")

    with database.read_only() as session:
        stored = UserRepository(session).get(ada)
    assert stored.password_hash is not None
    assert stored.password_hash.startswith("$argon2id$"), (
        "the account was stored with something other than the Argon2id hash the "
        f"operator's script is documented to use: {stored.password_hash[:16]!r}"
    )

    async with a_client(live_gateway) as client:
        headers = await signed_in(client, "ada")
        project_id = await only_project(client, headers)

        assert (
            operator(["--username", "ada", "--password", PASSWORD, "--no-membership"])
            == 1
        ), "the operator made a second account under a username already in use"

        before = accounts(database)
        routes = effective_routes(live_gateway.app)
        assert len(routes) >= EXPECTED_ROUTE_FLOOR, (
            f"enumerated only {len(routes)} routes, which means the walk is broken "
            "and this case is about to assert nothing"
        )

        probed: list[str] = []
        for route in routes:
            if not route.is_http or "POST" not in route.methods:
                continue
            path = probe_path(
                route.path,
                project_id=project_id,
                # Absent on purpose: the project is real because membership is
                # checked before a route body runs, and everything a body would
                # address is not, so a route that writes aims at something that
                # refuses. A probe that named real ones would be measuring what
                # those routes do rather than what this case is about.
                node_id="a-node-that-does-not-exist",
                task_id="a-task-that-does-not-exist",
            )
            for sent in (headers, {}):
                await client.request("POST", path, headers=sent, json={})
            probed.append(f"POST {path}")

        assert any(path.endswith("/members") for path in probed), probed
        assert accounts(database) == before, (
            "a route created an account; making a person is an operator's, at a "
            "terminal, and there is no open registration"
        )

    assert [membership.user_id for membership in live_in(database, project_id)] == [
        ada
    ], "the probe withdrew the owner it was made with"


# ── An owner opens a project ────────────────────────────────────────────────


async def test_p11_08_an_owner_opens_a_project_and_the_ownership_is_a_row(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """Opening a project is how a person becomes an owner, and it is a fact in
    PostgreSQL rather than a claim in a response body.

    The account is made with no membership at all, so the state before the route
    runs is a person who owns nothing — which is what makes the assertion after
    it about this request rather than about the fixture. The creator is the
    project's first membership, and the record says it was granted to nobody by
    nobody: there was no granter, and naming one would be inventing a party who
    was never there.
    """
    an_account(operator, "ada")
    ada = user_id_of(database, "ada")

    async with a_client(live_gateway) as client:
        headers = await signed_in(client, "ada")

        assert (await client.get("/projects", headers=headers)).json() == [], (
            "an account that owns nothing was already in a project"
        )

        created = await opened(client, headers)
        project_id = created["project_id"]
        assert created["role"] == UserRole.PROJECT_OWNER.value

        (owner,) = live_in(database, project_id)
        assert (owner.user_id, owner.role) == (ada, UserRole.PROJECT_OWNER)
        assert owner.granted_by is None
        assert owner.granted_at is not None

        listed = (await client.get("/projects", headers=headers)).json()
        assert [(entry["project_id"], entry["role"]) for entry in listed] == [
            (project_id, UserRole.PROJECT_OWNER.value)
        ]

        members = await members_of(client, project_id, headers)
        assert [(member["username"], member["role"]) for member in members] == [
            ("ada", UserRole.PROJECT_OWNER.value)
        ]

    events = stream_of(database, project_id)
    assert [event.event_type for event in events[:2]] == [
        ProjectEventType.PROJECT_CREATED,
        ProjectEventType.MEMBER_ADDED,
    ], "a project came into being without the record of who owns it"
    assert events[0].actor_id == ada, "the record does not say who opened it"


# ── An owner adds somebody who already exists ───────────────────────────────


async def test_p11_08_an_owner_adds_accounts_that_already_exist(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """Both roles the item names, and the account that does not exist.

    A lab user is the person who will run an experiment and an administrator is
    the operator of the runtime; an owner confers both. The refusal at the end is
    the boundary between conferring a role and creating a person: naming somebody
    who has no account is answered as an absence, and it is asserted by counting
    the accounts rather than by the status alone, because "no such account" and
    "and none was made" are two different claims.
    """
    an_owner(operator, "ada")
    an_account(operator, "ben")
    an_account(operator, "root")

    async with a_client(live_gateway) as client:
        headers = await signed_in(client, "ada")
        project_id = await only_project(client, headers)

        for name, role in (("ben", UserRole.LAB_USER), ("root", UserRole.ADMIN)):
            response = await grant(client, project_id, headers, name, role)
            assert response.status_code == 201, response.text
            assert response.json()["granted_by"] == "ada", (
                "the record of a conferred role does not name who conferred it"
            )

        members = await members_of(client, project_id, headers)
        assert [(member["username"], member["role"]) for member in members] == [
            ("ada", UserRole.PROJECT_OWNER.value),
            ("ben", UserRole.LAB_USER.value),
            ("root", UserRole.ADMIN.value),
        ]

        before = accounts(database)
        missing = await grant(client, project_id, headers, "mallory", UserRole.LAB_USER)
        assert missing.status_code == 404, missing.text
        assert "mallory" in missing.text, (
            "the refusal does not name the account nobody has, so an owner cannot "
            f"tell what to fix: {missing.text}"
        )
        assert accounts(database) == before, (
            "adding a member created an account; RAVEL's Gateway can authenticate "
            "a person and cannot make one"
        )

    assert [(member.user_id, member.role) for member in live_in(database, project_id)] == [
        (user_id_of(database, "ada"), UserRole.PROJECT_OWNER),
        (user_id_of(database, "ben"), UserRole.LAB_USER),
        (user_id_of(database, "root"), UserRole.ADMIN),
    ]


# ── Who may not ─────────────────────────────────────────────────────────────


async def test_p11_08_a_lab_user_and_an_administrator_cannot_manage_members(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """The two refusals, told apart from the third they are not.

    Managing members is the owner's alone. A lab user and an administrator are
    both refused, and the refusal is 403 rather than 404 because each of them is
    *in* this project and the project is not a secret from them — what they may
    not do is decide who else joins it. So each case reads the project first,
    which is the thing they may do, and only then fails to write a membership.

    The reads afterwards are what make the refusals mean something: five
    refusals are worth nothing if one of them quietly went through, and a
    membership list read from PostgreSQL is what says none did.
    """
    an_owner(operator, "ada")
    an_account(operator, "ben")
    an_account(operator, "root")

    async with a_client(live_gateway) as client:
        owners = await signed_in(client, "ada")
        project_id = await only_project(client, owners)
        for name, role in (("ben", UserRole.LAB_USER), ("root", UserRole.ADMIN)):
            assert (
                await grant(client, project_id, owners, name, role)
            ).status_code == 201

        ada = user_id_of(database, "ada")
        for name in ("ben", "root"):
            headers = await signed_in(client, name)
            readable = await client.get(f"/projects/{project_id}", headers=headers)
            assert readable.status_code == 200, (
                f"{name} cannot even read the project they were added to, so the "
                f"refusals below would say nothing about managing it: {readable.text}"
            )

            listed = await client.get(f"/projects/{project_id}/members", headers=headers)
            assert listed.status_code == 403, listed.text

            added = await grant(client, project_id, headers, "ada", UserRole.ADMIN)
            assert added.status_code == 403, added.text

            withdrawn = await revoke(client, project_id, headers, ada)
            assert withdrawn.status_code == 403, withdrawn.text

        members = await members_of(client, project_id, owners)
        assert [member["username"] for member in members] == ["ada", "ben", "root"]

    assert len(live_in(database, project_id)) == 3, (
        "one of the refusals took effect, or the owner's own membership moved"
    )
    assert held_by(database, project_id, ada).role is UserRole.PROJECT_OWNER


# ── An administrator is not a platform superuser ────────────────────────────


async def test_p11_08_an_administrator_is_not_a_platform_superuser(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """Administering the runtime is a role *in a project*, and it opens one.

    The item's distinction is between an operational role and a platform
    superuser, and the way to assert it is to give one person the role in one
    project and ask about another. The answer is the answer a stranger gets —
    the same status and the same sentence as for a project that was never
    created — because the alternative confirms that somebody else's research
    exists.

    The last assertion is the domain's statement of the same rule, read from the
    row rather than from a document: a membership that carries `ADMIN` does not
    direct the project it is in. That is what keeps the scientific route out of
    an administrator's reach, and it is a property of the membership rather than
    of a route somebody remembered to guard.
    """
    an_owner(operator, "ada")
    an_owner(operator, "carol", title="A project root is not in")
    an_account(operator, "root")

    async with a_client(live_gateway) as client:
        owners = await signed_in(client, "ada")
        mine = await only_project(client, owners)
        theirs = await only_project(client, await signed_in(client, "carol"))
        assert (await grant(client, mine, owners, "root", UserRole.ADMIN)).status_code == 201

        headers = await signed_in(client, "root")
        listed = (await client.get("/projects", headers=headers)).json()
        assert [entry["project_id"] for entry in listed] == [mine], (
            "an administrator was handed a project they hold no membership in"
        )
        assert (await client.get(f"/projects/{mine}", headers=headers)).status_code == 200

        foreign = await client.get(f"/projects/{theirs}", headers=headers)
        missing = await client.get("/projects/no-such-project", headers=headers)
        assert foreign.status_code == missing.status_code == 404
        assert foreign.text.replace(theirs, "") == missing.text.replace(
            "no-such-project", ""
        ), "the refusal for somebody else's project says more than the refusal for none"
        assert (
            await client.get(f"/projects/{theirs}/dag", headers=headers)
        ).status_code == 404
        assert (
            await client.get(f"/projects/{theirs}/members", headers=headers)
        ).status_code == 404

    root_here = held_by(database, mine, user_id_of(database, "root"))
    assert root_here.role is UserRole.ADMIN
    assert root_here.may_direct_project is False, (
        "an administrative membership directs the project it is in, which is the "
        "separation of powers this item is about"
    )
    assert [member.role for member in live_in(database, theirs)] == [
        UserRole.PROJECT_OWNER
    ], "being asked about a project root is not in was enough to gain a membership in it"


# ── A project keeps an owner ────────────────────────────────────────────────


async def test_p11_08_a_project_never_loses_its_owner(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """不能删除最后一个 PROJECT_OWNER, and what it takes to step down legally.

    The refusal is asserted first, on the request that would produce a project
    nobody can direct — and it is answered 409 rather than 403 because the caller
    *may* withdraw a membership; what they may not do is withdraw this one, which
    is a conflict with a fact rather than a matter of authority.

    Then the legal version of the same act, so the refusal is shown to be about
    the last owner and not about an owner stepping down at all: with a second
    owner in place the first one leaves, the project still has an owner, and the
    new owner is refused the same way when it is their turn. A rule that only
    ever said no would pass the first half of this.
    """
    an_owner(operator, "ada")
    an_account(operator, "carol")

    async with a_client(live_gateway) as client:
        ada_headers = await signed_in(client, "ada")
        project_id = await only_project(client, ada_headers)
        ada = user_id_of(database, "ada")

        refused = await revoke(client, project_id, ada_headers, ada)
        assert refused.status_code == 409, refused.text
        assert "last owner" in refused.text, (
            f"the refusal does not say what it is refusing to do: {refused.text}"
        )
        assert (
            await client.get(f"/projects/{project_id}", headers=ada_headers)
        ).status_code == 200

        granted = await grant(
            client, project_id, ada_headers, "carol", UserRole.PROJECT_OWNER
        )
        assert granted.status_code == 201, granted.text
        assert (await revoke(client, project_id, ada_headers, ada)).status_code == 200

        carol_headers = await signed_in(client, "carol")
        assert [entry["project_id"] for entry in (
            await client.get("/projects", headers=carol_headers)
        ).json()] == [project_id]
        last = await revoke(
            client, project_id, carol_headers, user_id_of(database, "carol")
        )
        assert last.status_code == 409, last.text

    assert [member.user_id for member in owners_of(database, project_id)] == [
        user_id_of(database, "carol")
    ], "the project was left without an owner, or with the one who stepped down"


# ── Authority is a row, not a claim ─────────────────────────────────────────


async def test_p11_08_authority_comes_from_the_row_and_not_the_token(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """role 必须从 DB re-read; 不依赖旧 token 中缓存权限.

    One token, held across the whole case, through three different states of the
    same person's membership. That is the only way to assert this: a case that
    signed in again after each change would pass whether the role came from the
    row or from a claim frozen at login.

    A withdrawal takes the authority away on the next request — and not the
    credential, which is why `/auth/me` is asserted in the middle of it. The
    account is still the account; what it may do is a row that is now revoked.
    Re-granting the same person a different role then changes what the same token
    reaches, in both directions: it can read the project again as an
    administrator, and it still cannot manage members, because an administrator
    could not before either.
    """
    an_owner(operator, "ada")
    an_account(operator, "ben")
    ben = user_id_of(database, "ben")

    async with a_client(live_gateway) as client:
        owners = await signed_in(client, "ada")
        project_id = await only_project(client, owners)
        assert (
            await grant(client, project_id, owners, "ben", UserRole.LAB_USER)
        ).status_code == 201

        # The token a lab user would already be holding while they work.
        headers = await signed_in(client, "ben")
        seen = await client.get(f"/projects/{project_id}", headers=headers)
        assert (seen.status_code, seen.json()["role"]) == (200, "LAB_USER")
        assert (
            await client.get(f"/projects/{project_id}/members", headers=headers)
        ).status_code == 403

        assert (await revoke(client, project_id, owners, ben)).status_code == 200

        gone = await client.get(f"/projects/{project_id}", headers=headers)
        assert gone.status_code == 404, (
            "a withdrawn membership still reached the project on a token issued "
            f"before the withdrawal: {gone.text}"
        )
        assert (await client.get("/projects", headers=headers)).json() == [], (
            "a project whose membership was withdrawn is still offered to the "
            "client that would open it"
        )
        assert (
            await client.get(f"/projects/{project_id}/dag", headers=headers)
        ).status_code == 404

        # The credential survives the authority, and the two routes that read
        # it are asserted together: `/auth/me` still answers and names the same
        # person, with no memberships left to open a screen on.
        me = await client.get("/auth/me", headers=headers)
        assert me.status_code == 200, "the withdrawal cancelled the credential itself"
        assert me.json() == {
            "user_id": ben,
            "username": "ben",
            "memberships": [],
        }

        assert (
            await grant(client, project_id, owners, "ben", UserRole.ADMIN)
        ).status_code == 201
        again = await client.get(f"/projects/{project_id}", headers=headers)
        assert (again.status_code, again.json()["role"]) == (200, "ADMIN"), (
            "the same token did not pick up the role the row now holds"
        )
        assert (
            await client.get(f"/projects/{project_id}/members", headers=headers)
        ).status_code == 403


# ── The record ──────────────────────────────────────────────────────────────


async def test_p11_08_every_membership_change_is_a_fact_on_the_record(
    database: Database,
    live_gateway: LiveGateway,
    operator: Callable[[list[str]], int],
) -> None:
    """membership mutation 必须 audit, and a withdrawal is history rather than
    deletion.

    The row a withdrawn membership leaves behind is the whole point: deleting it
    would make a role that was taken away indistinguishable from one that was
    never granted, and "who could direct this project in March" is a question an
    inquiry into a decision starts from. So the case asserts both halves — the
    stream, which says what happened and who made it happen, and the table, which
    still holds the row with the actor and the time it ended.
    """
    an_owner(operator, "ada")
    an_account(operator, "ben")
    ada, ben = user_id_of(database, "ada"), user_id_of(database, "ben")

    async with a_client(live_gateway) as client:
        owners = await signed_in(client, "ada")
        project_id = await only_project(client, owners)
        assert (
            await grant(client, project_id, owners, "ben", UserRole.LAB_USER)
        ).status_code == 201
        live = await members_of(client, project_id, owners)
        assert [member["revoked_at"] for member in live] == [None, None], (
            "a live membership answered with a withdrawal time"
        )

        withdrawn = await revoke(client, project_id, owners, ben)
        assert withdrawn.status_code == 200, withdrawn.text
        assert withdrawn.json()["revoked_at"] is not None, (
            "the answer to a withdrawal is indistinguishable from the answer to "
            "a grant, so a client cannot tell what it just did"
        )

    changes = membership_events(database, project_id)
    assert [event.event_type for event in changes] == [
        ProjectEventType.MEMBER_ADDED,
        ProjectEventType.MEMBER_ADDED,
        ProjectEventType.MEMBER_REVOKED,
    ], "a grant or a withdrawal happened without being recorded"
    assert changes[0].payload["user_id"] == ada
    assert changes[0].actor_type is ActorType.SYSTEM, (
        "the first membership was granted to nobody by nobody, and the record no "
        "longer says so"
    )
    assert [(event.actor_id, event.payload["user_id"]) for event in changes[1:]] == [
        (ada, ben),
        (ada, ben),
    ], "the record does not say who conferred the role and who took it away"
    assert changes[1].payload["membership_id"] == changes[2].payload["membership_id"], (
        "the withdrawal is not about the membership that was granted"
    )
    assert changes[2].payload["role"] == UserRole.LAB_USER.value

    with database.read_only() as session:
        history = MembershipRepository(session, project_id).history_for_user(ben)
    assert len(history) == 1, "the withdrawn membership was deleted, or was split in two"
    assert history[0].revoked_by == ada
    assert history[0].revoked_at is not None
    assert history[0].granted_by == ada
    assert history[0].is_active is False
    assert [member.user_id for member in live_in(database, project_id)] == [ada], (
        "a withdrawn membership is still listed among the live ones"
    )
