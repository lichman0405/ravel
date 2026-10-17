"""Who may manufacture authority, and who may answer a question put to a human.

These are the two places where the separation of powers can be undone by a
single write, so both are asserted against the real database rather than
against the domain types alone:

- membership is where authority comes from, so `grant` is the one method in the
  system that can create it — and it is checked twice, for the caller's
  standing and for the first-membership case;
- an approval exists because RAVEL is not allowed to decide something by
  itself, so `resolve` must not accept an answer from anything but a person
  with standing in the project. Taking `resolved_by` as a given string would
  make it a way for an agent to answer its own question by writing down a
  human's name.
"""

from __future__ import annotations

import pytest
from sqlalchemy import delete, update
from sqlalchemy.exc import IntegrityError

from ravel.domain.enums import ApprovalStatus, UserRole
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.outbox import events_since
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.identity import (
    ApprovalRepository,
    MembershipRepository,
    UserRepository,
    memberships_of,
)
from ravel.state.tables import ProjectMembershipRow

pytestmark = pytest.mark.integration

NON_MASTER = [
    AgentRole.RESEARCH,
    AgentRole.REVIEW,
    AgentRole.COMPUTE_WORKER,
    AgentRole.EXPERIMENTAL_WORKER,
]


def _owner_id(database: Database) -> str:
    """The project fixture's owner, read back the way a request would find them."""
    with database.read_only() as session:
        owner = UserRepository(session).by_username("owner")
        assert owner is not None
        return owner.user_id


def _a_user(database: Database, username: str) -> str:
    """Create an account and return its identifier.

    A membership names a user and the database holds it to that with a foreign
    key, so a test that invented an identifier would fail on the key rather
    than on the rule it is about.
    """
    with database.transaction() as session:
        return UserRepository(session).create(username=username).user_id


def _an_approval(database: Database, project: Project, *, required_role: UserRole):
    """An approval Master raised and nobody has answered."""
    with database.transaction() as session:
        return ApprovalRepository(session, project.project_id).request(
            requested_by="master",
            action="Widen the dopant series beyond the contract.",
            role=AgentRole.MASTER,
            required_role=required_role,
        )


# ── Membership: authority is granted, never taken ───────────────────────────


def test_the_first_membership_needs_no_granter(database: Database, other_project: Project) -> None:
    """A project with nobody in it cannot require somebody's permission.

    Without this exception no project could ever be given its first member, so
    the rule is "the first one is free" rather than "always name a granter".
    """
    with database.transaction() as session:
        membership = MembershipRepository(session, other_project.project_id).grant(
            user_id=_a_user(database, "first-owner"), role=UserRole.PROJECT_OWNER
        )

    assert membership.role is UserRole.PROJECT_OWNER


def test_a_second_membership_cannot_be_created_without_naming_a_granter(
    database: Database, project: Project
) -> None:
    """Otherwise anyone could reach power through a project that has members."""
    with pytest.raises(PermissionError, match="only the first"), (
        database.transaction()
    ) as session:
        MembershipRepository(session, project.project_id).grant(
            user_id=_a_user(database, "uninvited"), role=UserRole.PROJECT_OWNER
        )


def test_a_stranger_cannot_grant_authority(database: Database, project: Project) -> None:
    """A identifier with no membership is not a granter."""
    with pytest.raises(PermissionError, match="no membership"), (
        database.transaction()
    ) as session:
        MembershipRepository(session, project.project_id).grant(
            user_id=_a_user(database, "newcomer"),
            role=UserRole.LAB_USER,
            granted_by="someone-with-no-membership",
        )


def test_a_lab_user_cannot_promote_itself_to_owner(database: Database, project: Project) -> None:
    """The escalation that the equality check exists to stop."""
    owner = _owner_id(database)
    lab_user = _a_user(database, "lab-user")
    with database.transaction() as session:
        MembershipRepository(session, project.project_id).grant(
            user_id=lab_user, role=UserRole.LAB_USER, granted_by=owner
        )

    with pytest.raises(PermissionError, match="cannot confer authority they do not hold"), (
        database.transaction()
    ) as session:
        MembershipRepository(session, project.project_id).grant(
            user_id=lab_user, role=UserRole.PROJECT_OWNER, granted_by=lab_user
        )


def test_an_owner_may_grant_what_it_holds(database: Database, project: Project) -> None:
    """The rule is a ceiling, not a prohibition: a refusal here would be a bug."""
    owner = _owner_id(database)
    with database.transaction() as session:
        membership = MembershipRepository(session, project.project_id).grant(
            user_id=_a_user(database, "second-owner"),
            role=UserRole.PROJECT_OWNER,
            granted_by=owner,
        )

    assert membership.granted_by == owner


def test_an_owner_may_confer_the_operational_role(
    database: Database, project: Project
) -> None:
    """`ADMIN` is the one role the ranking would refuse and the check allows.

    The ranking exists so authority cannot be *escalated*: a user who holds
    less may not confer more. Administering a runtime is a different authority
    from directing research — `may_direct_project` excludes it — so an owner
    handing it over parts with nothing they had, and the check that governs
    managing members is that method rather than the comparison. What the
    comparison still refuses is a lab user reaching for a rank above its own,
    which the test above pins.
    """
    owner = _owner_id(database)
    with database.transaction() as session:
        membership = MembershipRepository(session, project.project_id).grant(
            user_id=_a_user(database, "operator"),
            role=UserRole.ADMIN,
            granted_by=owner,
        )

    assert membership.role is UserRole.ADMIN
    assert membership.may_direct_project is False, (
        "the operational role was conferred and it must not carry the scientific one"
    )


# ── Membership: a role is withdrawn, never deleted or edited ────────────────


def _granted(database: Database, project: Project, username: str, role: UserRole) -> str:
    """Account and live membership, granted by the fixture's owner."""
    user_id = _a_user(database, username)
    with database.transaction() as session:
        MembershipRepository(session, project.project_id).grant(
            user_id=user_id, role=role, granted_by=_owner_id(database)
        )
    return user_id


def test_a_withdrawn_membership_is_history_and_not_a_deletion(
    database: Database, project: Project
) -> None:
    """The row stays, the authority goes, and both are readable afterwards.

    Deleting the row would make "this person was in the project and was
    removed" indistinguishable from "this person was never here", which is the
    first question an inquiry into a project's decisions asks. So the read that
    authorization is made from returns nothing, and the read that answers the
    other question returns the row with the name of whoever withdrew it.
    """
    owner = _owner_id(database)
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with database.transaction() as session:
        repository = MembershipRepository(session, project.project_id)
        withdrawn = repository.revoke(lab_user, revoked_by=owner)

    assert withdrawn.revoked_by == owner
    assert withdrawn.revoked_at is not None
    assert withdrawn.is_active is False
    assert withdrawn.role is UserRole.LAB_USER, "the role they held is still on the row"

    with database.read_only() as session:
        repository = MembershipRepository(session, project.project_id)
        assert repository.for_user(lab_user) is None, (
            "a withdrawn membership is still answering the question authorization asks"
        )
        assert [membership.role for membership in repository.active()] == [
            UserRole.PROJECT_OWNER
        ]
        history = repository.history_for_user(lab_user)
    assert [membership.is_active for membership in history] == [False]


def test_a_withdrawal_is_on_the_record(database: Database, project: Project) -> None:
    """Membership is where authority comes from, so both directions of it are
    events: an authority that appeared or vanished with nothing in the stream
    would be one an inquiry could not reconstruct."""
    owner = _owner_id(database)
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            lab_user, revoked_by=owner
        )

    with database.read_only() as session:
        events = events_since(session, project.project_id)

    assert [event.event_type for event in events] == [
        ProjectEventType.PROJECT_CREATED,
        # The fixture's owner, then the lab user this test granted.
        ProjectEventType.MEMBER_ADDED,
        ProjectEventType.MEMBER_ADDED,
        ProjectEventType.MEMBER_REVOKED,
    ]
    added, revoked = events[2], events[3]
    assert added.payload == {
        "membership_id": revoked.payload["membership_id"],
        "user_id": lab_user,
        "role": UserRole.LAB_USER.value,
    }
    assert revoked.actor_type is ActorType.USER
    assert revoked.actor_id == owner, "the record does not say who withdrew it"


def test_a_lab_user_cannot_withdraw_a_membership(database: Database, project: Project) -> None:
    """Administering set-up is not directing the project, and the check is the
    one that draws that line rather than the ranking — a lab user outranks
    nobody, and the refusal has to say so for the right reason."""
    owner = _owner_id(database)
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with pytest.raises(PermissionError, match="managing membership"), (
        database.transaction()
    ) as session:
        MembershipRepository(session, project.project_id).revoke(
            owner, revoked_by=lab_user
        )


def test_an_administrator_cannot_withdraw_a_membership(
    database: Database, project: Project
) -> None:
    """The case the rank comparison would have got wrong.

    `ADMIN` outranks `PROJECT_OWNER` in the domain's ordering, so a check
    written as "at least as much authority as the role you are touching" would
    let an administrator remove the owner of a project it does not direct. The
    authority to run a runtime is not the authority to decide who is in a
    project.
    """
    admin = _granted(database, project, "operator", UserRole.ADMIN)

    with pytest.raises(PermissionError, match="managing membership"), (
        database.transaction()
    ) as session:
        MembershipRepository(session, project.project_id).revoke(
            _owner_id(database), revoked_by=admin
        )


def test_a_project_keeps_its_last_owner(database: Database, project: Project) -> None:
    """Nothing in RAVEL can repair a project nobody may direct.

    Memberships are granted by owners, so an owner who removes the last one
    leaves a project that no request can open — there is no operator-side
    recovery to fall back on. A second owner is what makes the first one's
    withdrawal a handover rather than an ending, so both halves are asserted.
    """
    owner = _owner_id(database)
    second = _granted(database, project, "second-owner", UserRole.PROJECT_OWNER)

    # A handover: with two owners, either may step down and the project still
    # has one.
    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            owner, revoked_by=second
        )

    with pytest.raises(PermissionError, match="last owner"), (
        database.transaction()
    ) as session:
        MembershipRepository(session, project.project_id).revoke(
            second, revoked_by=second
        )

    with database.read_only() as session:
        owners = MembershipRepository(session, project.project_id).owners()
    assert [membership.user_id for membership in owners] == [second], (
        "the refusal did not leave the project with the owner it had"
    )


def test_a_withdrawn_user_is_refused_on_the_next_request(
    database: Database, project: Project
) -> None:
    """Authority is re-read, so a withdrawal does not wait for a token to expire.

    This is the claim the domain's own docstring makes, and it is a claim about
    the repository rather than about tokens: `for_user` returns live rows, so
    the very next authorization decision sees the withdrawal. A cached
    membership would keep answering until something else invalidated it, and
    what invalidates a cache is exactly what an emergency removal cannot wait
    for.
    """
    owner = _owner_id(database)
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with database.read_only() as session:
        assert MembershipRepository(session, project.project_id).for_user(
            lab_user
        ) is not None

    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            lab_user, revoked_by=owner
        )

    with database.read_only() as session:
        assert MembershipRepository(session, project.project_id).for_user(
            lab_user
        ) is None
        assert (
            MembershipRepository(session, project.project_id).may_direct(lab_user)
            is False
        )


def test_a_withdrawn_membership_is_not_a_project_the_user_belongs_to(
    database: Database, project: Project
) -> None:
    """The read the project list is built from, which has to mean *live* too.

    `memberships_of` answers a different question from `for_user` — not "may
    this user do this here" but "which projects may this user open at all" —
    and it is what the Gateway's project list and `/auth/me` are both assembled
    from. It filtered on the user and not on the revocation, and that was
    correct only for as long as no membership could be withdrawn: with
    withdrawal it offers a client a project whose every other route refuses it,
    which is a door handed over that cannot be walked through.

    Asserted against the owner's own list as well, because the cheap way to make
    the first half pass is to filter away everything.
    """
    owner = _owner_id(database)
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with database.read_only() as session:
        assert [m.project_id for m in memberships_of(session, lab_user)] == [
            project.project_id
        ]

    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            lab_user, revoked_by=owner
        )

    with database.read_only() as session:
        assert memberships_of(session, lab_user) == []
        assert [m.project_id for m in memberships_of(session, owner)] == [
            project.project_id
        ], "the withdrawal took somebody else's membership with it"


def test_a_withdrawn_user_may_be_granted_again(
    database: Database, project: Project
) -> None:
    """The partial index is what makes the second grant possible, and it is the
    reason the constraint had to become partial rather than the rows being
    deleted: (project, user) is unique among *live* memberships."""
    owner = _owner_id(database)
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            lab_user, revoked_by=owner
        )

    with database.transaction() as session:
        again = MembershipRepository(session, project.project_id).grant(
            user_id=lab_user, role=UserRole.LAB_USER, granted_by=owner
        )

    with database.read_only() as session:
        repository = MembershipRepository(session, project.project_id)
        live = repository.for_user(lab_user)
        history = repository.history_for_user(lab_user)

    assert live is not None, "the second grant is not in force"
    assert live.membership_id == again.membership_id
    assert len(history) == 2, "the first grant is history, not something to overwrite"
    assert [membership.is_active for membership in history] == [False, True]


def test_a_live_membership_is_refused_rather_than_edited(
    database: Database, project: Project
) -> None:
    """A role change is a withdrawal and a grant, so that both are on the record.

    An `UPDATE` of `role` would leave the `MEMBER_ADDED` event describing a
    grant that is no longer anywhere on the record — which is the reason the
    column is in the identity trigger's list as well.
    """
    owner = _owner_id(database)
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with pytest.raises(PermissionError, match="withdrawing it and granting"), (
        database.transaction()
    ) as session:
        MembershipRepository(session, project.project_id).grant(
            user_id=lab_user, role=UserRole.PROJECT_OWNER, granted_by=owner
        )


def test_a_membership_that_is_not_there_cannot_be_withdrawn(
    database: Database, project: Project
) -> None:
    """A withdrawal is a change to something that exists, and the answer for
    "there is nothing here to change" is that, rather than a quiet success."""
    with pytest.raises(NotFound), database.transaction() as session:
        MembershipRepository(session, project.project_id).revoke(
            _a_user(database, "stranger"), revoked_by=_owner_id(database)
        )


def test_a_membership_cannot_be_deleted(database: Database, project: Project) -> None:
    """The guard that makes "withdrawn, never deleted" a property of the
    database rather than of the code that happens to write it today."""
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with pytest.raises(IntegrityError, match="never deleted"), (
        database.transaction()
    ) as session:
        session.execute(
            delete(ProjectMembershipRow).where(
                ProjectMembershipRow.user_id == lab_user,
                ProjectMembershipRow.project_id == project.project_id,
            )
        )


def test_a_role_cannot_be_edited_in_place(database: Database, project: Project) -> None:
    """The identity trigger's half of the same rule: only `revoked_at` and
    `revoked_by` may move, so a role change has to be the two events."""
    lab_user = _granted(database, project, "lab-user", UserRole.LAB_USER)

    with pytest.raises(IntegrityError, match="immutable"), (
        database.transaction()
    ) as session:
        session.execute(
            update(ProjectMembershipRow)
            .where(ProjectMembershipRow.user_id == lab_user)
            .values(role=UserRole.PROJECT_OWNER.value)
        )


# ── Approvals: only a person with standing may answer ───────────────────────


@pytest.mark.parametrize("role", NON_MASTER, ids=lambda role: role.value)
def test_only_master_raises_an_approval_request(
    database: Database, project: Project, role: AgentRole
) -> None:
    """Asking a human to authorize an action is part of deciding to take it."""
    with pytest.raises(PermissionError, match="may not raise an approval request"), (
        database.transaction()
    ) as session:
        ApprovalRepository(session, project.project_id).request(
            requested_by=role.value, action="Do the thing.", role=role
        )


def test_an_agent_cannot_answer_the_question_it_asked(
    database: Database, project: Project
) -> None:
    """The failure the check exists for, stated as the attack.

    `resolved_by` is a plain string on the row. Left unchecked, the Master agent
    would resolve its own request by naming a human who never saw it — and the
    record would say the project owner approved.
    """
    approval = _an_approval(database, project, required_role=UserRole.PROJECT_OWNER)

    with pytest.raises(PermissionError, match="is not a member"), (
        database.transaction()
    ) as session:
        ApprovalRepository(session, project.project_id).resolve(
            approval.approval_id,
            ApprovalStatus.APPROVED,
            resolved_by="master",
            note="Approved by the human, allegedly.",
        )


def test_a_member_without_the_required_authority_cannot_answer(
    database: Database, project: Project
) -> None:
    """Standing in the project is not the same as holding the authority asked for."""
    approval = _an_approval(database, project, required_role=UserRole.PROJECT_OWNER)
    lab_user = _a_user(database, "lab-user")
    with database.transaction() as session:
        MembershipRepository(session, project.project_id).grant(
            user_id=lab_user, role=UserRole.LAB_USER, granted_by=_owner_id(database)
        )

    with pytest.raises(PermissionError, match="requires PROJECT_OWNER"), (
        database.transaction()
    ) as session:
        ApprovalRepository(session, project.project_id).resolve(
            approval.approval_id, ApprovalStatus.APPROVED, resolved_by=lab_user
        )


def test_an_answer_from_the_owner_is_recorded(database: Database, project: Project) -> None:
    """And the identifier on the row is the one that was checked."""
    approval = _an_approval(database, project, required_role=UserRole.PROJECT_OWNER)
    owner = _owner_id(database)

    with database.transaction() as session:
        resolved = ApprovalRepository(session, project.project_id).resolve(
            approval.approval_id,
            ApprovalStatus.REJECTED,
            resolved_by=owner,
            note="Not with the current evidence.",
        )

    assert resolved.status is ApprovalStatus.REJECTED
    assert resolved.resolved_by == owner
    assert resolved.is_pending is False
    with database.read_only() as session:
        stored = ApprovalRepository(session, project.project_id).get(
            approval_id=approval.approval_id
        )
    assert stored.status is ApprovalStatus.REJECTED
    assert stored.resolved_by == owner


def test_an_approval_cannot_be_answered_twice(database: Database, project: Project) -> None:
    """A second answer is a second decision, and there is no method for one."""
    approval = _an_approval(database, project, required_role=UserRole.PROJECT_OWNER)
    owner = _owner_id(database)
    with database.transaction() as session:
        ApprovalRepository(session, project.project_id).resolve(
            approval.approval_id, ApprovalStatus.APPROVED, resolved_by=owner
        )

    with pytest.raises(ValueError, match="already APPROVED"), (
        database.transaction()
    ) as session:
        ApprovalRepository(session, project.project_id).resolve(
            approval.approval_id, ApprovalStatus.REJECTED, resolved_by=owner
        )


def test_an_approval_is_answered_against_the_project_that_asked(
    database: Database, project: Project, other_project: Project
) -> None:
    """Standing is per project, so an owner elsewhere is nobody here."""
    approval = _an_approval(database, project, required_role=UserRole.PROJECT_OWNER)
    other_owner = _a_user(database, "other-owner")
    with database.transaction() as session:
        MembershipRepository(session, other_project.project_id).grant(
            user_id=other_owner, role=UserRole.PROJECT_OWNER
        )

    with pytest.raises(PermissionError, match="is not a member"), (
        database.transaction()
    ) as session:
        ApprovalRepository(session, project.project_id).resolve(
            approval.approval_id, ApprovalStatus.APPROVED, resolved_by=other_owner
        )
