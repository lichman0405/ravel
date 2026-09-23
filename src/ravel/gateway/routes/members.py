"""Opening a project, and who may be in it.

This is the Gateway's only write to a membership, and the module exists as a
module for that reason: `docs/09` gives the owner three controls over a running
project and this is not one of them, because a membership is not a control —
it is where authority comes from. The route that can manufacture authority is
worth being able to find.

**Opening a project is how a person becomes an owner.** A project's first
membership is the one that may be created without naming a granter, so the
creator is its owner; every later membership names who conferred it. That is
the same rule `scripts/create_account.py` follows for an operator, and it is
why this route is open to any authenticated caller rather than to an owner:
there is nobody to be an owner *of* yet. It is not registration — the account
already exists, and RAVEL still cannot create one over HTTP.

**Managing members is the owner's alone.** Not the administrator's, and the
check is `may_direct_project` rather than the rank comparison: `ADMIN` outranks
`PROJECT_OWNER` in the domain's ordering, which exists so an administrator may
*answer* an approval, and administering a runtime is a different authority from
deciding who may join a project. A lab user reaches none of these routes.

**A withdrawal is recorded, never deleted, and a project keeps an owner.** What
the repository refuses — the last owner stepping down, a membership edited in
place — is refused there, so the script and any future screen obey the same
rules as these routes.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ravel.domain.enums import UserRole
from ravel.gateway.deps import CallerDep, DirectorDep, GatewayStateDep
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry

router = APIRouter(prefix="/projects", tags=["members"])


class NewProject(BaseModel):
    """What a person must say to open a project.

    Both fields are required and neither has a default. The objective is what
    Master plans against, so a project without one is a project whose first
    turn has nothing to plan from, and `"Untitled"` would be a title RAVEL
    invented rather than one somebody chose.
    """

    title: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=4000)


class MemberGrant(BaseModel):
    """Who to add, and as what.

    The account is named by username rather than by identifier: the person
    adding somebody knows their name, and a client that had to look up an
    identifier first would need a user directory this Gateway deliberately does
    not have. The user must already exist — creating an account is an
    operator's job, at a terminal, on the host.
    """

    username: str = Field(min_length=1, max_length=100)
    role: UserRole


class MemberView(BaseModel):
    """One membership, as the owner's screen reads it.

    `revoked_at` is what tells the two states apart, and it is the reason this
    is one view rather than two: the member list returns live memberships only,
    so it is null there, and the answer to a withdrawal is the membership that
    was just withdrawn, so it is set. A body without the field would describe a
    withdrawn membership and a live one identically, which is the confusion the
    rest of this item exists to prevent.
    """

    user_id: str
    username: str
    role: str
    granted_at: datetime
    granted_by: str | None
    revoked_at: datetime | None = None


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    summary="Open a project, owned by whoever opened it",
)
def create_project(
    caller: CallerDep, state: GatewayStateDep, request: NewProject
) -> dict[str, str]:
    """Create a project and its first membership, in one transaction.

    One transaction because the two are one fact: a project whose creator was
    not granted its ownership is a project nobody can direct and nobody can
    repair, since memberships are granted by owners.
    """
    with state.database.transaction() as session:
        project = ProjectRegistry(session).create(
            title=request.title,
            objective=request.objective,
            created_by=caller.user_id,
        )
        MembershipRepository(session, project.project_id).grant(
            user_id=caller.user_id,
            role=UserRole.PROJECT_OWNER,
        )
    return {
        "project_id": project.project_id,
        "display_id": project.display_id,
        "title": project.title,
        "status": project.status.value,
        "role": UserRole.PROJECT_OWNER.value,
    }


@router.get(
    "/{project_id}/members",
    summary="Everyone with a live role in this project",
)
def read_members(standing: DirectorDep, state: GatewayStateDep) -> list[MemberView]:
    """The live memberships, oldest first.

    Withdrawn rows are absent rather than flagged: this is the list an owner
    manages, and a membership that is not in force is not a member. Who *was*
    a member is a question about the project's history, and the event stream is
    where it is answered.
    """
    with state.database.read_only() as session:
        memberships = MembershipRepository(session, standing.project_id).active()
        users = UserRepository(session)
        found: list[MemberView] = []
        for membership in memberships:
            account = users.get(membership.user_id)
            found.append(
                MemberView(
                    user_id=membership.user_id,
                    username=account.username,
                    role=membership.role.value,
                    granted_at=membership.granted_at,
                    granted_by=_username_in(session, membership.granted_by),
                )
            )
    return found


@router.post(
    "/{project_id}/members",
    status_code=status.HTTP_201_CREATED,
    summary="Add somebody who already has an account to this project",
)
def add_member(
    standing: DirectorDep, state: GatewayStateDep, request: MemberGrant
) -> MemberView:
    """Grant a live membership, named by username.

    Raises:
        HTTPException: 404 if there is no such account, or if the caller is not
            a member of this project at all; 403 if the caller is a member but
            not one who may direct it; 409 if the user already holds a live
            membership here — a role change is a withdrawal and a grant, so
            that both are on the record.
    """
    with state.database.transaction() as session:
        account = UserRepository(session).by_username(request.username)
        if account is None:
            # Named by username in the refusal because that is what the caller
            # sent, and because "no account called that" is not a fact about
            # this project that a caller was not entitled to.
            raise NotFound(f"no account named {request.username!r}")
        repository = MembershipRepository(session, standing.project_id)
        # Read to explain, not to decide: `grant` refuses this write for every
        # caller, and what the read adds is *which* refusal to answer with.
        # A membership in force is not a permission problem — the caller may
        # add people — it is a request that conflicts with a fact, and the two
        # are different statuses because a client does different things with
        # them: one is fixed by asking somebody else, the other by withdrawing
        # the role first.
        held = repository.for_user(account.user_id)
        if held is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{account.username} already holds {held.role.value} in this "
                    "project; change a membership by withdrawing it and granting "
                    "the new one, so both are on the record"
                ),
            )
        membership = repository.grant(
            user_id=account.user_id,
            role=request.role,
            granted_by=standing.user_id,
        )
    return MemberView(
        user_id=account.user_id,
        username=account.username,
        role=membership.role.value,
        granted_at=membership.granted_at,
        granted_by=standing.username,
    )


@router.post(
    "/{project_id}/members/{user_id}/revoke",
    summary="Withdraw somebody's role in this project",
)
def revoke_member(
    standing: DirectorDep, state: GatewayStateDep, user_id: str
) -> MemberView:
    """Withdraw a live membership. The row stays; the authority goes.

    Raises:
        HTTPException: 404 if that user holds no live membership here; 403 if
            the caller may not direct the project; 409 if this is the project's
            last owner, since the request conflicts with a fact rather than
            with the caller's authority.
    """
    with state.database.transaction() as session:
        account = UserRepository(session).get(user_id)
        repository = MembershipRepository(session, standing.project_id)
        # The same read-to-explain as `add_member`: the repository holds the
        # rule, and this decides which refusal to answer with. A withdrawal
        # that raced past this read is still refused, and answered 403 with the
        # repository's own sentence — the status is then imprecise about a case
        # that took two simultaneous withdrawals to reach, which is a smaller
        # problem than a rule that only one of the two callers obeys.
        held = repository.for_user(user_id)
        if (
            held is not None
            and held.role is UserRole.PROJECT_OWNER
            and len(repository.owners()) <= 1
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"{account.username} is this project's last owner; a project "
                    "with no owner cannot be directed by anyone, and nothing in "
                    "RAVEL can restore one"
                ),
            )
        membership = repository.revoke(user_id, revoked_by=standing.user_id)
        granted_by = _username_in(session, membership.granted_by)
    return MemberView(
        user_id=membership.user_id,
        username=account.username,
        role=membership.role.value,
        granted_at=membership.granted_at,
        granted_by=granted_by,
        revoked_at=membership.revoked_at,
    )


def _username_in(session: Session, user_id: str | None) -> str | None:
    """The username behind a `granted_by` identifier, for display.

    A membership granted by an account that no longer resolves still has to
    render as *something* on the owner's screen, and the identifier is the
    honest fallback: it is what the row says, and inventing a name for it would
    be the screen making something up.
    """
    if user_id is None:
        return None
    try:
        return UserRepository(session).get(user_id).username
    except NotFound:
        return user_id


__all__ = ["MemberGrant", "MemberView", "NewProject", "router"]
