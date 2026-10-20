"""Fixtures for the T13 API and event-stream tests.

The Gateway under test is the real `create_app` against the real PostgreSQL test
database. The properties these cases assert — a per-project sequence that a
reconnect resumes without a gap, a revoked membership closing an open socket,
an old cursor being answered with a snapshot instead of silence, an OpenAPI
document with no DAG write in it — are properties of the composition plus the
database, and a fake of either would assert them about the fake.

Two things are supplied rather than real, and both are boundaries:

- Master is scripted, so a probe of the write routes cannot start a DSH runtime
  or reach a model. What the routes owe is the projection and the refusal, not
  the harness's answer.
- The external-delivery port is a recorder, for the reason
  `tests/integration/gateway/conftest.py` gives: no route test should need a
  Temporal server for an assertion that is not about Temporal.

The stream cadence and the reauthorization interval are lowered. Both are
timers, and a test cannot wait fifteen seconds for a heartbeat or four for a
revocation to be noticed; neither value changes what the route decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

# Re-exported on purpose: pytest registers an imported fixture under the name it
# has in this module, so these must keep their own names. The local fixtures and
# helpers below request them by those names, which is what F811 sees as a
# redefinition.
from tests.integration.conftest import (  # noqa: F401
    READ_AT,
    _database_test_lock,
    a_source,
    clean,
    database,
    integration_settings,
    other_project,
    prepare,
    project,
)
from tests.integration.gateway.conftest import (  # noqa: F401
    PASSWORD,
    SECRET,
    TTL_SECONDS,
    ScriptedDeliveries,
    a_project,
    account,
    bearer,
    deactivated_account,
    sign_in,
)

from ravel.config import Settings
from ravel.execution.loop import Situation
from ravel.gateway.app import create_app
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.conversation import Answer
from ravel.gateway.stream import Cadence
from ravel.state.database import Database

pytestmark = pytest.mark.integration

#: Test-speed stream: events are picked up quickly and heartbeats keep a client
#: that is waiting for a close or for silence moving.
FAST = Cadence(poll_seconds=0.01, heartbeat_seconds=0.05)

#: Well under the five seconds the task allows, so a case that waits for a
#: revocation to reach an open stream does not spend the allowance doing it.
STREAM_REAUTHORIZE_SECONDS = 0.2

#: The declared replay window these cases run under. Small enough that an
#: out-of-window cursor is cheap to produce, and larger than the events a
#: project fixture writes before a fresh stream opens.
RETENTION = 4


@dataclass
class ScriptedMaster:
    """A Master that answers without a model, and records what it was told."""

    reply: str = "Nothing has been planned yet."
    completed: bool = True
    seen: list[tuple[Situation, str]] = field(default_factory=list)

    async def respond(self, situation: Situation, message: str) -> Answer:
        self.seen.append((situation, message))
        return Answer(text=self.reply, completed=self.completed)


@pytest.fixture(autouse=True)
def _isolated(clean: None) -> None:  # noqa: F811
    """Every case starts from an empty database, as in the integration suite."""


@pytest.fixture
def gateway_settings(integration_settings: Settings) -> Settings:  # noqa: F811
    """The test settings, with a signing secret fit to be signed with.

    The reauthorization interval is lowered here rather than passed to the
    stream route, because it is a setting the deployment validates: a test that
    bypassed the validator would not be exercising the path a deployment takes.
    """
    settings = integration_settings.model_copy(deep=True)
    settings.gateway_jwt_secret = SecretStr(SECRET)
    settings.gateway_token_ttl_seconds = TTL_SECONDS
    settings.gateway_stream_reauthorize_seconds = STREAM_REAUTHORIZE_SECONDS
    settings.gateway_allowed_origins = "https://tui.ravel.test"
    settings.gateway_trusted_proxies = "127.0.0.1/32"
    return settings


@pytest.fixture
def tokens() -> TokenService:
    """The token service the application and the tests both use."""
    return TokenService(secret=SECRET, ttl_seconds=TTL_SECONDS)


@pytest.fixture
def deliveries() -> ScriptedDeliveries:
    return ScriptedDeliveries()


@pytest.fixture
def master() -> ScriptedMaster:
    return ScriptedMaster()


@pytest.fixture
def app(
    database: Database,  # noqa: F811
    gateway_settings: Settings,
    tokens: TokenService,
    deliveries: ScriptedDeliveries,
    master: ScriptedMaster,
) -> Any:
    """The real Gateway, at test speed and with a declared replay window.

    The window is declared so the resynchronization frame can be reached. It is
    the only policy argument passed; everything else is what a deployment runs.
    """
    return create_app(
        settings=gateway_settings,
        database=database,
        tokens=tokens,
        master_of=lambda _project_id: master,
        deliver_external=deliveries,
        cadence=FAST,
        heartbeat_seconds=None,
        event_retention=RETENTION,
    )


@pytest.fixture
def client(app: Any) -> Any:
    with TestClient(app) as open_client:
        yield open_client
