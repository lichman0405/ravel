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

import logging
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.execution.node_runs import ExternalResultPort
from ravel.gateway.auth.tokens import InvalidAccessToken, TokenService
from ravel.gateway.conversation import MasterFactory
from ravel.gateway.runtime import HarnessRuntime
from ravel.gateway.stream import DEFAULT_CADENCE, Cadence
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository, UserRepository

logger = logging.getLogger(__name__)

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

    `master_of` is the fourth, and it is the only one a route reaches a *model*
    through. It arrives as a factory rather than as an object because a Master
    belongs to a project and a Gateway serves several, and it is a parameter
    with no default here rather than something this module builds, so that the
    one place that decides how a message reaches a harness is the application
    factory — and so that a test can hand the Gateway a scripted Master without
    touching the runtime at all.

    `runtime` is the same object `master_of` reaches Master through, held
    separately because two different routes want two different halves of it: the
    conversation wants a Master, and the administrator's screen wants to know
    how the harness is doing. Keeping only the factory would leave the second
    question unanswerable, and building a second pool to answer it would be a
    second pool.

    `cadence` is here for a narrower reason: the event stream is the only thing
    in the Gateway that does something on a timer, and a timer is the one kind
    of behaviour a test cannot wait for at its production rate. It is a
    parameter so that a test can make the stream look every few milliseconds,
    which is the difference between a suite that runs and a suite that sleeps.

    `deliver_external` is the fifth, and it is the only way a request reaches a
    *run*. A laboratory user's upload or report has to reach the Worker that is
    waiting for it, and that Worker is blocked in a durable wait inside a
    Temporal workflow — so the request has to send a signal, which is the one
    thing in this application that is neither a read of PostgreSQL nor a turn
    of a model. It is a parameter rather than something built here for the same
    reason `master_of` is: the composition that decides how a signal leaves the
    process belongs to the application factory, and a test that is about what
    the route records should not need a scheduler to be running.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        database: Database,
        tokens: TokenService,
        master_of: MasterFactory,
        runtime: HarnessRuntime,
        deliver_external: ExternalResultPort,
        cadence: Cadence | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.tokens = tokens
        self.master_of = master_of
        self.runtime = runtime
        self.deliver_external = deliver_external
        self.cadence = cadence or DEFAULT_CADENCE


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


def authenticate(state: GatewayState, token: str | None) -> Caller:
    """Turn a bearer token into the user it names, or refuse.

    The checks are written as a plain function rather than inside the
    dependency below because the event stream is a WebSocket, and a WebSocket
    cannot answer with a 401 — it has to close with a code. Two authorization
    paths would be two places for the rule to drift, so the socket calls this
    one and translates its refusal into a close, and the dependency calls it
    and lets FastAPI translate the same refusal into a response.

    Raises:
        HTTPException: 401. The reason is a fixed sentence rather than the
            token library's message, because a caller who cannot authenticate
            has no business learning which part of their token was wrong.
            `InvalidAccessToken` already says its reason is for the log and not
            for the response; putting it in the body anyway would distinguish
            an expired token from a forged one, and the second answer is a
            forgery being told which part of it to fix.
    """
    if not token:
        raise _unauthenticated("this route requires an access token")
    try:
        grant = state.tokens.verify_access(token)
    except InvalidAccessToken as refused:
        logger.info("refused an access token: %s", refused.reason)
        raise _unauthenticated("that access token was not accepted") from refused

    with state.database.read_only() as session:
        user = UserRepository(session).get(grant.user_id)
    if not user.is_active:
        raise _unauthenticated("this account is not active")
    return Caller(user_id=user.user_id, username=user.username)


def standing_in(state: GatewayState, project_id: str, caller: Caller) -> Principal:
    """The caller's standing in one project, or a refusal.

    Reads the membership row from PostgreSQL. Not from the token, and not from
    anything the caller sent: a token carries no project and no role, so a
    stolen one cannot be aimed at a project its owner was never a member of —
    which also means a revocation takes effect on the next request rather than
    whenever the token happens to expire.

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


def current_caller(
    state: Annotated[GatewayState, Depends(gateway_state)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(BEARER)],
) -> Caller:
    """`authenticate`, as a dependency. See it for what the check is."""
    return authenticate(
        state, credentials.credentials if credentials is not None else None
    )


def principal(
    project_id: str,
    caller: Annotated[Caller, Depends(current_caller)],
    state: Annotated[GatewayState, Depends(gateway_state)],
) -> Principal:
    """`standing_in`, as a dependency.

    `project_id` is read from the route's path. It is a dependency parameter
    rather than an argument each route passes so that no route can forget to
    check membership: naming `PrincipalDep` in a signature is what performs
    the check, and a route that omitted it would not have the project at all.
    """
    return standing_in(state, project_id, caller)


def require_director(standing: Annotated[Principal, Depends(principal)]) -> Principal:
    """Refuse a principal who may not direct this project's research.

    The parameter is spelled as a dependency rather than as a plain
    `Principal`, and that is load-bearing rather than decoration: FastAPI reads
    an unannotated dataclass parameter as a request *body*, so the plain form
    asks every caller for a JSON object describing their own standing. The
    membership check and the role check are one dependency chain, resolved once
    per request.

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


def require_administrator(standing: Annotated[Principal, Depends(principal)]) -> Principal:
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
#: the dependency is spelled. The two refusing ones are shorthands for a
#: *pair* — membership, then the role — so a route that names one has both
#: checks and cannot end up holding a principal nobody admitted to.
CallerDep = Annotated[Caller, Depends(current_caller)]
PrincipalDep = Annotated[Principal, Depends(principal)]
DirectorDep = Annotated[Principal, Depends(require_director)]
AdministratorDep = Annotated[Principal, Depends(require_administrator)]
GatewayStateDep = Annotated[GatewayState, Depends(gateway_state)]

__all__ = [
    "BEARER",
    "AdministratorDep",
    "Caller",
    "CallerDep",
    "DirectorDep",
    "GatewayState",
    "GatewayStateDep",
    "Principal",
    "PrincipalDep",
    "authenticate",
    "current_caller",
    "gateway_state",
    "principal",
    "require_administrator",
    "require_director",
    "standing_in",
]
