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

from ravel.domain.enums import ApprovalStatus, UserRole
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.identity import (
    ApprovalRepository,
    MembershipRepository,
    UserRepository,
)

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
