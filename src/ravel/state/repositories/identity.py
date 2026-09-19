"""Membership, agent identity, Master checkpoints, and approvals.

Two rules from the constitution are implemented here rather than in prompts:

- Authority is a property of a stored membership row, so revoking it takes
  effect on the next request instead of whenever a token expires.
- Master may *raise* an approval request and may not resolve one. There is no
  method on `ApprovalRepository` that writes `resolved_by` from an agent
  identity; resolution takes a user identifier, and the caller that has one is
  the Gateway acting for a human.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ravel.domain.enums import ApprovalStatus, UserRole
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.identity import (
    AgentIdentity,
    ApprovalRequest,
    MasterCheckpoint,
    ProjectMembership,
    User,
)
from ravel.domain.roles import AgentRole
from ravel.state.mapping import from_row
from ravel.state.outbox import emit
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.tables import (
    AgentIdentityRow,
    ApprovalRequestRow,
    MasterCheckpointRow,
    ProjectMembershipRow,
    UserRow,
)


class UserRepository:
    """Human accounts. Not project-scoped: a user exists across projects."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        username: str,
        password_hash: str | None = None,
        email: str | None = None,
    ) -> User:
        """Register an account. The caller hashes the password; this never sees one."""
        user = User(username=username, password_hash=password_hash, email=email)
        self.session.add(UserRow(**user.model_dump(mode="python")))
        return user

    def by_username(self, username: str) -> User | None:
        """The account with this username, if any."""
        row = (
            self.session.query(UserRow).filter(UserRow.username == username).one_or_none()
        )
        return from_row(User, row) if row is not None else None

    def get(self, user_id: str) -> User:
        """One account by identifier.

        Raises:
            NotFound: No such account.
        """
        row = self.session.get(UserRow, user_id)
        if row is None:
            raise NotFound(f"no user {user_id!r}")
        return from_row(User, row)


def memberships_of(session: Session, user_id: str) -> list[ProjectMembership]:
    """Every project this user belongs to, oldest grant first.

    A module-level function rather than a `MembershipRepository` method because
    that repository is scoped to one project and this question is deliberately
    across all of them — it is how a client learns which projects to offer, and
    it is the only read in the system that is not filtered by a project.

    That it takes a user identifier and returns only *that* user's rows is what
    keeps it from being a way around the scoping. There is no variant that
    enumerates a project's members, because nothing in V0 asks who else is in a
    project, and adding one would be a new authorization decision rather than a
    new query.
    """
    rows = (
        session.execute(
            select(ProjectMembershipRow)
            .where(ProjectMembershipRow.user_id == user_id)
            .order_by(ProjectMembershipRow.granted_at)
        )
        .scalars()
        .all()
    )
    return [from_row(ProjectMembership, row) for row in rows]


class MembershipRepository(ProjectScopedRepository[ProjectMembership]):
    """Who may do what in one project."""

    row_type = ProjectMembershipRow
    record_type = ProjectMembership

    def _order_by(self) -> Any:
        return ProjectMembershipRow.granted_at

    def for_user(self, user_id: str) -> ProjectMembership | None:
        """This user's membership in this project, if any."""
        return self._one(user_id=user_id)

    def may_direct(self, user_id: str) -> bool:
        """Whether this user may direct the project's research.

        Admin is excluded by the domain model, so a user who holds both
        operational and scientific authority cannot arise here.
        """
        membership = self.for_user(user_id)
        return membership is not None and membership.may_direct_project

    def grant(
        self,
        *,
        user_id: str,
        role: UserRole,
        granted_by: str | None = None,
    ) -> ProjectMembership:
        """Give a user a role in this project.

        Membership is where authority comes from, so this is the one write in
        the system that can manufacture it. It is therefore checked twice: a
        granter must hold authority at least equal to what is being granted,
        and only the project's *first* membership may be created without a
        granter at all. Without the second rule a caller could always reach
        power by way of an empty project; without the first, a lab user could
        promote themselves to owner.

        Raises:
            PermissionError: No granter was named for a project that already
                has members, or the granter does not hold this much authority.
        """
        existing = self.all()
        if granted_by is None:
            if existing:
                raise PermissionError(
                    f"{self.project_id} already has {len(existing)} membership(s); "
                    "only the first may be created without naming who granted it"
                )
        else:
            granter = self.for_user(granted_by)
            if granter is None or not granter.satisfies(role):
                held = granter.role.value if granter is not None else "no membership"
                raise PermissionError(
                    f"{granted_by!r} may not grant {role.value} in {self.project_id} "
                    f"({held}); a user cannot confer authority they do not hold"
                )
        return self.add(
            ProjectMembership(
                project_id=self.project_id,
                user_id=user_id,
                role=role,
                granted_by=granted_by,
            )
        )


class AgentIdentityRepository(ProjectScopedRepository[AgentIdentity]):
    """The durable half of each agent, one row per role per project."""

    row_type = AgentIdentityRow
    record_type = AgentIdentity

    def for_role(self, role: AgentRole) -> AgentIdentity | None:
        """The identity serving a role in this project, if one exists."""
        return self._one(role=role.value)

    def ensure(self, role: AgentRole) -> AgentIdentity:
        """The identity for a role, creating it on first use.

        Idempotent: a restarted runtime asks for the identity it already has
        rather than minting a second one, which is what keeps "a project has
        exactly one Master" true across process restarts.
        """
        existing = self.for_role(role)
        if existing is not None:
            return existing
        return self.add(AgentIdentity(project_id=self.project_id, role=role))

    def touch(self, role: AgentRole) -> AgentIdentity:
        """Record that an agent was seen just now."""
        identity = self.ensure(role)
        touched = identity.touched()
        row = self.session.get(AgentIdentityRow, identity.identity_id)
        assert row is not None
        row.last_seen_at = touched.last_seen_at
        return touched


class CheckpointRepository(ProjectScopedRepository[MasterCheckpoint]):
    """Master's written-down state, kept as a history.

    Each checkpoint is a new row. The newest one is a query, so a recovery can
    see not only where Master was but how it got there.
    """

    row_type = MasterCheckpointRow
    record_type = MasterCheckpoint

    def latest(self) -> MasterCheckpoint | None:
        """The most recent checkpoint, or `None` if Master has never written one."""
        row = (
            self.session.query(MasterCheckpointRow)
            .filter(MasterCheckpointRow.project_id == self.project_id)
            .order_by(MasterCheckpointRow.created_at.desc())
            .first()
        )
        return from_row(MasterCheckpoint, row) if row is not None else None

    def write(self, checkpoint: MasterCheckpoint, *, actor_id: str) -> MasterCheckpoint:
        """Record a checkpoint and note it in the stream."""
        self.add(checkpoint)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.MASTER_CHECKPOINTED,
            actor_type=ActorType.AGENT,
            actor_id=actor_id,
            payload={
                "checkpoint_id": checkpoint.checkpoint_id,
                "last_event_seq": checkpoint.last_event_seq,
            },
        )
        return checkpoint


class ApprovalRepository(ProjectScopedRepository[ApprovalRequest]):
    """Questions RAVEL asks a human, and the humans' answers."""

    row_type = ApprovalRequestRow
    record_type = ApprovalRequest

    def _order_by(self) -> Any:
        return ApprovalRequestRow.requested_at

    def pending(self) -> list[ApprovalRequest]:
        """Every request still waiting on a human."""
        return self.all(status=ApprovalStatus.PENDING.value)

    def request(
        self,
        *,
        requested_by: str,
        action: str,
        role: AgentRole,
        rationale: str = "",
        required_role: UserRole = UserRole.PROJECT_OWNER,
        payload: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Raise a request. Master does this; Master cannot answer it.

        Raises:
            PermissionError: The requester is not Master. Asking a human to
                authorize an action is part of deciding to take it, and
                deciding is Master's.
        """
        if role is not AgentRole.MASTER:
            raise PermissionError(
                f"the {role.value} role may not raise an approval request; asking "
                f"a human to authorize an action is part of "
                f"{AgentRole.MASTER.value}'s decision to take it"
            )
        approval = ApprovalRequest(
            project_id=self.project_id,
            requested_by=requested_by,
            action=action,
            rationale=rationale,
            required_role=required_role,
            payload=payload or {},
        )
        self.add(approval)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.APPROVAL_REQUESTED,
            actor_type=ActorType.AGENT,
            actor_id=requested_by,
            payload={"approval_id": approval.approval_id, "action": action},
        )
        return approval

    def resolve(
        self,
        approval_id: str,
        status: ApprovalStatus,
        *,
        resolved_by: str,
        note: str = "",
    ) -> ApprovalRequest:
        """Record a human's answer.

        `resolved_by` is a user identifier, and it is *checked*: the identifier
        must name an active account holding a membership in this project whose
        role satisfies the request's `required_role`. Taking the identifier on
        trust would make this method a way for any caller — an agent included —
        to answer its own question by writing down the name of a human who never
        saw it, which is the separation of powers failing in the one place it
        matters most.

        Raises:
            PermissionError: `resolved_by` is not an active member of this
                project, or does not hold the authority the request requires.
        """
        approval = self.get(approval_id=approval_id)
        self._require_resolver(approval, resolved_by)
        resolved = approval.resolve(status, resolved_by=resolved_by, note=note)

        row = self.session.get(ApprovalRequestRow, approval_id)
        assert row is not None
        for column in ("status", "resolved_by", "resolution_note", "resolved_at"):
            setattr(row, column, getattr(resolved, column))

        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.APPROVAL_RESOLVED,
            actor_type=ActorType.USER,
            actor_id=resolved_by,
            payload={
                "approval_id": approval_id,
                "status": resolved.status.value,
                "action": approval.action,
            },
        )
        return resolved

    def _require_resolver(self, approval: ApprovalRequest, resolved_by: str) -> None:
        """Refuse an answer from anyone without standing to give one.

        Raises:
            PermissionError: The resolver is not an active member holding the
                required authority.
        """
        membership = MembershipRepository(self.session, self.project_id).for_user(
            resolved_by
        )
        if membership is None:
            raise PermissionError(
                f"{resolved_by!r} is not a member of {self.project_id}; an approval "
                "is answered by a person with standing in the project, not by any "
                "identifier that happens to be written down"
            )
        if not membership.satisfies(approval.required_role):
            raise PermissionError(
                f"{resolved_by!r} holds {membership.role.value} in {self.project_id} "
                f"and {approval.display_id} requires {approval.required_role.value}"
            )
        if not UserRepository(self.session).get(resolved_by).is_active:
            raise PermissionError(f"account {resolved_by!r} is deactivated")
