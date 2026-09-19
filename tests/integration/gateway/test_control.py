"""What a person may change, and what the record says about it.

The controls `docs/08` gives an owner are three, and each of them leaves a
trace that can be read back: a status change is an event, an envelope change is
a new version, an approval answer is a row. Every test here checks the trace
rather than the response body, because the response body is the server's
account of what it did and the trace is what it did.

The refusals are half the subject. A lab user cannot pause a project, a
non-member cannot do anything, and an approval addressed to an owner cannot be
answered by a lab user — and in each case what is asserted is not just the
status code but that nothing was written.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import account, bearer, sign_in

from ravel.domain.enums import ApprovalStatus, ProjectStatus, UserRole
from ravel.domain.events import ProjectEventType
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.outbox import events_since
from ravel.state.repositories.contracts import AuthorityEnvelopeRepository
from ravel.state.repositories.identity import ApprovalRepository
from ravel.state.repositories.projects import ProjectRegistry

pytestmark = pytest.mark.integration


def _status(database: Database, project: Project) -> ProjectStatus:
    """The project's status, read fresh from PostgreSQL."""
    with database.read_only() as session:
        return ProjectRegistry(session).get(project.project_id).status


def _planned(database: Database, project: Project) -> None:
    """Move a project to `CONTRACT_DEFINED`, which is where pausing starts to be possible.

    `CREATED` has nowhere to pause from, so every test that pauses one has to
    get it past that first. Done through the registry rather than by writing
    the column, because the move is itself a transition and a fixture that
    bypassed the table would be setting up a state the system cannot reach.
    """
    with database.transaction() as session:
        ProjectRegistry(session).transition(
            project.project_id, ProjectStatus.CONTRACT_DEFINED, actor_id="master"
        )


def _status_changes(database: Database, project: Project) -> list[dict[str, Any]]:
    """Every status change the project has recorded, oldest first."""
    with database.read_only() as session:
        return [
            event.payload
            for event in events_since(session, project.project_id)
            if event.event_type is ProjectEventType.PROJECT_STATUS_CHANGED
        ]


# ── Pause and resume ────────────────────────────────────────────────────────


def test_an_owner_pauses_and_resumes_a_project_and_the_stream_says_so(
    client: TestClient, database: Database, project: Project
) -> None:
    """Both directions, with the record as the evidence.

    The project is moved to `CONTRACT_DEFINED` first because a project that has
    not been planned cannot be paused at all, and a control that appeared to
    work on one would be a control checking nothing. That move is itself a
    status change, so the baseline is taken after it.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    _planned(database, project)
    before = len(_status_changes(database, project))
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    paused = client.post(
        f"/projects/{project.project_id}/pause",
        headers=headers,
        json={"reason": "the budget needs looking at"},
    )
    assert paused.status_code == 200
    assert paused.json()["status"] == "PAUSED"
    assert _status(database, project) is ProjectStatus.PAUSED

    resumed = client.post(f"/projects/{project.project_id}/resume", headers=headers)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "EXECUTING"
    assert _status(database, project) is ProjectStatus.EXECUTING

    assert _status_changes(database, project)[before:] == [
        {"from": "CONTRACT_DEFINED", "to": "PAUSED", "reason": "the budget needs looking at"},
        {"from": "PAUSED", "to": "EXECUTING", "reason": None},
    ]


def test_pausing_records_who_did_it(
    client: TestClient, database: Database, project: Project
) -> None:
    """A status change names the person, not the system.

    `ActorType.USER` rather than the repository's `SYSTEM` default, because
    this one has an author and a later reader is entitled to know that a human
    stopped the project rather than that it stopped.
    """
    user_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    _planned(database, project)
    pair = sign_in(client, "ada")

    client.post(f"/projects/{project.project_id}/pause", headers=bearer(pair["access_token"]))

    with database.read_only() as session:
        changes = [
            event
            for event in events_since(session, project.project_id)
            if event.event_type is ProjectEventType.PROJECT_STATUS_CHANGED
            and event.actor_type.value == "USER"
        ]
    assert [(event.actor_type.value, event.actor_id) for event in changes] == [
        ("USER", user_id)
    ]


def test_pausing_twice_writes_once(
    client: TestClient, database: Database, project: Project
) -> None:
    """A retried pause is the same answer, and no second event.

    This is what makes the control safe to send over a network that may drop
    the response: a client that did not see the first one can ask again without
    putting a duplicate in the project's history.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    _planned(database, project)
    before = len(_status_changes(database, project))
    headers = bearer(sign_in(client, "ada")["access_token"])

    first = client.post(f"/projects/{project.project_id}/pause", headers=headers, json={})
    second = client.post(f"/projects/{project.project_id}/pause", headers=headers, json={})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert len(_status_changes(database, project)) == before + 1


def test_an_illegal_move_is_refused_by_the_transition_table(
    client: TestClient, database: Database, project: Project
) -> None:
    """A `CREATED` project can be neither paused nor resumed, and the domain says so.

    The route carries no list of pausable statuses; it asks the domain to make
    the move and reports the refusal as a conflict. Both directions are tried,
    because a route that had got one of them right by accident would look the
    same as one that had got both right on purpose.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    paused = client.post(f"/projects/{project.project_id}/pause", headers=headers, json={})
    resumed = client.post(f"/projects/{project.project_id}/resume", headers=headers)

    assert paused.status_code == 409
    assert resumed.status_code == 409
    assert _status(database, project) is ProjectStatus.CREATED
    assert _status_changes(database, project) == [], "a refused move is not an event"


def test_a_lab_user_cannot_pause_a_project(
    client: TestClient, database: Database, project: Project
) -> None:
    """Reading is shared; directing is not.

    The project is planned first so that the refusal is the *role* and not the
    status: a request that would have failed anyway proves nothing about who
    may make it.
    """
    account(database, username="sam", role=UserRole.LAB_USER, project=project)
    _planned(database, project)
    headers = bearer(sign_in(client, "sam")["access_token"])

    response = client.post(f"/projects/{project.project_id}/pause", headers=headers, json={})

    assert response.status_code == 403
    assert _status(database, project) is ProjectStatus.CONTRACT_DEFINED


def test_a_non_member_cannot_pause_a_project(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """Refused as though the project did not exist, and nothing is written."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(f"/projects/{other_project.project_id}/pause", headers=headers, json={})

    assert response.status_code == 404
    assert _status(database, other_project) is ProjectStatus.CREATED


# ── The Authority Envelope ──────────────────────────────────────────────────


def test_reading_an_envelope_that_was_never_set_is_null_not_missing(
    client: TestClient, database: Database, project: Project
) -> None:
    """A project with no envelope is a different thing from a project that is not there."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.get(f"/projects/{project.project_id}/envelope", headers=headers)

    assert response.status_code == 200
    assert response.json() is None


def test_setting_an_envelope_appends_a_version(
    client: TestClient, database: Database, project: Project
) -> None:
    """Two writes make version 1 and version 2, and the newest governs.

    Neither write is an edit: version 1 is still there afterwards, saying what
    was permitted before the change. That is the difference between an
    envelope and a setting.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    path = f"/projects/{project.project_id}/envelope"

    first = client.post(
        path,
        headers=headers,
        json={
            "requires_approval": [
                {
                    "action": "ADD_DAG_NODES_OVER_LIMIT",
                    "required_role": "PROJECT_OWNER",
                    "rationale": "a bigger graph is a bigger commitment",
                }
            ],
            "max_dag_nodes_without_approval": 12,
            "budget_time_limits": {"max_wall_clock_hours": 72},
        },
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1

    second = client.post(
        path, headers=headers, json={"max_dag_nodes_without_approval": 40}
    )
    assert second.status_code == 200
    assert second.json()["version"] == 2
    assert second.json()["requires_approval"] == [], "the whole envelope is sent, not a patch"

    with database.read_only() as session:
        repository = AuthorityEnvelopeRepository(session, project.project_id)
        assert repository.latest().version == 2
        assert repository.version(1).max_dag_nodes_without_approval == 12
        assert repository.version(1).requires_approval[0].action == "ADD_DAG_NODES_OVER_LIMIT"


def test_the_version_of_an_envelope_is_not_the_callers_to_choose(
    client: TestClient, database: Database, project: Project
) -> None:
    """A body that carries a version is ignored rather than obeyed."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    path = f"/projects/{project.project_id}/envelope"

    client.post(path, headers=headers, json={})
    response = client.post(path, headers=headers, json={"version": 99})

    assert response.status_code == 200
    assert response.json()["version"] == 2


def test_a_lab_user_cannot_change_the_envelope(
    client: TestClient, database: Database, project: Project
) -> None:
    """The envelope bounds Master, so only a director sets it."""
    account(database, username="sam", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "sam")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/envelope", headers=headers, json={}
    )

    assert response.status_code == 403
    with database.read_only() as session:
        assert AuthorityEnvelopeRepository(session, project.project_id).all() == []


def test_every_member_may_read_the_envelope(
    client: TestClient, database: Database, project: Project
) -> None:
    """Reading what bounds Master is not directing the research."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="sam", role=UserRole.LAB_USER, project=project)
    owner = bearer(sign_in(client, "ada")["access_token"])
    path = f"/projects/{project.project_id}/envelope"
    client.post(path, headers=owner, json={"max_dag_nodes_without_approval": 7})

    response = client.get(path, headers=bearer(sign_in(client, "sam")["access_token"]))

    assert response.status_code == 200
    assert response.json()["max_dag_nodes_without_approval"] == 7


# ── Approvals ───────────────────────────────────────────────────────────────


def _ask(
    database: Database,
    project: Project,
    *,
    action: str = "SPEND_OVER_BUDGET",
    required_role: UserRole = UserRole.PROJECT_OWNER,
) -> str:
    """Raise an approval request the way Master does, and return its identifier."""
    with database.transaction() as session:
        request = ApprovalRepository(session, project.project_id).request(
            requested_by="master",
            action=action,
            role=AgentRole.MASTER,
            rationale="this would spend past what the envelope allows",
            required_role=required_role,
        )
        return request.approval_id


def test_an_owner_answers_an_approval_and_the_answer_is_recorded(
    client: TestClient, database: Database, project: Project
) -> None:
    """The one place a user's decision enters the system, and it is a record."""
    user_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    approval_id = _ask(database, project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    listed = client.get(f"/projects/{project.project_id}/approvals", headers=headers)
    assert [item["approval_id"] for item in listed.json()] == [approval_id]

    answered = client.post(
        f"/projects/{project.project_id}/approvals/{approval_id}/resolve",
        headers=headers,
        json={"status": "APPROVED", "note": "one more run, then stop"},
    )

    assert answered.status_code == 200
    assert answered.json()["status"] == "APPROVED"
    assert answered.json()["resolved_by"] == user_id

    assert client.get(f"/projects/{project.project_id}/approvals", headers=headers).json() == []
    history = client.get(
        f"/projects/{project.project_id}/approvals",
        headers=headers,
        params={"pending_only": False},
    ).json()
    assert [item["status"] for item in history] == ["APPROVED"]


def test_an_approval_is_answered_by_the_caller_and_not_by_whoever_they_name(
    client: TestClient, database: Database, project: Project
) -> None:
    """A named resolver in the body would be a way to answer as somebody else.

    The body carries a `resolved_by` that names the owner; the owner answers it
    themselves, so the field is ignored and what is recorded is the caller.
    """
    owner_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    approval_id = _ask(database, project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/approvals/{approval_id}/resolve",
        headers=headers,
        json={"status": "APPROVED", "resolved_by": "somebody-else"},
    )

    assert response.status_code == 200
    assert response.json()["resolved_by"] == owner_id


def test_a_lab_user_cannot_answer_an_owners_approval(
    client: TestClient, database: Database, project: Project
) -> None:
    """The request says who may answer it, and the repository is what holds it."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="sam", role=UserRole.LAB_USER, project=project)
    approval_id = _ask(database, project)
    headers = bearer(sign_in(client, "sam")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/approvals/{approval_id}/resolve",
        headers=headers,
        json={"status": "APPROVED"},
    )

    assert response.status_code == 403
    with database.read_only() as session:
        still = ApprovalRepository(session, project.project_id).get(approval_id=approval_id)
    assert still.status is ApprovalStatus.PENDING


def test_a_lab_user_answers_the_approval_addressed_to_lab_users(
    client: TestClient, database: Database, project: Project
) -> None:
    """Which is the case a route-level role check would have got wrong.

    The authority is a property of the request, not of the route, so a lab user
    is the right person to answer a question addressed to lab users — and
    nothing in this module had to know that.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="sam", role=UserRole.LAB_USER, project=project)
    approval_id = _ask(database, project, required_role=UserRole.LAB_USER)
    headers = bearer(sign_in(client, "sam")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/approvals/{approval_id}/resolve",
        headers=headers,
        json={"status": "REJECTED", "note": "the sample would not survive that"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"


def test_pending_is_not_an_answer(
    client: TestClient, database: Database, project: Project
) -> None:
    """`PENDING` is the absence of an answer, and the schema says so.

    Whether a request may be answered is not something a client gets to assert:
    accepting `PENDING` would let one be un-answered and then answered again,
    which is two decisions where there was one.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    approval_id = _ask(database, project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/approvals/{approval_id}/resolve",
        headers=headers,
        json={"status": "PENDING"},
    )

    assert response.status_code == 422


def test_another_projects_approval_is_not_there(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """An identifier from elsewhere resolves nothing and discloses nothing."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    elsewhere = _ask(database, other_project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/approvals/{elsewhere}/resolve",
        headers=headers,
        json={"status": "APPROVED"},
    )

    assert response.status_code == 404
    with database.read_only() as session:
        assert (
            ApprovalRepository(session, other_project.project_id).get(
                approval_id=elsewhere
            ).status
            is ApprovalStatus.PENDING
        )
