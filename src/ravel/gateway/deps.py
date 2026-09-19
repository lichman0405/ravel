"""Who is asking, and what they are allowed to ask for.

Every route that touches a project resolves its authority the same way, and
this module is the only place that does it. The rule from `docs/09` is that an
action checks four things — authenticated user, project membership, role,
requested action — and the important word is *membership*: the project a
request names comes from the URL, and the authority to act on it comes from a
row in PostgreSQL. Nothing the caller sends is treated as proof of anything.

**A refusal does not distinguish absent from forbidden.** A caller with no
membership in a project is told the project does not exist, because telling
them otherwise confirms it does, which is a fact about somebody else's
research. This is the same answer `ProjectScopedRepository._one` gives for a
record in another project, for the same reason.

Two of the checks below look interchangeable and are not:

- `Principal.directs` is true only for a `PROJECT_OWNER`;
- `Principal.may_answer` follows the domain's ranking, where an administrator
  outranks an owner.

`docs/08` decides which is which by assigning capabilities to views, and the
split matters: pausing a project and editing its Authority Envelope are the
owner's, while answering an approval is something an administrator may do.
An administrator who could also direct the research would hold both the
operational and the scientific authority, which is what the separation exists
to prevent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.gateway.auth.tokens import InvalidAccessToken, TokenService
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository, UserRepository

#: The authority each user role carries, weakest first. The domain keeps its
#: own copy in `ravel.domain.identity`; this one is repeated rather than
#: imported because that name is private to that module, and the agreement
#: between the two is asserted by a test rather than assumed here.
_ROLE_RANK: dict[UserRole, int] = {
    UserRole.LAB_USER: 0,
    UserRole.PROJECT_OWNER: 1,
    UserRole.ADMIN: 2,
}

#: Parsed for its OpenAPI documentation as much as for its behaviour. It is
#: declared with `auto_error=False` so that a missing credential reaches
#: `current_caller` and is answered with this application's own refusal rather
#: than with the library's.
BEARER = HTTPBearer(auto_error=False, description="A short-lived access token.")


class GatewayState:
    """What a request needs that outlives the request.

    Constructed once, by `create_app`, and reached through `app.state`. Held in
    one object rather than installed as separate attributes so a route can only
    get at the database, the settings and the tokens together — a route that
    needs the token service but not the database does not exist, and if one
    appears it should be obvious that it was deliberate.
    """

    def __init__(self, *, settings: Settings, database: Database, tokens: TokenService) -> None:
        self.settings = settings
        self.database = database
        self.tokens = tokens


@dataclass(frozen=True)
class Caller:
    """An authenticated user, with no project in view.

    The routes that take one are the ones where no project is being named —
    logging in, listing the projects the caller can see — so there is nothing
    for membership to be checked against yet.
    """

    user_id: str
    username: str


@dataclass(frozen=True)
class Principal:
    """A `Caller`, plus what they may do in the project the request names."""

    caller: Caller
    project_id: str
    role: UserRole

    @property
    def user_id(self) -> str:
        return self.caller.user_id

    @property
    def username(self) -> str:
        return self.caller.username

    @property
    def directs(self) -> bool:
        """Whether this user may direct the project's research.

        True for an owner and false for an administrator, which is the
        distinction `ProjectMembership.may_direct_project` draws. Restated
        rather than delegated only because the route reads better for it; the
        rule itself is the domain's.
        """
        return self.role is UserRole.PROJECT_OWNER

    @property
    def administers(self) -> bool:
        """Whether this user may inspect the runtime in this project."""
        return self.role is UserRole.ADMIN

    def may_answer(self, required: UserRole) -> bool:
        """Whether this user's authority suffices to answer a request.

        Uses the domain's ranking, so an administrator may resolve an approval
        that names an owner as its resolver. That is narrower than it looks:
        resolving an approval records a human's answer to a question RAVEL was
        not allowed to decide, and it mutates no node — Master does that, and
        Master is not a user.
        """
        return _ROLE_RANK[self.role] >= _ROLE_RANK[required]


# ── Dependencies ────────────────────────────────────────────────────────────
#
# FastAPI resolves each of these once per request and reuses the result, so a
# route that names both `principal` and `gateway_state` opens one session for
# the membership check rather than two. The dependencies are therefore written
# to be small and composable rather than bundled.


def gateway_state(request: Request) -> GatewayState:
    """The application's own state, as installed by `create_app`."""
    state: GatewayState = request.app.state.ravel
    return state


def current_caller(
    state: Annotated[GatewayState, Depends(gateway_state)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)],
) -> Caller:
    """The authenticated user, or a refusal.

    The token says *who*, and nothing else. It carries no project and no role,
    so a stolen token cannot be aimed at a project its owner was never a member
    of — the membership row is consulted on every request, which also means
    revoking one takes effect immediately rather than when a token expires.

    Raises:
        HTTPException: 401. The reason is a fixed sentence rather than the
            token library's message, because a caller who cannot authenticate
            has no business learning which part of their token was wrong.
    """
    if credentials is None or not credentials.credentials:
        raise _unauthenticated("this route requires an access token")
    try:
        grant = state.tokens.verify_access(credentials.credentials)
    except InvalidAccessToken as refused:
        raise _unauthenticated(str(refused)) from refused

    with state.database.read_only() as session:
        user = UserRepository(session).get(grant.user_id)
    if not user.is_active:
        raise _unauthenticated("this account is not active")
    return Caller(user_id=user.user_id, username=user.username)


def principal(
    project_id: str,
    caller: Annotated[Caller, Depends(current_caller)],
    state: Annotated[GatewayState, Depends(gateway_state)],
) -> Principal:
    """The caller's standing in the project this request names.

    `project_id` is read from the route's path. It is a dependency parameter
    rather than an argument each route passes so that no route can forget to
    check membership: naming `PrincipalDep` in a signature is what performs
    the check, and a route that omitted it would not have the project at all.

    Raises:
        HTTPException: 404, whether the project does not exist or this caller
            is not a member of it. The two are one answer on purpose.
    """
    with state.database.read_only() as session:
        membership = MembershipRepository(session, project_id).for_user(caller.user_id)
    if membership is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no project {project_id!r}",
        )
    return Principal(caller=caller, project_id=project_id, role=membership.role)


def require_director(standing: Principal) -> Principal:
    """Refuse a principal who may not direct this project's research.

    Raises:
        HTTPException: 403. Unlike a missing membership, this is not hidden:
            the caller can already see the project, so pretending it is absent
            would be a lie rather than a silence.
    """
    if not standing.directs:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="directing this project's research requires the PROJECT_OWNER role",
        )
    return standing


def require_administrator(standing: Principal) -> Principal:
    """Refuse a principal who may not inspect this project's runtime."""
    if not standing.administers:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="inspecting the runtime requires the ADMIN role",
        )
    return standing


def _unauthenticated(reason: str) -> HTTPException:
    """A 401 that says what kind of credential was wanted."""
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=reason,
        headers={"WWW-Authenticate": "Bearer"},
    )


#: Shorthands, so a route signature reads as what it needs rather than as how
#: the dependency is spelled.
CallerDep = Annotated[Caller, Depends(current_caller)]
PrincipalDep = Annotated[Principal, Depends(principal)]
GatewayStateDep = Annotated[GatewayState, Depends(gateway_state)]

__all__ = [
    "BEARER",
    "Caller",
    "CallerDep",
    "GatewayState",
    "GatewayStateDep",
    "Principal",
    "PrincipalDep",
    "current_caller",
    "gateway_state",
    "principal",
    "require_administrator",
    "require_director",
]
