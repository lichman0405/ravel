"""Talking to Master, and the two facts the transcript has to keep apart.

A question that was never answered and a question that was never asked are
different things about a project, and the design decision this module tests is
that the record can tell them apart: the question is committed before the turn
runs, and the answer only if there was one.

Master is scripted here, and that is not a shortcut around the interesting
part. What is under test is the Gateway's half — that the message is recorded,
that the turn is handed the project's real authoritative state, that the reply
is attributed to Master rather than to the person, and that a failed turn
leaves an unanswered question rather than silence. A test that needed a model
in the loop would be testing the model; the harness's own turns are covered by
`tests/dsh`, against a real pinned runtime.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import account, bearer, sign_in

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeType, UserRole
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.execution.loop import Situation
from ravel.gateway.app import create_app
from ravel.gateway.conversation import Answer
from ravel.state.database import Database
from ravel.state.outbox import events_since
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import MembershipRepository

pytestmark = pytest.mark.integration


@dataclass
class ScriptedMaster:
    """A Master that answers without a model, and records what it was told.

    Both of the things this test needs to check are about the *input* — that
    the situation handed over is the project's real state — so the script keeps
    what it saw rather than only what it said.
    """

    reply: str = "Nothing has been planned yet."
    completed: bool = True
    seen: list[tuple[Situation, str]] = field(default_factory=list)

    async def respond(self, situation: Situation, message: str) -> Answer:
        self.seen.append((situation, message))
        return Answer(text=self.reply, completed=self.completed)


@pytest.fixture
def master() -> ScriptedMaster:
    return ScriptedMaster()


@pytest.fixture
def client(database: Database, gateway_settings: Any, tokens: Any, master: ScriptedMaster):
    """A Gateway whose Master is scripted.

    Built by the real `create_app` rather than assembled here, so what the
    scripted Master replaces is exactly one collaborator and everything else —
    the routers, the dependencies, the error handlers, the database — is what a
    deployment runs.
    """
    app = create_app(
        settings=gateway_settings,
        database=database,
        tokens=tokens,
        master_of=lambda _project_id: master,
    )
    with TestClient(app) as open_client:
        yield open_client


def _planned(database: Database, project: Project, objective: str) -> str:
    """Put one node in the project, so the situation has something in it."""
    with database.transaction() as session:
        return (
            DagRepository(session, project.project_id)
            .add_node(
                DagNode(
                    display_id=objective[:12],
                    project_id=project.project_id,
                    node_type=NodeType.RESEARCH,
                    objective=objective,
                    executor_role=AgentRole.RESEARCH,
                    created_by="master",
                ),
                role=AgentRole.MASTER,
                decision_ref="dec-1",
            )
            .node_id
        )


# ── Saying something ────────────────────────────────────────────────────────


def test_an_owner_says_something_and_master_answers(
    client: TestClient, database: Database, project: Project, master: ScriptedMaster
) -> None:
    """One exchange, recorded as two messages sharing a turn."""
    user_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    master.reply = "The DAG is empty; I would start with a literature search."
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/messages",
        headers=headers,
        json={"body": "Where are we?"},
    )

    assert response.status_code == 200, response.text
    turn = response.json()
    assert turn["question"]["body"] == "Where are we?"
    assert turn["question"]["author_id"] == user_id
    assert turn["question"]["author_type"] == "USER"
    assert turn["answer"]["body"] == master.reply
    assert turn["answer"]["author_id"] == "master"
    assert turn["answer"]["author_type"] == "AGENT"
    assert turn["question"]["turn_id"] == turn["answer"]["turn_id"] == turn["turn_id"]

    transcript = client.get(
        f"/projects/{project.project_id}/messages", headers=headers
    ).json()
    assert [message["body"] for message in transcript] == ["Where are we?", master.reply]


def test_master_is_handed_the_projects_real_state(
    client: TestClient, database: Database, project: Project, master: ScriptedMaster
) -> None:
    """The turn's input is read from PostgreSQL, not from the session.

    The node was written by somebody else, before the request, and the scripted
    Master is given it anyway — which is the property that matters: a
    conversation turn begins from the record, so a session that has drifted
    from it cannot answer from its own memory.
    """
    node_id = _planned(database, project, "Measure the baseline.")
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    client.post(
        f"/projects/{project.project_id}/messages",
        headers=headers,
        json={"body": "What is running?"},
    )

    (situation, message) = master.seen[0]
    assert message == "What is running?"
    assert situation.project.project_id == project.project_id
    assert [node.node_id for node in situation.nodes] == [node_id]


def test_a_turn_that_produced_nothing_leaves_the_question_standing(
    client: TestClient, database: Database, project: Project, master: ScriptedMaster
) -> None:
    """An unanswered question is not the same as a question nobody asked.

    The turn did not complete, so there is no reply to record — and the
    transcript says so by holding one message rather than by holding two, one
    of which is empty. `answer` is `None` for the same reason.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    master.completed = False
    master.reply = ""
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/messages",
        headers=headers,
        json={"body": "Are you there?"},
    )

    assert response.status_code == 200
    assert response.json()["answer"] is None

    transcript = client.get(
        f"/projects/{project.project_id}/messages", headers=headers
    ).json()
    assert [message["body"] for message in transcript] == ["Are you there?"]
    assert transcript[0]["turn_id"] == response.json()["turn_id"]


def test_a_message_cannot_be_empty(
    client: TestClient, database: Database, project: Project
) -> None:
    """There is nothing to answer and nothing to record."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/messages", headers=headers, json={"body": ""}
    )

    assert response.status_code == 422


# ── Who may ─────────────────────────────────────────────────────────────────


def test_a_lab_user_may_read_the_conversation_and_not_add_to_it(
    client: TestClient, database: Database, project: Project, master: ScriptedMaster
) -> None:
    """What a person says to Master is how the research gets directed.

    So the transcript is every member's — a lab user is part of the project and
    can see what was decided — and speaking is the owner's alone. The refusal
    is checked together with the read, because "may not speak" would be a
    strange claim about a route a member cannot even see.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    account(database, username="sam", role=UserRole.LAB_USER, project=project)
    owner = bearer(sign_in(client, "ada")["access_token"])
    lab = bearer(sign_in(client, "sam")["access_token"])
    client.post(
        f"/projects/{project.project_id}/messages",
        headers=owner,
        json={"body": "Start with the literature."},
    )

    read = client.get(f"/projects/{project.project_id}/messages", headers=lab)
    refused = client.post(
        f"/projects/{project.project_id}/messages",
        headers=lab,
        json={"body": "Actually, start with the simulation."},
    )

    assert read.status_code == 200
    assert [message["body"] for message in read.json()] == [
        "Start with the literature.",
        master.reply,
    ]
    assert refused.status_code == 403
    assert len(master.seen) == 1, "a refused message still reached Master"


def test_a_non_member_cannot_read_or_speak(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """Both answers are the same 404 the rest of the surface gives."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    path = f"/projects/{other_project.project_id}/messages"

    assert client.get(path, headers=headers).status_code == 404
    assert client.post(path, headers=headers, json={"body": "hello"}).status_code == 404


def test_one_projects_conversation_is_not_anothers(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """The transcript is scoped like every other read.

    One account, a member of both projects, so what is being checked is the
    project boundary and not the membership one. The second membership is
    granted directly because `account` creates a user, and this test is about
    the same person standing in two places.
    """
    user_id = account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    client.post(
        f"/projects/{project.project_id}/messages",
        headers=headers,
        json={"body": "Only this project should see this."},
    )
    with database.transaction() as session:
        MembershipRepository(session, other_project.project_id).grant(
            user_id=user_id, role=UserRole.PROJECT_OWNER
        )

    elsewhere = client.get(
        f"/projects/{other_project.project_id}/messages", headers=headers
    ).json()

    assert elsewhere == []


# ── What a turn is not ──────────────────────────────────────────────────────


def test_a_conversation_turn_does_not_write_project_events(
    client: TestClient, database: Database, project: Project
) -> None:
    """Saying something is not a scientific act.

    The event vocabulary in `schemas/project_events.yaml` is closed and versioned,
    and none of its members means "somebody asked a question". Widening a
    specification artifact so an implementation has somewhere to write would be
    backwards; what Master *does* in a turn emits its own events through the
    tools, and a turn that only talked leaves the project exactly as it was.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    with database.read_only() as session:
        before = len(events_since(session, project.project_id))

    client.post(
        f"/projects/{project.project_id}/messages",
        headers=headers,
        json={"body": "Just thinking out loud."},
    )

    with database.read_only() as session:
        after = events_since(session, project.project_id)
    assert len(after) == before
