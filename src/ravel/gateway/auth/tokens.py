"""The two kinds of credential the Gateway hands out, and why they differ.

An **access token** is a signed JWT with a short life. It is checked without a
database read, which is the point of signing it: every request to the Gateway
carries one, and making each of those a query would put the session table on
the critical path of the whole API.

A **refresh token** is 256 bits of randomness, opaque, single-use, and stored
as a SHA-256 digest. It is presented rarely — when an access token expires —
so it can afford to cost a query, and being opaque means it carries no claims
that could go stale: what a refresh token is worth is decided from the
database at the moment it is exchanged, which is where membership lives.

The digest is SHA-256 rather than Argon2, and that is deliberate rather than a
shortcut. A password is low-entropy and guessable, so verifying one has to be
made expensive. A refresh token is 256 random bits; the search space is the
defence, and a slow digest would only make the Gateway slow. What a stored
digest buys is that a leaked database holds no usable token.
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from jwt.exceptions import InvalidTokenError

from ravel.domain.clock import utcnow

#: Fixed rather than read from the token. A decoder that honours the header's
#: `alg` will accept `none`, or an RS256 token verified with the public key as
#: an HMAC secret — the algorithm-confusion family, which is a whole class of
#: real breaks and costs one argument to avoid.
ALGORITHM = "HS256"

#: Distinguishes an access token from anything else that might one day be
#: signed with the same key. Refresh tokens are opaque today, so there is
#: nothing to confuse one with; the claim is here so that stays true if they
#: are ever made JWTs.
TOKEN_KIND = "access"

#: The development default in `config.Settings`. A Gateway that will not be
#: reached by anyone but its operator can run with it; one that can be reached
#: by anyone else must not, and `require_a_real_secret` is what says so.
DEV_SECRET = "dev-only-change-me"

#: 32 bytes is 256 bits, which is the size a token whose only defence is its
#: own unguessability has to be.
REFRESH_SECRET_BYTES = 32

#: RFC 7518 §3.2: an HMAC key for SHA-256 is at least 256 bits. Shorter is not
#: merely weaker — it makes the key the cheapest thing to attack about the
#: token, which is the one part of this design that is not meant to be.
MIN_SECRET_BYTES = 32


class InvalidAccessToken(PermissionError):
    """An access token that will not be honoured.

    Like `RefreshRefused`, the reason is for the log and not for the response:
    an expired token and a forged one are both "sign in again" to the client.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class AccessGrant:
    """What a verified access token says, as this system reads it."""

    user_id: str
    token_id: str
    issued_at: datetime
    expires_at: datetime


class TokenService:
    """Mints and verifies access tokens, and mints refresh secrets."""

    def __init__(self, *, secret: str, ttl_seconds: int) -> None:
        if not secret:
            raise ValueError("a token service requires a signing secret")
        if ttl_seconds < 1:
            raise ValueError("an access token must outlive its own issue")
        self._secret = secret
        self.ttl_seconds = ttl_seconds

    def issue_access(self, user_id: str, *, now: datetime | None = None) -> tuple[str, AccessGrant]:
        """Sign an access token for a user.

        Returns the encoded token and what it says, so a caller that wants to
        tell the user when their session ends does not have to decode the
        thing it just made.

        The issue time is truncated to a whole second because a JWT's `iat` and
        `exp` are NumericDates, which have no fractional part. Truncating here
        rather than letting `int()` do it below is what keeps the returned
        grant identical to what the token actually says — otherwise a caller
        told its session ends at `...:49.371776` would be holding a token that
        ends at `...:49`.
        """
        issued_at = (now or utcnow()).replace(microsecond=0)
        expires_at = issued_at + timedelta(seconds=self.ttl_seconds)
        token_id = secrets.token_urlsafe(16)
        claims = {
            "sub": user_id,
            "jti": token_id,
            "typ": TOKEN_KIND,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        encoded = jwt.encode(claims, self._secret, algorithm=ALGORITHM)
        return encoded, AccessGrant(
            user_id=user_id,
            token_id=token_id,
            issued_at=issued_at,
            expires_at=expires_at,
        )

    def verify_access(self, token: str) -> AccessGrant:
        """Read an access token, or refuse it.

        There is no injectable clock, and that is a decision rather than an
        omission. PyJWT validates `exp` and `iat` against the wall clock and
        offers only a deprecated `current_time` that this version accepts,
        warns about, and ignores — so a `now` parameter here would look like it
        moved the clock while silently leaving the real one in place, and a
        test written against it would pass for the wrong reason. Expiry is
        tested by issuing a token that is already expired instead, which the
        signature of `issue_access` supports.

        Raises:
            InvalidAccessToken: The signature does not verify, the token has
                expired, it is not yet valid, or it is not an access token.
        """
        try:
            claims = jwt.decode(
                token,
                self._secret,
                algorithms=[ALGORITHM],
                options={"require": ["exp", "iat", "sub", "jti", "typ"]},
            )
        except InvalidTokenError as error:
            raise InvalidAccessToken(f"{type(error).__name__}: {error}") from error

        if claims["typ"] != TOKEN_KIND:
            raise InvalidAccessToken(f"not a {TOKEN_KIND} token")
        return AccessGrant(
            user_id=claims["sub"],
            token_id=claims["jti"],
            issued_at=datetime.fromtimestamp(claims["iat"], tz=utcnow().tzinfo),
            expires_at=datetime.fromtimestamp(claims["exp"], tz=utcnow().tzinfo),
        )

    # ── Refresh material ────────────────────────────────────────────────────

    @staticmethod
    def new_refresh_secret() -> str:
        """A fresh refresh secret. The only copy is the one returned."""
        return secrets.token_urlsafe(REFRESH_SECRET_BYTES)

    @staticmethod
    def refresh_hash(secret: str) -> str:
        """The digest a refresh secret is stored and looked up by."""
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    def refresh_expiry(self, *, ttl_seconds: int, now: datetime | None = None) -> datetime:
        """When a refresh grant issued now stops being accepted."""
        return (now or utcnow()) + timedelta(seconds=ttl_seconds)


def require_a_real_secret(secret: str, *, environment: str) -> None:
    """Refuse to run a reachable Gateway on a key that is not a key.

    Two ways for a signing key to be worthless, checked together because they
    are the same failure from the caller's side — every token is forgeable:

    - it is the value shipped in the repository, which anyone who can read the
      source also has;
    - it is shorter than the 256 bits RFC 7518 asks of an HS256 key, which
      makes the key the cheapest thing to attack about the token.

    Both are allowed in `development` and `test`, where the Gateway is on
    loopback and the point of a shared default is that it is the same for
    everyone. `test` in particular has to be allowed: the suite mints tokens
    for users it invents, and requiring a deployment-grade secret to run a test
    would push the secret into the test file.

    Raises:
        ValueError: The production secret is missing, is the shipped default,
            or is too short to be an HS256 key.
    """
    if environment != "production":
        return
    if not secret or secret == DEV_SECRET:
        raise ValueError(
            "RAVEL_GATEWAY_JWT_SECRET is unset or is the shipped development "
            "default; in production that key is public, so any token signed "
            "with it can be forged by anyone who can read this repository"
        )
    if len(secret.encode("utf-8")) < MIN_SECRET_BYTES:
        raise ValueError(
            f"RAVEL_GATEWAY_JWT_SECRET is {len(secret.encode('utf-8'))} bytes and "
            f"HS256 asks for at least {MIN_SECRET_BYTES}; a short key is the "
            "cheapest thing to attack about a signed token"
        )
