"""Users, project membership, and agent identity.

Three user roles exist and no more. Authorization is a property of a
membership row, not of a token's claims: a user who loses a membership loses
the access on their next request rather than when their token expires.

`AgentIdentity` is the durable half of an agent. A harness session is
ephemeral — it dies with its process and cannot be resumed across processes —
so the identity, its checkpoints, and its decisions are what survive.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import ApprovalStatus, UserRole
from ravel.domain.events import ActorType
from ravel.domain.ids import DisplayPrefix, display_id, new_id
from ravel.domain.roles import AgentRole


class User(Record):
    """A human account. The password hash never leaves the identity service."""

    user_id: str = Field(default_factory=new_id)
    username: str = Field(min_length=1)
    #: Free text. V0 authenticates by username and password, so an address is
    #: a contact detail rather than an identifier, and validating it here would
    #: add a dependency without changing what the system does.
    email: str | None = None
    password_hash: str | None = None
    is_active: bool = True
    created_at: datetime = Field(default_factory=utcnow)


#: The authority each user role carries, weakest first. An approval's
#: `required_role` names the least authority that may answer it, so the
#: comparison is a ranking rather than an equality.
#:
#: Admin outranks a project owner here, and that is narrower than it looks.
#: It lets an administrator *administer* — answer a request that is waiting on
#: a human — and it does not make them a scientific decision maker: the DAG is
#: mutated by the Master agent role, and no method anywhere takes a user
#: identity and writes a node. So the rule the spec states, "admin cannot
#: silently become scientific decision maker", holds by there being no path,
#: not by this ordering.
_USER_ROLE_RANK: dict[UserRole, int] = {
    UserRole.LAB_USER: 0,
    UserRole.PROJECT_OWNER: 1,
    UserRole.ADMIN: 2,
}


class ProjectMembership(Record):
    """What one user may do in one project."""

    membership_id: str = Field(default_factory=new_id)
    project_id: str
    user_id: str
    role: UserRole
    granted_at: datetime = Field(default_factory=utcnow)
    granted_by: str | None = None

    @property
    def is_admin(self) -> bool:
        """Whether this membership carries administrative authority."""
        return self.role is UserRole.ADMIN

    @property
    def may_direct_project(self) -> bool:
        """Whether this membership may direct the project.

        Admin is deliberately excluded. An administrator who could also decide
        the research route would hold both the operational and the scientific
        authority, which is what the separation exists to prevent.
        """
        return self.role is UserRole.PROJECT_OWNER

    def satisfies(self, required: UserRole) -> bool:
        """Whether this membership carries at least the required authority."""
        return _USER_ROLE_RANK[self.role] >= _USER_ROLE_RANK[required]


class AgentIdentity(Record):
    """One agent's durable identity within one project.

    A project has exactly one Master identity, which outlives every harness
    session that serves it. Workers are task-scoped and are not given a durable
    identity: their authority comes from a frozen contract, and a persistent
    identity would invite them to accumulate authority across tasks.
    """

    identity_id: str = Field(default_factory=new_id)
    project_id: str
    role: AgentRole
    display_name: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    last_seen_at: datetime | None = None
    is_retired: bool = False

    @model_validator(mode="after")
    def _display_name_defaults_to_the_role(self) -> AgentIdentity:
        if not self.display_name:
            object.__setattr__(self, "display_name", self.role.display_name)
        return self

    def touched(self, at: datetime | None = None) -> AgentIdentity:
        """Return a copy recording that the identity was seen just now."""
        return self.model_copy(update={"last_seen_at": at or utcnow()})


class MasterCheckpoint(Record):
    """What Master was thinking, written where a recovery can read it.

    Deliberately not the harness session: a session does not survive its
    process, so a checkpoint that lived only there would be lost exactly when
    it is needed. `last_event_seq` is the anchor — recovery replays the event
    stream from there rather than trusting the summary alone.

    This is a navigational aid, never the authoritative record. Every claim in
    it must be re-derivable from PostgreSQL.
    """

    checkpoint_id: str = Field(default_factory=new_id)
    project_id: str
    master_identity_id: str
    current_focus: str = ""
    active_hypotheses: tuple[str, ...] = ()
    pending_questions: tuple[str, ...] = ()
    waiting_on: tuple[str, ...] = ()
    recent_decision_refs: tuple[str, ...] = ()
    important_context_refs: tuple[str, ...] = ()
    last_event_seq: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utcnow)


class ApprovalRequest(Record):
    """A question RAVEL is asking a human before it proceeds.

    The authority envelope decides which actions need one of these. Master may
    not resolve its own request: `resolved_by` is a user, and the domain model
    does not offer Master a path to write it.
    """

    approval_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.APPROVAL))
    project_id: str
    requested_by: str
    action: str = Field(min_length=1)
    rationale: str = ""
    payload: dict[str, object] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    required_role: UserRole = UserRole.PROJECT_OWNER
    requested_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_note: str = ""
    decision_ref: str | None = None

    @property
    def is_pending(self) -> bool:
        """Whether a human still has to answer."""
        return self.status is ApprovalStatus.PENDING

    def resolve(
        self,
        status: ApprovalStatus,
        resolved_by: str,
        note: str = "",
        at: datetime | None = None,
    ) -> ApprovalRequest:
        """Return a resolved copy.

        Raises:
            ValueError: The request is already resolved, or the resolution is
                not a terminal one.
        """
        if not self.is_pending:
            raise ValueError(f"approval {self.display_id} is already {self.status.value}")
        if status is ApprovalStatus.PENDING:
            raise ValueError("resolving an approval to PENDING would not resolve it")
        return self.model_copy(
            update={
                "status": status,
                "resolved_by": resolved_by,
                "resolution_note": note,
                "resolved_at": at or utcnow(),
            }
        )


class RefreshToken(Record):
    """One grant of a refresh token, as it was issued.

    **A row is a grant and never a session.** Rotating writes a *second* row
    naming the first as its parent, so the chain of a single login is a series
    of immutable facts rather than one row edited each time somebody refreshes.
    That is what makes the interesting question — *was this token already
    used?* — a query (`has_child`) rather than a flag somebody has to remember
    to set, and it is why this table is append-only like every other record in
    RAVEL.

    The secret is not here. `token_hash` is a SHA-256 of it, which is what the
    repository looks the presented token up by; a leaked database therefore
    holds no usable token. A password is hashed with Argon2id because it is
    low-entropy and guessable; a refresh token is 256 bits of randomness, so a
    fast digest is the right tool and the search space is what protects it.

    Expiry is stored rather than derived from `issued_at`. The lifetime is a
    deployment setting, and a token issued under yesterday's setting has to
    keep whatever lifetime it was granted.
    """

    token_id: str = Field(default_factory=new_id)
    user_id: str
    #: The chain this grant belongs to: the id of the first token issued for
    #: one login. Revoking a family is how a detected replay is contained —
    #: the thief and the victim both lose the chain, which is the only safe
    #: answer once a token is known to have been copied.
    family_id: str
    #: The grant this one replaced, or `None` for a family's first.
    parent_id: str | None = None
    token_hash: str = Field(min_length=1)
    issued_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime

    def is_expired(self, at: datetime | None = None) -> bool:
        """Whether the grant's lifetime has run out."""
        return (at or utcnow()) >= self.expires_at


class RevokedTokenFamily(Record):
    """A login chain that is no longer accepted, and why.

    A row here rather than a column on `RefreshToken` for the same reason
    rotation writes a new row: revoking is an event, and the record of it has
    to survive whatever happens to the tokens it is about. It also means the
    two facts stay separable — *this grant was issued* remains true of a grant
    whose family was later revoked.
    """

    family_id: str
    user_id: str
    reason: str = ""
    revoked_at: datetime = Field(default_factory=utcnow)


class MasterMessage(Record):
    """One thing said between a person and Master, in the order it was said.

    The conversation is recorded here rather than read back out of the harness,
    for the reason a checkpoint is: a DSH session dies with its process and
    cannot be resumed across one, so a conversation kept only there would
    vanish at the moment somebody wants to read what was decided. `docs/04`
    also makes the point the other way round — DSH session is not Project — and
    a transcript stored in the harness would be exactly that confusion.

    A row is one utterance, and it is append-only like everything else. Nothing
    edits a message and nothing unsays one, which is what lets a later reader
    treat the transcript as a record of what was actually said rather than of
    what somebody last thought should have been said.

    `turn_id` groups a question with whatever answering it produced. Master may
    take several turns' worth of tool calls before it says anything, so the
    reply is not necessarily one message, and a grouping that was inferred from
    timestamps would be a guess.
    """

    message_id: str = Field(default_factory=new_id)
    project_id: str
    #: The exchange this belongs to: one message from a person and everything
    #: Master said back about it.
    turn_id: str = Field(default_factory=new_id)
    #: A user id when a person spoke, or Master's agent identity when Master
    #: did. Which of the two is `author_type`, and the pair is what a reader
    #: needs to attribute the row without consulting a second table.
    author_id: str
    author_type: ActorType
    body: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utcnow)
