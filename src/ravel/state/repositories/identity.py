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
        """Give a user a role in this project."""
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
        rationale: str = "",
        required_role: UserRole = UserRole.PROJECT_OWNER,
        payload: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        """Raise a request. Master does this; Master cannot answer it."""
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

        `resolved_by` is a user identifier. There is no overload that accepts an
        agent, because an agent resolving its own request is the separation of
        powers failing in the one place it matters most.
        """
        approval = self.get(approval_id=approval_id)
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
