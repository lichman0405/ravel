"""The versioned authentication surface.

These routes call the existing handlers rather than restating them. That is the
compatibility rule this package follows everywhere: the legacy routes are what
the TUI uses today, and a second implementation of login rotation or of the
failure budget would be a second set of rules that could drift. What `/v1`
adds is a stable prefix and the error envelope; the authentication itself is
exactly the same code.

No harness credential is involved at any point. A v1 token names a RAVEL user
and carries no role and no project — authority is read from the membership row
on every request, as `deps` documents.
"""

from __future__ import annotations

from fastapi import APIRouter, Request

from ravel.gateway.deps import GatewayStateDep
from ravel.gateway.routes import auth as legacy
from ravel.gateway.routes.auth import LoginRequest, RefreshRequest, TokenPair

router = APIRouter(tags=["v1-auth"])


@router.post(
    "/auth/login",
    response_model=TokenPair,
    summary="Exchange a username and password for tokens",
)
def login(
    credentials: LoginRequest, request: Request, state: GatewayStateDep
) -> TokenPair:
    """The same login, rate limit and refusal timing as the legacy route."""
    return legacy.login(credentials, request, state)


@router.post(
    "/auth/refresh",
    response_model=TokenPair,
    summary="Exchange a refresh grant for a new pair",
)
def refresh(request: RefreshRequest, state: GatewayStateDep) -> TokenPair:
    """The same single-use rotation and family revocation as the legacy route."""
    return legacy.refresh(request, state)


@router.post("/auth/logout", response_model=dict[str, str], summary="Cut a login chain")
def logout(request: RefreshRequest, state: GatewayStateDep) -> dict[str, str]:
    """The same idempotent chain cut as the legacy route."""
    return legacy.logout(request, state)


__all__ = ["router"]
