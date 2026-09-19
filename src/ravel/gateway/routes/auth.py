"""Login, refresh, logout, and who am I.

`docs/09` settles the shape: Argon2id for passwords, a short-lived access
token, and refresh token rotation. Authentication terminates here — DSH has no
end-user identity responsibility, and a user is never asked to hold a harness
credential.

Three decisions are worth stating because they are the ones a login endpoint
usually gets wrong:

**A wrong username and a wrong password are one answer.** Both produce the same
401 with the same text. When the account does not exist the password is
verified against a stand-in hash anyway, so the two paths do the same work and
take the same time; otherwise the endpoint is an oracle for which usernames are
registered.

**Refresh rotation revokes the family on replay.** That rule lives in
`RefreshTokenRepository.rotate`, not here. This module's part is to hand it the
presented token's digest and to let the refusal through as one answer, because
telling a caller their token was *replayed* tells whoever copied it that the
copy was noticed.

**The access token is not revocable, and logout does not pretend otherwise.**
It is a bearer token with no server-side record and it expires on its own,
which is what "short-lived" is for. What logout cuts is the chain, so the
session cannot be extended.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from ravel.domain.identity import RefreshToken
from ravel.gateway.auth.passwords import hash_password, verify_password
from ravel.gateway.deps import CallerDep, GatewayStateDep
from ravel.state.repositories.identity import UserRepository, memberships_of
from ravel.state.repositories.tokens import RefreshRefused, RefreshTokenRepository

router = APIRouter(prefix="/auth", tags=["auth"])

#: A real Argon2id hash of a password nobody has. Verified against when no
#: account matches, so that a login attempt costs the same whether or not the
#: username exists. It is hashed here rather than written out as a literal so
#: that it is a genuine hash of this deployment's parameters: a constant copied
#: from elsewhere would cost whatever *its* parameters cost, and the timing
#: would differ by exactly the amount this is meant to hide.
_STAND_IN_HASH = hash_password("this is not a password anybody has")


class LoginRequest(BaseModel):
    """What a person types."""

    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class RefreshRequest(BaseModel):
    """The grant handed back by a previous login or refresh."""

    refresh_token: str = Field(min_length=1)


class TokenPair(BaseModel):
    """What a successful authentication returns.

    Both halves are returned because they answer different needs: the access
    token is what a client sends on every request and cannot be revoked, and
    the refresh token is single-use and is what a client keeps. A client handed
    only the first would have to log in again within the hour; one handed only
    the second would refresh before every request.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: str
    username: str


class Membership(BaseModel):
    """One project this user may see, and the role they hold in it."""

    project_id: str
    role: str


class Identity(BaseModel):
    """Who a token belongs to."""

    user_id: str
    username: str
    memberships: list[Membership]


@router.post("/login", summary="Exchange a username and password for tokens")
def login(request: LoginRequest, state: GatewayStateDep) -> TokenPair:
    """Authenticate and open a login chain.

    Raises:
        HTTPException: 401. The account is unknown, the password is wrong, or
            the account is deactivated — one answer for all three, and the same
            amount of work for the first as for the second.
    """
    with state.database.read_only() as session:
        user = UserRepository(session).by_username(request.username)

    # An account may exist with no password set, which is not a way in: the
    # stand-in hash stands in for that case exactly as it does for no account.
    stored = (user.password_hash if user is not None else None) or _STAND_IN_HASH
    accepted = verify_password(stored_hash=stored, password=request.password)
    if not accepted or user is None or not user.is_active:
        raise _refused()

    return _open_chain(state, user.user_id, user.username)


@router.post("/refresh", summary="Exchange a refresh grant for a new pair")
def refresh(request: RefreshRequest, state: GatewayStateDep) -> TokenPair:
    """Rotate a login chain.

    An account that has been deactivated is refused here exactly as it is at
    login. That check is not redundant with the one `current_caller` makes on
    every request: without it a deactivated account would keep being handed
    fresh chains, and the only thing standing between it and the API would be
    the access token's own expiry. A state enforced on one way in and not the
    other is enforced by accident.

    Raises:
        HTTPException: 401. The grant is unknown, expired, revoked, or has
            already been exchanged, or the account it belongs to is no longer
            active. The five are one answer.
    """
    presented = state.tokens.refresh_hash(request.refresh_token)
    expires_at = state.tokens.refresh_expiry(
        ttl_seconds=state.settings.gateway_refresh_ttl_seconds
    )
    secret = state.tokens.new_refresh_secret()

    rotated: RefreshToken | None = None
    refusal: RefreshRefused | None = None
    with state.database.transaction() as session:
        try:
            rotated = RefreshTokenRepository(session).rotate(
                presented_hash=presented,
                new_token_hash=state.tokens.refresh_hash(secret),
                expires_at=expires_at,
            )
        except RefreshRefused as refused:
            # Caught *inside* the transaction rather than allowed to escape it,
            # and that is not tidiness. `rotate` revokes the whole family when
            # it detects a replay and then refuses; an exception leaving this
            # block would roll the revocation back, and the theft that was just
            # noticed would leave the chain alive for the next attempt. The
            # refusal is reported below, after the transaction has committed.
            refusal = refused

    if rotated is None:
        raise _refused() from refusal

    with state.database.read_only() as session:
        user = UserRepository(session).get(rotated.user_id)
    if not user.is_active:
        raise _refused()
    return _pair(state, user.user_id, user.username, secret)


@router.post("/logout", summary="Cut a login chain")
def logout(request: RefreshRequest, state: GatewayStateDep) -> dict[str, str]:
    """Revoke the chain this grant belongs to.

    Idempotent, and deliberately uninformative: a caller presenting a token
    that was never issued is told exactly what one whose chain is now dead is
    told. A logout that distinguished them would be a way to ask whether a
    token is valid.
    """
    digest = state.tokens.refresh_hash(request.refresh_token)
    with state.database.transaction() as session:
        repository = RefreshTokenRepository(session)
        grant = repository.by_hash(digest)
        if grant is not None:
            repository.revoke_family(
                family_id=grant.family_id,
                user_id=grant.user_id,
                reason="logged out",
            )
    return {"status": "ok"}


@router.get("/me", summary="Who this token belongs to, and where")
def me(caller: CallerDep, state: GatewayStateDep) -> Identity:
    """The caller, and the projects they may see.

    The memberships are here rather than only under `/projects` because a
    client has to know which role it is holding before it can decide which
    screen to open, and being told once is one chance to be wrong instead of
    two.
    """
    with state.database.read_only() as session:
        held = memberships_of(session, caller.user_id)
    return Identity(
        user_id=caller.user_id,
        username=caller.username,
        memberships=[
            Membership(project_id=membership.project_id, role=membership.role.value)
            for membership in held
        ],
    )


def _open_chain(state: GatewayStateDep, user_id: str, username: str) -> TokenPair:
    """Issue an access token and the first refresh grant of a new chain."""
    secret = state.tokens.new_refresh_secret()
    expires_at = state.tokens.refresh_expiry(
        ttl_seconds=state.settings.gateway_refresh_ttl_seconds
    )
    with state.database.transaction() as session:
        RefreshTokenRepository(session).issue(
            user_id=user_id,
            token_hash=state.tokens.refresh_hash(secret),
            expires_at=expires_at,
        )
    return _pair(state, user_id, username, secret)


def _pair(state: GatewayStateDep, user_id: str, username: str, secret: str) -> TokenPair:
    """An access token for `user_id`, alongside an already-minted refresh secret.

    The refresh secret is a parameter rather than minted here because the two
    callers learn it at different moments: `refresh` has to hash it *before*
    the transaction that records it, so that the digest stored and the secret
    returned are provably the same one.
    """
    access_token, _grant = state.tokens.issue_access(user_id)
    return TokenPair(
        access_token=access_token,
        refresh_token=secret,
        expires_in=state.settings.gateway_token_ttl_seconds,
        user_id=user_id,
        username=username,
    )


def _refused() -> HTTPException:
    """The one answer every authentication failure gets."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="those credentials were not accepted",
        headers={"WWW-Authenticate": "Bearer"},
    )


__all__ = ["Identity", "LoginRequest", "Membership", "RefreshRequest", "TokenPair", "router"]
