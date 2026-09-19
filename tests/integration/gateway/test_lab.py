"""What the person at the bench can see, and the one thing they can say.

`docs/08` gives the lab user a screen of reads and a single write, and the write
is the interesting one: reporting that the plan and the bench disagreed. These
tests are mostly about what that report is *not* — it is not a permission, not a
node transition, and not an edit to the contract — because that is the property
the three-way separation of Execution, Review and Decision rests on. A lab user
who could widen their own contract would be deciding, and the decision belongs
to Master.

The contract *is* the instruction, which is why the tests read the approved
procedure out of the execution contract rather than looking for an instruction
of its own. A second table would be a second source of truth about what a worker
was told to do.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import Prepared
from tests.integration.gateway.conftest import account, bearer, sign_in

from ravel.domain.dag import executor_for
from ravel.domain.enums import NodeStatus, NodeType, UserRole
from ravel.domain.project import Project
from ravel.gateway.routes.lab import LAB_ROLE
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import DeviationRepository

pytestmark = pytest.mark.integration

REPORT = {
    "requested_action": "run_measurement",
    "description": "The furnace would not hold 900C, so the run was done at 850C.",
}


def test_the_lab_role_is_the_role_that_runs_experiments() -> None:
    """The assertion the route module defers to a test rather than making at import.

    `LAB_ROLE` is the whole definition of what appears on this screen, and it is
    a copy of a fact the domain already holds. If the domain ever assigned
    experiments to a different role, the screen would quietly stop showing them
    — no error, just an empty list — and this is the test that says so out loud
    instead.
    """
    assert executor_for(NodeType.EXPERIMENT) is LAB_ROLE


def test_a_lab_member_is_shown_their_task_and_the_contract_it_runs_under(
    client: TestClient,
    database: Database,
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """The approved procedure, the allowed actions and the required outputs.

    All three come out of the frozen execution contract. A lab user who has to
    ask what the allowed range was is a lab user who will guess.
    """
    prepared = prepare(node_type=NodeType.EXPERIMENT)
    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    listed = client.get(f"/projects/{project.project_id}/lab/tasks", headers=headers)
    assert listed.status_code == 200, listed.text
    tasks = listed.json()

    assert [task["node"]["node_id"] for task in tasks] == [prepared.node_id]
    instruction = tasks[0]["instruction"]
    assert instruction["objective"] == prepared.contract.objective
    assert instruction["allowed_actions"] == ["run_measurement"]
    assert instruction["required_outputs"] == list(prepared.contract.required_outputs)
    assert instruction["version"] == prepared.contract.version
    # Frozen, so the lab user can see that what they are reading is not going to
    # change under them mid-run.
    assert instruction["frozen_at"] is not None

    one = client.get(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}", headers=headers
    )
    assert one.status_code == 200
    assert one.json() == tasks[0]


def test_a_non_experiment_node_is_not_a_lab_task(
    client: TestClient,
    database: Database,
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """A computation is somebody else's work, and saying so is not a secret.

    The refusal is a 404 with a message that distinguishes "no such task" from
    "that is not an experiment", because a member who got the identifier right
    should not be sent looking for a typo.
    """
    computation = prepare(node_type=NodeType.COMPUTATION)
    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    listed = client.get(f"/projects/{project.project_id}/lab/tasks", headers=headers).json()
    assert listed == []

    refused = client.get(
        f"/projects/{project.project_id}/lab/tasks/{computation.node_id}", headers=headers
    )
    assert refused.status_code == 404
    assert "not an experiment" in refused.json()["detail"]

    missing = client.get(
        f"/projects/{project.project_id}/lab/tasks/no-such-node", headers=headers
    )
    assert missing.status_code == 404
    assert "no task" in missing.json()["detail"]


def test_a_task_with_no_contract_shows_no_instruction(
    client: TestClient, database: Database, project: Project
) -> None:
    """A planned node with no contract is a task nobody should start.

    `None` rather than an empty procedure, because those are different facts: an
    empty procedure reads as a task with nothing to do, and the truth is that
    nobody has written down what doing it means yet.
    """
    from ravel.domain.dag import DagNode
    from ravel.domain.roles import AgentRole

    with database.transaction() as session:
        node = DagRepository(session, project.project_id).add_node(
            DagNode.create(
                project_id=project.project_id,
                node_type=NodeType.EXPERIMENT,
                objective="Run the furnace series.",
                created_by="master",
            ),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )

    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    task = client.get(
        f"/projects/{project.project_id}/lab/tasks/{node.node_id}", headers=headers
    ).json()
    assert task["instruction"] is None
    assert task["node"]["node_id"] == node.node_id


# ── The one write ───────────────────────────────────────────────────────────


def test_reporting_a_deviation_records_it_against_the_frozen_contract(
    client: TestClient,
    database: Database,
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """Attributed to the person who raised it, and pointed at what it deviates from."""
    prepared = prepare(node_type=NodeType.EXPERIMENT)
    user_id = account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/deviations",
        headers=headers,
        json=REPORT,
    )

    assert response.status_code == 201, response.text
    recorded = response.json()
    assert recorded["requested_action"] == REPORT["requested_action"]
    assert recorded["description"] == REPORT["description"]
    assert recorded["raised_by"] == user_id
    assert recorded["execution_contract_ref"] == prepared.contract.contract_id
    assert recorded["node_id"] == prepared.node_id

    with database.read_only() as session:
        stored = DeviationRepository(session, project.project_id).all(
            node_id=prepared.node_id
        )
    assert [deviation.deviation_id for deviation in stored] == [recorded["deviation_id"]]


def test_a_deviation_report_permits_nothing_and_moves_nothing(
    client: TestClient,
    database: Database,
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """The report is an observation, and it stays one.

    Three things must not change: the deviation is not marked permitted, the
    node does not move, and the contract is not edited. The first is what makes
    this a report rather than an execution; the second and third are the
    separation of powers — a lab user who could widen their own contract, or
    release their own node, would be deciding, and deciding is Master's.
    """
    prepared = prepare(node_type=NodeType.EXPERIMENT)
    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    def snapshot() -> tuple[Any, ...]:
        with database.read_only() as session:
            from ravel.state.repositories.contracts import ExecutionContractRepository

            node = DagRepository(session, project.project_id).get(node_id=prepared.node_id)
            contract = ExecutionContractRepository(session, project.project_id).get(
                contract_id=prepared.contract.contract_id
            )
            return (
                node.status,
                node.execution_contract_ref,
                contract.version,
                contract.allowed_actions,
                contract.allowed_ranges,
                contract.frozen_at,
            )

    before = snapshot()

    recorded = client.post(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/deviations",
        headers=headers,
        json=REPORT,
    ).json()

    assert recorded["permitted"] is False
    assert recorded["resolved_by_decision_ref"] is None
    assert snapshot() == before


def test_a_deviation_needs_a_contract_to_deviate_from(
    client: TestClient, database: Database, project: Project
) -> None:
    """409, because there is no permitted list for the report to be a deviation from.

    A record written here would say that a document refused something, and there
    is no document.
    """
    from ravel.domain.dag import DagNode
    from ravel.domain.roles import AgentRole

    with database.transaction() as session:
        node = DagRepository(session, project.project_id).add_node(
            DagNode.create(
                project_id=project.project_id,
                node_type=NodeType.EXPERIMENT,
                objective="Run the furnace series.",
                created_by="master",
            ),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )

    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    refused = client.post(
        f"/projects/{project.project_id}/lab/tasks/{node.node_id}/deviations",
        headers=headers,
        json=REPORT,
    )

    assert refused.status_code == 409
    with database.read_only() as session:
        assert DeviationRepository(session, project.project_id).all() == []


def test_a_deviation_needs_something_to_have_been_asked_for(
    client: TestClient,
    database: Database,
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """An empty report is not a report."""
    prepared = prepare(node_type=NodeType.EXPERIMENT)
    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/deviations",
        headers=headers,
        json={"requested_action": "", "description": ""},
    )
    assert response.status_code == 422


# ── Who may do it ───────────────────────────────────────────────────────────


def test_a_member_of_another_project_cannot_see_or_report(
    client: TestClient,
    database: Database,
    project: Project,
    other_project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """404 rather than 403, and the upload of a report is refused too."""
    prepared = prepare(node_type=NodeType.EXPERIMENT)
    account(database, username="bob", role=UserRole.PROJECT_OWNER, project=other_project)
    headers = bearer(sign_in(client, "bob")["access_token"])

    assert (
        client.get(f"/projects/{project.project_id}/lab/tasks", headers=headers).status_code
        == 404
    )
    assert (
        client.post(
            f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/deviations",
            headers=headers,
            json=REPORT,
        ).status_code
        == 404
    )
    with database.read_only() as session:
        assert DeviationRepository(session, project.project_id).all() == []


def test_no_token_is_refused(
    client: TestClient, database: Database, project: Project
) -> None:
    assert client.get(f"/projects/{project.project_id}/lab/tasks").status_code == 401


def test_the_node_is_left_where_the_report_found_it(
    client: TestClient,
    database: Database,
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """A deviation from a node that has already run does not un-run it.

    Stated as its own case because the tempting implementation — mark the node
    BLOCKED so somebody looks at it — would be the Gateway making a scheduling
    decision. Master reads the record on the next turn, and until then the node
    is where the run left it.
    """
    prepared = prepare(node_type=NodeType.EXPERIMENT)
    account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    with database.transaction() as session:
        DagRepository(session, project.project_id).transition_node(
            prepared.node_id, NodeStatus.RUNNING, actor_id="experimental-worker"
        )

    client.post(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/deviations",
        headers=headers,
        json=REPORT,
    )

    with database.read_only() as session:
        node = DagRepository(session, project.project_id).get(node_id=prepared.node_id)
    assert node.status is NodeStatus.RUNNING
