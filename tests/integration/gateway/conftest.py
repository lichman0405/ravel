"""Fixtures for tests that speak HTTP to the Gateway.

The application under test is built by the real `create_app`, against the real
test database, with a real token service. Nothing is doubled: the properties
these tests assert — that a token carries no authority, that a membership is
consulted on every request, that a refusal does not distinguish absent from
forbidden — are properties of the composition, and a route test that replaced
the composition would assert them about the replacement.

The secret is a genuine one rather than the development placeholder, so the
tests exercise the code path a deployment takes. `require_a_real_secret`
refuses that placeholder in production and would let it through here, which
would leave the check itself untested.

**One thing is scripted, and it is the boundary rather than a collaborator.**
`ScriptedDeliveries` stands in for the port that reaches a waiting run, so that
no route test opens a Temporal connection. What the routes owe at that boundary
is that everything is *recorded* before anything is delivered and that a
delivery which does not go is reported rather than raised — and both of those
are asserted here, against the scripted port. What the durable layer does with
a delivery is the subject of `tests/integration/temporal/`, where it runs
against a real server.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ravel.config import Settings
from ravel.domain.clock import utcnow
from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.execution.temporal.contracts import ExternalResult
from ravel.gateway.app import create_app
from ravel.gateway.auth.passwords import hash_password
from ravel.gateway.auth.tokens import TokenService
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.tables import UserRow

pytestmark = pytest.mark.integration

#: Long enough for HS256, and not the placeholder, so nothing here depends on
#: the development default being accepted.
SECRET = "a-gateway-secret-that-is-not-the-default-one"

#: One hour, matching the default. Named so a test that cares about expiry says
#: so by minting a token of its own rather than by changing this.
TTL_SECONDS = 3600

PASSWORD = "a password the tests know"


@pytest.fixture(autouse=True)
def _isolated(clean: None) -> None:
    """Start every case from an empty database.

    Autouse rather than pulled in by the fixtures that happen to need it.
    Usernames are unique, so a case that registered `ada` would fail the next
    one on a constraint rather than on what it was testing — and the failure
    would name the wrong thing entirely.
    """


@pytest.fixture
def gateway_settings(integration_settings: Settings) -> Settings:
    """The test settings, with a signing secret fit to be signed with.

    Set here rather than left at the development placeholder so that a test
    which builds the application without passing a token service exercises the
    path a deployment takes, instead of the one `require_a_real_secret` exists
    to refuse.
    """
    settings = integration_settings.model_copy(deep=True)
    settings.gateway_jwt_secret = SecretStr(SECRET)
    settings.gateway_token_ttl_seconds = TTL_SECONDS
    return settings


@pytest.fixture
def tokens() -> TokenService:
    """The token service the application and the tests both use.

    Shared rather than built twice so that a test can mint a token the
    application will accept, and — more usefully — can mint one it should
    *not* accept, by handing the service a different secret.
    """
    return TokenService(secret=SECRET, ttl_seconds=TTL_SECONDS)


class ScriptedDeliveries:
    """An `ExternalResultPort` that records what it was told, or refuses to be told.

    The Gateway's part of this boundary is to *record* and then hand the signal
    over; what the durable layer does with the signal is
    `tests/integration/temporal/`'s subject. So the port is scripted here, and
    for the reason the protocol itself gives: a lab user's upload has to be
    recorded whether or not a scheduler is reachable, and a suite that could not
    run without one would be unable to test the property it exists for.

    `failure` is how a test plays the case that is not rare — a run whose worker
    is gone. It is raised from `deliver`, which is where a transport failure
    would come from.
    """

    def __init__(self) -> None:
        #: Every delivery, in order: which node, under which contract version,
        #: carrying what. The version is asserted rather than the node alone
        #: because a node Master has revised runs again, and the run waiting is
        #: the one under the newest terms.
        self.deliveries: list[tuple[str, int, ExternalResult]] = []
        self.failure: Exception | None = None

    async def deliver(
        self, *, node_id: str, execution_contract_version: int, result: ExternalResult
    ) -> None:
        if self.failure is not None:
            raise self.failure
        self.deliveries.append((node_id, execution_contract_version, result))


@pytest.fixture
def deliveries() -> ScriptedDeliveries:
    """The scripted port, for a test that wants to read what was delivered."""
    return ScriptedDeliveries()


@pytest.fixture
def app(
    database: Database,
    gateway_settings: Settings,
    tokens: TokenService,
    deliveries: ScriptedDeliveries,
):
    """The Gateway, wired to the test database and to a scripted delivery port.

    The port is supplied rather than left to the default for every test in this
    directory, not only the laboratory ones: the default reaches Temporal, and a
    route test that opened a connection to the scheduler would be asserting
    about a service that is not what it is testing.
    """
    return create_app(
        settings=gateway_settings,
        database=database,
        tokens=tokens,
        deliver_external=deliveries,
    )


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    """An HTTP client for the Gateway.

    `TestClient` runs the application's own request handling, including its
    dependency resolution and exception handlers, so a status code asserted
    here is the status code a real client would see.
    """
    with TestClient(app) as open_client:
        yield open_client


# ── Accounts ────────────────────────────────────────────────────────────────


def account(
    database: Database,
    *,
    username: str,
    password: str = PASSWORD,
    role: UserRole | None = None,
    project: Project | None = None,
) -> str:
    """Create a user, optionally with a membership, and return their identifier.

    One helper for both because a user without a membership and a user with one
    differ by a single call, and tests that need both should not have to say so
    twice.
    """
    with database.transaction() as session:
        user = UserRepository(session).create(
            username=username, password_hash=hash_password(password)
        )
        if role is not None and project is not None:
            # Mirrors the domain's rule rather than working around it: a
            # project's *first* membership needs no granter, and every one after
            # it does. The `project` fixture has already spent its first on the
            # owner, so a second member names that owner — which is the point of
            # the rule, since authority is conferred rather than assumed. A
            # project built by `a_project` has no members yet, so its first
            # member is granted by nobody, as it must be.
            repository = MembershipRepository(session, project.project_id)
            repository.grant(
                user_id=user.user_id,
                role=role,
                granted_by=project.created_by if repository.all() else None,
            )
        return user.user_id


def a_project(database: Database, *, title: str) -> Project:
    """A project with no members, so a test can put somebody in it first.

    Not the `project` fixture, which comes with an owner already: a test about
    who may be in a project needs the moment before the first membership, and
    one about a project with a single owner needs a project that has one.
    """
    with database.transaction() as session:
        return ProjectRegistry(session).create(
            title=title, objective="Nothing in particular.", created_by="someone"
        )


def deactivated_account(database: Database, *, user_id: str, username: str) -> None:
    """Create an account in the deactivated state, returning nothing.

    A separate helper from `account` because it is a separate thing: `users` is
    append-only — it is absent from `UPDATABLE_TABLES` — so an account cannot
    be *edited* into this state, only created in it, and a caller that wanted
    `account(..., is_active=False)` would be asking for a mutation the schema
    forbids. That the table is append-only is deliberate: it makes an account's
    existence a fact about the past rather than a current opinion.

    V0 has no route that deactivates an account. This reaches a state the
    schema permits and the Gateway's guard must handle; it does not model a
    workflow that exists.
    """
    with database.transaction() as session:
        session.add(
            UserRow(
                user_id=user_id,
                username=username,
                email=None,
                password_hash=hash_password(PASSWORD),
                is_active=False,
                created_at=utcnow(),
            )
        )


def sign_in(client: TestClient, username: str, password: str = PASSWORD) -> dict[str, Any]:
    """Log in and return the token pair, as a client would.

    The values are heterogeneous — `expires_in` is a number and the rest are
    strings — so the mapping is typed loosely rather than pretending otherwise.

    Raises:
        AssertionError: The login was refused, which means the test's setup is
            wrong rather than the thing it is about to assert.
    """
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200, response.text
    pair: dict[str, Any] = response.json()
    return pair


def bearer(token: str) -> dict[str, str]:
    """The header a client sends."""
    return {"Authorization": f"Bearer {token}"}
