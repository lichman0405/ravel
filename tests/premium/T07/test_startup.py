"""What the process refuses to start with.

The shipped defaults are development conveniences: a signing key in this
repository, a database password in `docker-compose.yml`, a storage secret in
the same file. They are safe on a laptop and worthless on a CVM, so a
production process refuses them — before it opens a database, and even when a
caller has injected its own token service.

The same module checks the two numbers the task fixes (the stream's
reauthorization interval and the upload cap) in every environment, and the
origin parser that the socket's exact comparison depends on. The cases below
are the configuration surface, not the routes: every one asserts the process
fails to start rather than starting weakly.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from ravel.config import Settings
from ravel.gateway.app import create_app
from ravel.gateway.auth.policy import (
    exact_origins,
    normalize_origin,
    parse_origins,
    validate_gateway_settings,
)
from ravel.gateway.auth.tokens import TokenService
from ravel.state.database import Database

REAL_JWT_SECRET = "k" * 64
REAL_DATABASE_PASSWORD = "a database password that is not shipped"
REAL_STORAGE_SECRET = "an object store secret that is not shipped"


@pytest.fixture
def production(gateway_settings: Settings) -> Settings:
    """The test configuration, moved to production with deployment credentials.

    Every value a production process insists on is set here, so each case
    below changes exactly one thing and the refusal it produces is attributable
    to that change.
    """
    deployed = gateway_settings.model_copy(deep=True)
    deployed.env = "production"
    deployed.gateway_jwt_secret = SecretStr(REAL_JWT_SECRET)
    deployed.postgres_password = SecretStr(REAL_DATABASE_PASSWORD)
    deployed.postgres_dsn_override = None
    deployed.s3_secret_key = SecretStr(REAL_STORAGE_SECRET)
    deployed.gateway_host = "127.0.0.1"
    deployed.gateway_public_origin = "https://tui.ravel.test"
    deployed.gateway_allowed_origins = "https://tui.ravel.test,https://console.ravel.test"
    deployed.gateway_trusted_proxies = "127.0.0.1/32"
    return deployed


def test_a_production_configuration_with_deployment_credentials_is_accepted(
    production: Settings,
) -> None:
    """The refusal cases are only meaningful if the baseline is allowed."""
    validate_gateway_settings(production)


def test_a_public_bind_is_accepted_only_when_it_is_asked_for(production: Settings) -> None:
    """The escape hatch is explicit, which is what makes the default safe."""
    production.gateway_host = "0.0.0.0"
    with pytest.raises(ValueError, match="RAVEL_GATEWAY_ALLOW_PUBLIC_BIND"):
        validate_gateway_settings(production)

    production.gateway_allow_public_bind = True
    validate_gateway_settings(production)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "10.12.0.4", "192.168.1.9"])
def test_a_loopback_or_private_bind_is_accepted_in_production(
    production: Settings, host: str
) -> None:
    production.gateway_host = host
    validate_gateway_settings(production)


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "8.8.8.8", "gateway.example"])
def test_a_public_or_unresolvable_bind_is_refused_in_production(
    production: Settings, host: str
) -> None:
    production.gateway_host = host
    with pytest.raises(ValueError, match="RAVEL_GATEWAY_HOST"):
        validate_gateway_settings(production)


# ── The refusal table ──────────────────────────────────────────────────────


def _shipped_jwt(settings: Settings) -> None:
    settings.gateway_jwt_secret = SecretStr("dev-only-change-me")


def _short_jwt(settings: Settings) -> None:
    settings.gateway_jwt_secret = SecretStr("too short to be a signing key")


def _shipped_database(settings: Settings) -> None:
    settings.postgres_password = SecretStr("ravel_dev_password")


def _dsn_carrying_the_shipped_database(settings: Settings) -> None:
    settings.postgres_dsn_override = (
        "postgresql+psycopg://ravel:ravel_dev_password@127.0.0.1:55432/ravel"
    )


def _shipped_storage(settings: Settings) -> None:
    settings.s3_secret_key = SecretStr("ravel_minio_dev_password")


def _empty_storage(settings: Settings) -> None:
    settings.s3_secret_key = SecretStr("")


def _missing_public_origin(settings: Settings) -> None:
    settings.gateway_public_origin = None


def _plain_http_public_origin(settings: Settings) -> None:
    settings.gateway_public_origin = "http://tui.ravel.test"


def _public_origin_not_allowlisted(settings: Settings) -> None:
    settings.gateway_public_origin = "https://elsewhere.ravel.test"


def _plain_http_allowlist_entry(settings: Settings) -> None:
    settings.gateway_allowed_origins = (
        "https://tui.ravel.test,http://console.ravel.test"
    )


def _wildcard_allowlist(settings: Settings) -> None:
    settings.gateway_allowed_origins = "https://*.ravel.test"


def _allowlist_with_a_path(settings: Settings) -> None:
    settings.gateway_allowed_origins = "https://tui.ravel.test/app"


def _allowlist_with_credentials(settings: Settings) -> None:
    settings.gateway_allowed_origins = "https://user:secret@tui.ravel.test"


def _allowlist_with_an_empty_entry(settings: Settings) -> None:
    settings.gateway_allowed_origins = "https://tui.ravel.test,"


def _allowlist_with_a_non_numeric_port(settings: Settings) -> None:
    settings.gateway_allowed_origins = "https://tui.ravel.test:https"


def _empty_trusted_proxies(settings: Settings) -> None:
    settings.gateway_trusted_proxies = ""


@pytest.mark.parametrize(
    ("mutate", "fragment", "leaked"),
    [
        pytest.param(
            _shipped_jwt, "RAVEL_GATEWAY_JWT_SECRET", "dev-only-change-me", id="shipped-jwt"
        ),
        pytest.param(
            _short_jwt,
            "HS256 asks for at least 32",
            "too short to be a signing key",
            id="short-jwt",
        ),
        pytest.param(
            _shipped_database,
            "RAVEL_POSTGRES_PASSWORD",
            "ravel_dev_password",
            id="shipped-database",
        ),
        pytest.param(
            _dsn_carrying_the_shipped_database,
            "RAVEL_POSTGRES_DSN",
            "ravel_dev_password",
            id="dsn-with-shipped-database",
        ),
        pytest.param(
            _shipped_storage,
            "RAVEL_S3_SECRET_KEY",
            "ravel_minio_dev_password",
            id="shipped-storage",
        ),
        pytest.param(_empty_storage, "RAVEL_S3_SECRET_KEY", None, id="empty-storage"),
        pytest.param(
            _missing_public_origin,
            "RAVEL_GATEWAY_PUBLIC_ORIGIN is required",
            None,
            id="missing-public-origin",
        ),
        pytest.param(
            _plain_http_public_origin, "has to be an https origin", None, id="http-public-origin"
        ),
        pytest.param(
            _public_origin_not_allowlisted,
            "has to contain the value",
            None,
            id="origin-not-allowlisted",
        ),
        pytest.param(
            _plain_http_allowlist_entry,
            "may contain only https origins",
            None,
            id="http-allowlist",
        ),
        pytest.param(
            _wildcard_allowlist, "may not contain a wildcard", None, id="wildcard-allowlist"
        ),
        pytest.param(
            _allowlist_with_a_path, "has to be an origin", None, id="allowlist-with-path"
        ),
        pytest.param(
            _allowlist_with_credentials,
            "may not carry credentials",
            "user:secret",
            id="allowlist-with-credentials",
        ),
        pytest.param(
            _allowlist_with_an_empty_entry,
            "contains an empty origin",
            None,
            id="allowlist-empty-entry",
        ),
        pytest.param(
            _allowlist_with_a_non_numeric_port, "not a number", None, id="allowlist-bad-port"
        ),
        pytest.param(
            _empty_trusted_proxies,
            "RAVEL_GATEWAY_TRUSTED_PROXIES is required",
            None,
            id="empty-trusted-proxies",
        ),
    ],
)
def test_production_refuses_a_configuration_that_would_be_undefended(
    production: Settings,
    mutate: Any,
    fragment: str,
    leaked: str | None,
) -> None:
    """One refusal per hazard, naming the variable and never the credential.

    The `leaked` assertion is the second half of the requirement: an error a
    deployment prints must not become a way to read the secret it is
    complaining about.
    """
    mutate(production)
    with pytest.raises(ValueError) as refused:
        validate_gateway_settings(production)

    message = str(refused.value)
    assert fragment in message, message
    if leaked is not None:
        assert leaked not in message, message


def test_development_may_use_the_shipped_credentials(gateway_settings: Settings) -> None:
    """The refusals are a production rule, and deliberately so.

    `make up` on a laptop runs with the published defaults, and a test suite
    that had to invent a deployment credential would push it into the test
    file. The scope is asserted here so that a future change tightening or
    widening it is visible.
    """
    development = gateway_settings.model_copy(deep=True)
    development.env = "development"
    development.gateway_jwt_secret = SecretStr("dev-only-change-me")
    development.postgres_password = SecretStr("ravel_dev_password")
    development.s3_secret_key = SecretStr("ravel_minio_dev_password")
    validate_gateway_settings(development)


# ── The two numeric bounds, in every environment ───────────────────────────


@pytest.mark.parametrize("seconds", [0.0, -1.0, 5.0001, 30.0])
def test_a_stream_reauthorization_interval_over_five_seconds_is_refused(
    gateway_settings: Settings, seconds: float
) -> None:
    """A slower check would break the documented five-second revocation bound."""
    gateway_settings.gateway_stream_reauthorize_seconds = seconds
    with pytest.raises(ValueError, match="STREAM_REAUTHORIZE_SECONDS"):
        validate_gateway_settings(gateway_settings)


@pytest.mark.parametrize("seconds", [0.001, 1.0, 5.0])
def test_a_stream_reauthorization_interval_at_or_under_five_seconds_is_accepted(
    gateway_settings: Settings, seconds: float
) -> None:
    gateway_settings.gateway_stream_reauthorize_seconds = seconds
    validate_gateway_settings(gateway_settings)


@pytest.mark.parametrize("limit", [0, -1])
def test_a_non_positive_upload_limit_is_refused(
    gateway_settings: Settings, limit: int
) -> None:
    gateway_settings.gateway_upload_limit_bytes = limit
    with pytest.raises(ValueError, match="UPLOAD_LIMIT_BYTES"):
        validate_gateway_settings(gateway_settings)


# ── Origins ────────────────────────────────────────────────────────────────


def test_origins_are_normalized_to_the_form_a_browser_sends() -> None:
    """Case, a trailing slash, and a default port all denote one origin."""
    assert normalize_origin("https://Console.Ravel.Test:443/") == "https://console.ravel.test"
    assert normalize_origin("http://tui.example:80") == "http://tui.example"
    assert normalize_origin("https://tui.example:8443") == "https://tui.example:8443"


def test_an_origin_that_is_not_a_bare_origin_normalizes_to_nothing() -> None:
    """`None` is a refusal, not a wildcard: it can never match an allowlist."""
    for value in (
        "null",
        "",
        "https://tui.example/app",
        "https://tui.example?x=1",
        "https://tui.example#fragment",
        "ftp://tui.example",
        "https://user:secret@tui.example",
        "https://*.tui.example",
    ):
        assert normalize_origin(value) is None, value


def test_the_allowlist_is_an_exact_normalized_set(gateway_settings: Settings) -> None:
    gateway_settings.gateway_allowed_origins = (
        " https://TUI.ravel.test/ , https://console.ravel.test:443 "
    )
    assert exact_origins(gateway_settings) == frozenset(
        {"https://tui.ravel.test", "https://console.ravel.test"}
    )


def test_an_unconfigured_allowlist_is_empty_rather_than_everything() -> None:
    settings = Settings(env="test", gateway_allowed_origins="")
    assert exact_origins(settings) == frozenset()
    assert parse_origins("") == ()


def test_an_ipv6_origin_keeps_its_brackets() -> None:
    assert normalize_origin("https://[2001:db8::1]:8443/") == "https://[2001:db8::1]:8443"


# ── Composition ────────────────────────────────────────────────────────────


def test_create_app_refuses_before_it_opens_a_database(
    monkeypatch: pytest.MonkeyPatch, production: Settings
) -> None:
    """The check is not merely early in a docstring; it is before the call.

    `Database.from_settings` is made to fail loudly, so a future reordering
    that built the engine first would fail this test rather than a deployment.
    """

    def explode(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the database was opened before the settings were validated")

    monkeypatch.setattr(Database, "from_settings", classmethod(explode))
    production.gateway_upload_limit_bytes = 0

    with pytest.raises(ValueError, match="UPLOAD_LIMIT_BYTES"):
        create_app(settings=production)


def test_create_app_validates_even_when_a_token_service_is_injected(
    production: Settings,
) -> None:
    """An injected token service is not a way around the other production rules."""
    production.gateway_stream_reauthorize_seconds = 30.0
    tokens = TokenService(secret=REAL_JWT_SECRET, ttl_seconds=60)

    with pytest.raises(ValueError, match="STREAM_REAUTHORIZE_SECONDS"):
        create_app(settings=production, tokens=tokens)


def test_a_valid_production_configuration_builds_an_application_that_uses_it(
    production: Settings,
) -> None:
    """The accepted configuration reaches `app.state`, not just the validator.

    Built without a `database` argument, so the engine is created from the
    settings as a deployment would; no connection is opened until a request
    needs one, which is why this test needs no PostgreSQL.
    """
    app = create_app(settings=production)
    state = app.state.ravel
    assert state.allowed_origins == frozenset(
        {"https://tui.ravel.test", "https://console.ravel.test"}
    )
    assert state.stream_reauthorize_seconds == pytest.approx(
        production.gateway_stream_reauthorize_seconds
    )
    assert state.trusted_proxies.trusts("127.0.0.1") is True
    assert state.trusted_proxies.trusts("203.0.113.9") is False

    with TestClient(app) as http:
        # Liveness is the only thing an unauthenticated caller learns.
        assert http.get("/healthz").json() == {"status": "ok"}
