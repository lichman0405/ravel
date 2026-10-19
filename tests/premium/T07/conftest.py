"""Fixtures for the T07 boundary tests.

The Gateway under test is built by the real `create_app` against the real
PostgreSQL test database, for the reason the integration suite gives: the
properties these cases assert — that authority is read from a membership row on
every request, that a revocation reaches an open socket, that a refused upload
leaves no Artifact row — are properties of the composition, and a fake
composition would assert them about the fake.

Two things are supplied rather than real, and both are boundaries:

- The login limiter gets a clock the test moves, so a fifteen-minute lock is
  observed instead of waited out.
- The artifact store is a recorder, so a case can make a store refuse after it
  has consumed the upload, or hold the bytes long enough to prove they were
  sent unchanged, without requiring MinIO for assertions that are not about
  MinIO. The real `StreamingS3ArtifactStore` is exercised separately against a
  scripted S3 client.
"""

from __future__ import annotations

import hashlib
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
    _database_test_lock,
    clean,
    database,
    integration_settings,
    other_project,
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
from ravel.gateway.app import create_app
from ravel.gateway.auth.limits import LoginRateLimiter
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.stream import Cadence
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.store import StoredObject

pytestmark = pytest.mark.integration

#: Test-speed stream: events are picked up quickly and heartbeats keep a client
#: that is waiting for a close moving.
FAST = Cadence(poll_seconds=0.01, heartbeat_seconds=0.05)

#: Well under the five seconds the task allows, so a test that waits for a
#: revocation to reach an open stream does not spend the allowance doing it.
STREAM_REAUTHORIZE_SECONDS = 0.2

#: The upload cap these cases run under. Named rather than imported from the
#: route so that a deployment default moving is a visible change here instead of
#: a test that silently starts asserting a different number.
UPLOAD_LIMIT_BYTES = 64 * 1024 * 1024


class MutableClock:
    """Monotonic time a test advances by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def _isolated(clean: None) -> None:  # noqa: F811
    """Every case starts from an empty database, as in the integration suite."""


@pytest.fixture
def clock() -> MutableClock:
    return MutableClock()


@pytest.fixture
def limiter(clock: MutableClock) -> LoginRateLimiter:
    """The shipped policy, on a clock the test controls."""
    return LoginRateLimiter(now=clock)


@pytest.fixture
def gateway_settings(integration_settings: Settings) -> Settings:  # noqa: F811
    """A Gateway configuration that is fit for a test and strict where it counts.

    The browser sandbox provider stays unset, because T07's browser rule is
    fail-closed until T19 binds one and a fixture that quietly named a provider
    would make the gate untestable. The proxy is loopback because the
    `X-Forwarded-For` cases build a client whose peer address is loopback; the
    default TestClient peer name is not in the set, so every other case charges
    the login limit to the peer it actually has.
    """
    settings = integration_settings.model_copy(deep=True)
    settings.gateway_jwt_secret = SecretStr(SECRET)
    settings.gateway_token_ttl_seconds = TTL_SECONDS
    settings.gateway_allowed_origins = "https://tui.ravel.test,https://console.ravel.test"
    settings.gateway_trusted_proxies = "127.0.0.1/32"
    settings.gateway_stream_reauthorize_seconds = STREAM_REAUTHORIZE_SECONDS
    settings.gateway_upload_limit_bytes = UPLOAD_LIMIT_BYTES
    settings.gateway_public_origin = "https://tui.ravel.test"
    settings.research_contact_email = "research@ravel.test"
    settings.research_browser_sandbox_provider = None
    return settings


@pytest.fixture
def tokens() -> TokenService:
    return TokenService(secret=SECRET, ttl_seconds=TTL_SECONDS)


@pytest.fixture
def deliveries() -> ScriptedDeliveries:
    return ScriptedDeliveries()


@pytest.fixture
def app(
    database: Database,  # noqa: F811
    gateway_settings: Settings,
    tokens: TokenService,
    limiter: LoginRateLimiter,
    deliveries: ScriptedDeliveries,
) -> Any:
    """The real Gateway, at test speed and with a controllable limiter."""
    return create_app(
        settings=gateway_settings,
        database=database,
        tokens=tokens,
        deliver_external=deliveries,
        cadence=FAST,
        limiter=limiter,
        heartbeat_seconds=None,
    )


@pytest.fixture
def client(app: Any) -> Any:
    with TestClient(app) as open_client:
        yield open_client


def deactivate(
    database: Database,  # noqa: F811
    user_id: str,
    *,
    by: str = "operator",
    reason: str | None = None,
) -> None:
    """Stop an account the way an operator would: an audited row beside `users`."""
    with database.transaction() as session:
        UserRepository(session).deactivate(user_id, deactivated_by=by, reason=reason)


def revoke(
    database: Database,  # noqa: F811
    project: Any,  # noqa: F811
    user_id: str,
    *,
    by: str,
) -> None:
    """Withdraw a membership, naming the owner who did it."""
    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(user_id, revoked_by=by)


@dataclass
class RecordedUpload:
    """What a store was asked to keep, without necessarily keeping it.

    `body` is present only when the case asked for it. The sixty-five megabyte
    upload case does not: retaining the bytes there would replace the property
    under test — the Gateway's memory — with the test's own.
    """

    key: str
    content_hash: str
    size_bytes: int
    media_type: str
    body: bytes | None


@dataclass
class RecordingStore:
    """An `ArtifactStore` that records calls and can refuse at the last moment.

    The refusal is raised after the chunks have been consumed on purpose: the
    route's promise is that a store failure leaves no formal Artifact, and a
    store that refused before reading would not exercise the ordering that
    promise is about.
    """

    capture_bytes: bool = False
    failure: Exception | None = None
    uploads: list[RecordedUpload] = field(default_factory=list)

    def put(self, key: str, chunks: Any, *, media_type: str = "") -> StoredObject:
        digest = hashlib.sha256()
        size = 0
        kept = bytearray() if self.capture_bytes else None
        for chunk in chunks:
            digest.update(chunk)
            size += len(chunk)
            if kept is not None:
                kept.extend(chunk)
        content_hash = f"sha256:{digest.hexdigest()}"
        self.uploads.append(
            RecordedUpload(
                key=key,
                content_hash=content_hash,
                size_bytes=size,
                media_type=media_type,
                body=bytes(kept) if kept is not None else None,
            )
        )
        if self.failure is not None:
            raise self.failure
        return StoredObject(key=key, content_hash=content_hash, size_bytes=size)

    def get(self, key: str) -> bytes:  # pragma: no cover - not used by these cases
        raise AssertionError("the recorder does not serve reads")

    def open(self, key: str) -> Any:  # pragma: no cover - not used by these cases
        raise AssertionError("the recorder does not serve reads")

    def exists(self, key: str) -> bool:  # pragma: no cover - not used by these cases
        return any(upload.key == key for upload in self.uploads)


@pytest.fixture
def recording_store(monkeypatch: pytest.MonkeyPatch) -> RecordingStore:
    """Make every artifact upload in this case go to the recorder."""
    from ravel.gateway.routes import artifacts as artifact_routes

    store = RecordingStore()
    monkeypatch.setattr(artifact_routes, "artifact_store", lambda state: store)
    return store
