"""Passwords and access tokens: the two things a Gateway hands a client.

No database here. What is asserted is that a password hash is not a password,
that a signed token cannot be edited or re-signed with another key, and that
the one configuration that would make every token forgeable is refused.

Expiry is tested by minting a token that is already past it, not by waiting and
not by moving a clock: PyJWT's only clock hook is deprecated and ignored, so a
test that appeared to move time would leave the real one in place.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from ravel.gateway.auth.passwords import hash_password, needs_rehash, verify_password
from ravel.gateway.auth.tokens import (
    ALGORITHM,
    DEV_SECRET,
    MIN_SECRET_BYTES,
    InvalidAccessToken,
    TokenService,
    require_a_real_secret,
)

#: Long enough to be an HS256 key rather than merely a string. PyJWT warns
#: below `MIN_SECRET_BYTES`, and the warning is right.
SECRET = "a-secret-that-is-not-the-default-and-is-long-enough"
TTL = 900


@pytest.fixture
def service() -> TokenService:
    return TokenService(secret=SECRET, ttl_seconds=TTL)


def _expired(service: TokenService) -> str:
    """A token that is genuinely past its expiry, signed by a real key.

    Minted in the past rather than aged: PyJWT checks `exp` against the wall
    clock and ignores its own deprecated `current_time`, so a test cannot move
    the clock. Issuing from a moment far enough back is the same fact stated
    the other way round — and it is the real verifier doing the refusing.
    """
    encoded, _ = service.issue_access("user-1", now=datetime.now(UTC) - timedelta(seconds=TTL * 2))
    return encoded


# ── Passwords ───────────────────────────────────────────────────────────────


def test_a_hash_is_not_the_password_and_does_not_repeat() -> None:
    """Two hashes of one password differ, so the salt is doing its job.

    Identical hashes would mean no salt, and a table of them would be a table a
    rainbow attack reads directly.
    """
    first = hash_password("correct horse battery staple")
    second = hash_password("correct horse battery staple")

    assert "correct horse battery staple" not in first
    assert first != second
    assert first.startswith("$argon2id$")


@pytest.mark.parametrize(
    ("stored", "presented", "expected"),
    [
        ("correct horse battery staple", "correct horse battery staple", True),
        ("correct horse battery staple", "Correct horse battery staple", False),
        ("correct horse battery staple", "", False),
    ],
)
def test_a_password_verifies_only_against_itself(
    stored: str, presented: str, expected: bool
) -> None:
    assert verify_password(hash_password(stored), presented) is expected


def test_an_empty_password_is_refused_rather_than_hashed() -> None:
    """An empty password is a missing one, and the two must not be one row."""
    with pytest.raises(ValueError, match="cannot be empty"):
        hash_password("")


def test_a_hash_that_is_not_a_hash_verifies_as_false() -> None:
    """A malformed stored hash is a wrong password, not a crash.

    A login screen that raised here would answer differently for an account
    whose hash is broken, which is a fact about the account that a caller has
    no business learning.
    """
    assert verify_password("not-a-hash-at-all", "anything") is False


def test_a_current_hash_does_not_ask_to_be_remade() -> None:
    """The check that decides whether to rehash after a login."""
    assert needs_rehash(hash_password("anything")) is False


# ── Access tokens ───────────────────────────────────────────────────────────


def test_a_token_round_trips_and_carries_the_user(service: TokenService) -> None:
    """What the caller is told and what the token says are the same thing.

    Including the sub-second part: a JWT timestamp has no fractional part, so
    `issue_access` truncates rather than reporting a time the token does not
    carry.
    """
    issued_at = datetime.now(UTC)
    encoded, issued = service.issue_access("user-1", now=issued_at)

    verified = service.verify_access(encoded)

    assert verified.user_id == "user-1"
    assert verified.token_id == issued.token_id
    assert verified.issued_at == issued.issued_at == issued_at.replace(microsecond=0)
    assert verified.expires_at == issued.expires_at
    assert verified.expires_at == verified.issued_at + timedelta(seconds=TTL)


def test_an_expired_token_is_refused(service: TokenService) -> None:
    """An access token is a session with an end, and the end is enforced."""
    with pytest.raises(InvalidAccessToken, match="ExpiredSignature"):
        service.verify_access(_expired(service))


def test_a_token_issued_in_the_future_is_refused(service: TokenService) -> None:
    """`iat` is checked too, so a token cannot be minted ahead of its use.

    Worth stating because it is the reason `verify_access` takes no clock: a
    token issued from a future `now` fails against the real one, which is how
    this was noticed.
    """
    ahead, _ = service.issue_access("user-1", now=datetime.now(UTC) + timedelta(hours=1))

    with pytest.raises(InvalidAccessToken, match="ImmatureSignature"):
        service.verify_access(ahead)


def test_a_token_signed_with_another_key_is_refused(service: TokenService) -> None:
    """The signature is the whole of what makes a token a credential."""
    forged = jwt.encode(
        {
            "sub": "user-1",
            "jti": "made-up",
            "typ": "access",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        "a-different-secret-but-still-a-long-one",
        algorithm=ALGORITHM,
    )

    with pytest.raises(InvalidAccessToken):
        service.verify_access(forged)


def test_an_unsigned_token_is_refused(service: TokenService) -> None:
    """`alg: none` is the oldest break in the family, and the decoder fixes `alg`.

    The header is not consulted, so a token that asks to be verified by nobody
    is verified by HS256 against the real key and fails.
    """
    unsigned = jwt.encode(
        {
            "sub": "user-1",
            "jti": "made-up",
            "typ": "access",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(hours=1)).timestamp()),
        },
        key="",
        algorithm="none",
    )

    with pytest.raises(InvalidAccessToken):
        service.verify_access(unsigned)


def test_a_token_without_an_expiry_is_refused(service: TokenService) -> None:
    """An access token that never expires is a session nobody can end.

    `require` is what makes the claim mandatory rather than merely honoured
    when present, which is the difference between a rule and a convention.
    """
    eternal = jwt.encode(
        {
            "sub": "user-1",
            "jti": "made-up",
            "typ": "access",
            "iat": int(datetime.now(UTC).timestamp()),
        },
        SECRET,
        algorithm=ALGORITHM,
    )

    with pytest.raises(InvalidAccessToken):
        service.verify_access(eternal)


def test_nonsense_is_refused_rather_than_raising_something_else(
    service: TokenService,
) -> None:
    """Every failure leaves as one exception type, so no caller has to guess."""
    for token in ("", "not.a.token", "a.b.c"):
        with pytest.raises(InvalidAccessToken):
            service.verify_access(token)


def test_a_service_with_no_secret_is_refused() -> None:
    """A signing key of nothing signs nothing. Caught where it is constructed."""
    with pytest.raises(ValueError, match="signing secret"):
        TokenService(secret="", ttl_seconds=900)


# ── Refresh material ────────────────────────────────────────────────────────


def test_refresh_secrets_do_not_repeat_and_are_not_stored_as_themselves() -> None:
    secrets_ = {TokenService.new_refresh_secret() for _ in range(50)}
    assert len(secrets_) == 50

    digest = TokenService.refresh_hash("a-secret")
    assert digest != "a-secret"
    assert len(digest) == 64
    assert TokenService.refresh_hash("a-secret") == digest, "the lookup has to be stable"
    assert TokenService.refresh_hash("a-secret ") != digest


# ── The one deployment that must not start ──────────────────────────────────


@pytest.mark.parametrize("environment", ["development", "test"])
def test_the_shipped_secret_is_allowed_where_the_gateway_is_on_loopback(
    environment: str,
) -> None:
    require_a_real_secret(DEV_SECRET, environment=environment)


@pytest.mark.parametrize("secret", [DEV_SECRET, ""])
def test_the_shipped_secret_is_refused_in_production(secret: str) -> None:
    """A key in the repository is a public key, whatever it is called.

    Anyone who can read the source could otherwise mint a token naming any
    user, and nothing else in the authentication path would notice.
    """
    with pytest.raises(ValueError, match="public"):
        require_a_real_secret(secret, environment="production")


def test_a_short_secret_is_refused_in_production() -> None:
    """An HS256 key below 256 bits is the cheapest thing to attack.

    Refused rather than warned about: PyJWT does warn, and a warning in a log
    nobody reads is not a control. The length is measured in bytes, since a
    passphrase of fewer characters can still be long enough in UTF-8.
    """
    short = "s" * (MIN_SECRET_BYTES - 1)
    with pytest.raises(ValueError, match="at least"):
        require_a_real_secret(short, environment="production")


def test_a_short_secret_is_allowed_in_test() -> None:
    """The suite mints tokens for users it invents, on a loopback Gateway.

    Requiring a deployment-grade key to run a test would push one into the
    repository, which is the thing the production check exists to prevent.
    """
    require_a_real_secret("short", environment="test")


@pytest.mark.parametrize("length", [MIN_SECRET_BYTES, MIN_SECRET_BYTES + 8])
def test_a_long_enough_secret_is_accepted_in_production(length: int) -> None:
    require_a_real_secret("s" * length, environment="production")
