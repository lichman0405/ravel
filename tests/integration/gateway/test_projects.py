"""Reading a project over HTTP, and the boundary that decides who may.

Two properties carry most of the weight here:

**A project a caller is not in does not exist as far as they are concerned.**
The answer for somebody else's project and for a project that was never
created are the same status and the same body. Anything else confirms that a
project exists, which is a fact about research the caller was not admitted to.

**Reading is all any of these routes do.** The DAG, the decisions, the reviews
and the evidence are all served, and none of them can be written over HTTP by
anybody. The module is checked for the mutation service by import, and the
nodes are read back out of PostgreSQL afterwards — a route that changed one
would have to fail both checks.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import a_project, account, bearer, sign_in
from tests.support.routes import EXPECTED_ROUTE_FLOOR, effective_routes, probe_path

from ravel.domain.dag import DagNode, JoinPolicy
from ravel.domain.enums import NodeType, UserRole
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.gateway.auth.tokens import TokenService
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository

pytestmark = pytest.mark.integration


def a_node(project_id: str, objective: str, **overrides: Any) -> DagNode:
    """One node, with a decision to cite.

    Created through `DagRepository.add_node` with the MASTER role rather than
    inserted, because that is the only way a node comes into existence and a
    test that bypassed it would be testing a DAG shape the system cannot
    produce.
    """
    defaults: dict[str, Any] = {
        "display_id": objective[:12],
        "project_id": project_id,
        "node_type": NodeType.RESEARCH,
        "objective": objective,
        "executor_role": AgentRole.RESEARCH,
        "created_by": "master",
    }
    merged = defaults | overrides
    if merged.get("dependencies") and "join_policy" not in merged:
        # A node with dependencies must state when it becomes ready; the domain
        # refuses one that leaves that undefined, so the helper supplies the
        # strictest reading rather than making every caller say it.
        merged["join_policy"] = JoinPolicy.ALL
    return DagNode(**merged)


def plant(database: Database, project: Project, *objectives: str) -> list[str]:
    """Put nodes in a project and return their identifiers."""
    with database.transaction() as session:
        repository = DagRepository(session, project.project_id)
        return [
            repository.add_node(
                a_node(project.project_id, objective),
                role=AgentRole.MASTER,
                decision_ref="dec-1",
            ).node_id
            for objective in objectives
        ]


# ── Listing ─────────────────────────────────────────────────────────────────


def test_a_caller_lists_only_the_projects_they_are_in(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """The list is built from the caller's memberships, not filtered afterwards."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")

    response = client.get("/projects", headers=bearer(pair["access_token"]))

    assert response.status_code == 200
    listed = response.json()
    assert [entry["project_id"] for entry in listed] == [project.project_id]
    assert listed[0]["role"] == "PROJECT_OWNER"
    assert listed[0]["title"] == project.title


def test_a_caller_in_no_project_lists_nothing(client: TestClient, database: Database) -> None:
    account(database, username="ada")
    pair = sign_in(client, "ada")

    response = client.get("/projects", headers=bearer(pair["access_token"]))

    assert response.status_code == 200
    assert response.json() == []


# ── The boundary ────────────────────────────────────────────────────────────


def test_a_foreign_project_and_a_missing_one_are_one_answer(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """The distinction is not made, in status or in what the body discloses.

    `other_project` genuinely exists and the caller is simply not in it;
    `no-such-project` does not exist at all. Both are answered identically.

    The bodies are compared after substituting the identifier each request
    asked about, because both echo it back and the caller already knows what
    they asked for. What must not differ is anything *else* — a title, an
    owner, a member count would each be a fact about research the caller was
    not admitted to. The two bodies are otherwise the same sentence, which is
    the check that neither route learned anything to say.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    foreign = client.get(f"/projects/{other_project.project_id}", headers=headers)
    missing = client.get("/projects/no-such-project", headers=headers)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == {
        "detail": f"no project {other_project.project_id!r}"
    }, "the refusal says something about a project that exists"
    assert foreign.json()["detail"].replace(other_project.project_id, "") == (
        missing.json()["detail"].replace("no-such-project", "")
    )
    assert other_project.title not in foreign.text
    assert project.project_id not in foreign.text


def test_every_read_of_a_foreign_project_is_refused_the_same_way(
    client: TestClient, database: Database, project: Project, other_project: Project
) -> None:
    """The boundary is on the router, so no route can be the one that forgot."""
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    for path in (
        "",
        "/dag",
        "/decisions",
        "/reviews",
        "/evidence",
        "/artifacts",
        "/executions",
        "/deviations",
        "/events",
    ):
        response = client.get(f"/projects/{other_project.project_id}{path}", headers=headers)
        assert response.status_code == 404, f"{path} answered {response.status_code}"


@pytest.mark.parametrize("role", list(UserRole))
def test_every_role_may_read_the_project_they_are_in(
    client: TestClient, database: Database, project: Project, role: UserRole
) -> None:
    """Reading is what the three roles have in common; doing is what separates them.

    Every role, because a role is a role in a project: an administrator is not
    a platform superuser and holds no standing here beyond the membership an
    owner gave them. What each may *do* is asserted in `test_members.py`.
    """
    account(database, username="ada", role=role, project=project)
    pair = sign_in(client, "ada")

    response = client.get(
        f"/projects/{project.project_id}", headers=bearer(pair["access_token"])
    )

    assert response.status_code == 200
    assert response.json()["role"] == role.value


def test_an_administrator_may_read_but_must_be_a_member(
    client: TestClient, database: Database, project: Project
) -> None:
    """Administering the runtime is not standing in a project, and the whole
    difference between them is a membership row.

    An owner may confer `ADMIN`, and that is the one place the domain's ranking
    is deliberately wider than the check it drives. The ranking exists so that
    authority cannot be *escalated* — a lab user promoting itself to owner —
    and an owner handing over the operational role is not that: `ADMIN` is
    excluded from `may_direct_project`, so the granter parts with a standing
    they never had and gains nothing. What the ranking still refuses is the
    user who holds less conferring more.

    What an administrator does not get is a place in somebody else's project.
    The second half is the one that matters: root administers a project of its
    own and is answered about this one exactly as a stranger is, because
    `ADMIN` is a role in a project like any other rather than a platform
    superuser.
    """
    owner = account(database, username="ada", role=UserRole.ADMIN, project=project)
    pair = sign_in(client, "ada")

    response = client.get(
        f"/projects/{project.project_id}", headers=bearer(pair["access_token"])
    )
    assert response.status_code == 200
    assert response.json()["role"] == "ADMIN"
    assert response.json()["project"]["project_id"] == project.project_id
    assert owner  # the identifier the grant wrote down

    fresh = a_project(database, title="Operations")
    account(database, username="root", role=UserRole.ADMIN, project=fresh)
    root = sign_in(client, "root")

    mine = client.get(
        f"/projects/{fresh.project_id}", headers=bearer(root["access_token"])
    )
    assert mine.status_code == 200
    assert mine.json()["role"] == "ADMIN"

    # Not a member of `project`, so it is not a project root can see — and the
    # answer is the same one a stranger gets, which is the point: membership is
    # what opens a project, not the role somebody holds elsewhere.
    theirs = client.get(
        f"/projects/{project.project_id}", headers=bearer(root["access_token"])
    )
    assert theirs.status_code == 404


# ── The projection ──────────────────────────────────────────────────────────


def test_the_projection_reports_the_project_and_how_far_it_has_got(
    client: TestClient, database: Database, project: Project
) -> None:
    """What the TUI opens on, assembled by the same reader Master recovers with."""
    plant(database, project, "Find a better dopant.")
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")

    response = client.get(
        f"/projects/{project.project_id}", headers=bearer(pair["access_token"])
    )

    assert response.status_code == 200
    body = response.json()
    assert body["project"]["project_id"] == project.project_id
    assert body["role"] == "PROJECT_OWNER"
    assert body["is_finished"] is False
    assert isinstance(body["last_event_seq"], int)
    assert body["last_event_seq"] > 0, "a project with a DAG has emitted events"


# ── The DAG ─────────────────────────────────────────────────────────────────


def test_the_dag_lists_every_node_and_what_it_depends_on(
    client: TestClient, database: Database, project: Project
) -> None:
    """Read-only, and complete enough to draw the graph from."""
    first, second = plant(database, project, "Measure the baseline.", "Compare candidates.")
    with database.transaction() as session:
        DagRepository(session, project.project_id).add_node(
            a_node(project.project_id, "Analyse both.", dependencies=(first, second)),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")

    response = client.get(
        f"/projects/{project.project_id}/dag", headers=bearer(pair["access_token"])
    )

    assert response.status_code == 200
    nodes = response.json()
    assert [node["objective"] for node in nodes] == [
        "Measure the baseline.",
        "Compare candidates.",
        "Analyse both.",
    ]
    analysis = nodes[2]
    assert analysis["dependencies"] == [first, second]
    assert analysis["status"] == "PLANNED"
    assert analysis["executor_role"] == AgentRole.RESEARCH.value


# ── The event stream ────────────────────────────────────────────────────────


def test_events_can_be_read_from_a_sequence(
    client: TestClient, database: Database, project: Project
) -> None:
    """What a reconnecting client asks for: everything it has not seen."""
    plant(database, project, "One.", "Two.")
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    everything = client.get(
        f"/projects/{project.project_id}/events", headers=headers
    ).json()["events"]
    assert len(everything) >= 3, "a project, two nodes, and their edges"

    half = client.get(
        f"/projects/{project.project_id}/events",
        headers=headers,
        params={"after_seq": everything[1]["seq"]},
    ).json()["events"]

    assert [event["seq"] for event in half] == [event["seq"] for event in everything[2:]]


def test_reading_events_reports_whether_more_remain(
    client: TestClient, database: Database, project: Project
) -> None:
    """So a client that is far behind knows to ask again rather than assume."""
    plant(database, project, *[f"Node {index}." for index in range(4)])
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    page = client.get(
        f"/projects/{project.project_id}/events",
        headers=headers,
        params={"after_seq": 0, "limit": 2},
    ).json()

    assert len(page["events"]) == 2
    assert page["more"] is True

    last = client.get(
        f"/projects/{project.project_id}/events",
        headers=headers,
        params={"after_seq": 10_000},
    ).json()
    assert last == {"events": [], "more": False}


# ── What cannot be written ──────────────────────────────────────────────────


def test_no_route_lets_a_user_change_the_dag(
    app: FastAPI, client: TestClient, database: Database, project: Project
) -> None:
    """The separation of powers, asserted by trying to break it.

    Every route the application declares is probed with an owner's token — the
    strongest user credential in the system — and the DAG is read back out of
    PostgreSQL afterwards. An owner may pause a project, answer an approval and
    change its Authority Envelope; none of those is a node.

    The routes are enumerated from the application rather than from a list
    written here, so a route added later is probed without anybody remembering
    to add it. That covers what exists; `test_the_gateway_cannot_reach_the_dag_
    mutation_service` covers what could be added.

    This test spent its whole life until now probing *nothing*. `app.routes`
    under FastAPI 0.141 does not flatten included routers, so the loop found no
    entry with a path and made no request, and the assertions below held
    trivially — which is why there are three of them about the probe itself
    before there are any about the DAG. See `tests/support/routes.py`.
    """
    (node_id,) = plant(database, project, "The only node.")
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    pair = sign_in(client, "ada")
    headers = bearer(pair["access_token"])

    routes = effective_routes(app)
    assert len(routes) >= EXPECTED_ROUTE_FLOOR, (
        f"enumerated only {len(routes)} routes, which means the walk is broken "
        "and this test is about to assert nothing"
    )

    probed: list[str] = []
    for route in routes:
        if not route.is_http:
            # A WebSocket is connected to, not requested. It reads events and
            # writes nothing, and the one write in the whole Gateway that is not
            # a route — Master's tools — is not reachable from a socket.
            continue
        # The stand-ins come from `ABSENT_PLACEHOLDERS` and the check that none
        # was left behind is inside `probe_path`, because this test's twin in
        # `tests/e2e/test_tui.py` enumerates the same application: a placeholder
        # taught here and not there is a route one probe stops covering, which
        # is how the e2e probe came to fail on the laboratory package route.
        path = probe_path(
            route.path,
            project_id=project.project_id,
            node_id=node_id,
            task_id=node_id,
        )

        for method in route.methods - {"HEAD", "OPTIONS"}:
            client.request(method, path, headers=headers, json={})
            probed.append(f"{method} {path}")

    # The probe reached the routes, not merely the documentation ones: a
    # project-scoped route is only served by the routers that carry the
    # interesting surface.
    assert any("/dag" in entry for entry in probed), probed
    assert any("lab/tasks" in entry for entry in probed), probed

    with database.read_only() as session:
        nodes = DagRepository(session, project.project_id).nodes()
    assert [node.node_id for node in nodes] == [node_id]
    assert nodes[0].objective == "The only node."
    assert nodes[0].status.value == "PLANNED"


def test_the_gateway_cannot_reach_the_dag_mutation_service() -> None:
    """Read from the source, because a probe only covers the routes that exist.

    The probed list above is a list somebody has to maintain. This is not: it
    asks whether any module under `ravel.gateway` names the service that writes
    nodes, and it fails on the import rather than on the effect.
    """
    from pathlib import Path

    gateway = Path(__file__).resolve().parents[3] / "src" / "ravel" / "gateway"
    offenders = [
        path.relative_to(gateway).as_posix()
        for path in gateway.rglob("*.py")
        if "DagMutationService" in path.read_text()
    ]

    assert offenders == [], f"the Gateway names the DAG mutation service: {offenders}"


def test_a_valid_token_is_not_itself_authority(
    client: TestClient, database: Database, tokens: TokenService, project: Project
) -> None:
    """A well-formed, unexpired token for a non-member reaches nothing.

    This is the property the per-request membership read exists for. The token
    is genuine — `/auth/me` accepts it and says who it is for — and it still
    cannot see the project, because the authority to see a project is a row in
    PostgreSQL and this caller has none.

    **This case is the absent half of that property, and there is a second one
    for the withdrawn half.** A membership that was never granted and one that
    was taken away are the same absence to this read, which is why the read is
    written as "the live row for this user" rather than as a check on anything
    the caller presents. The case that holds a token issued *before* a
    withdrawal is
    `test_members.py::test_an_owner_withdraws_a_role_and_it_stops_working_at_once`,
    and `tests/integration/state/test_identity.py` asserts the reads underneath
    both. Until Phase 11 there was no way to withdraw a membership at all —
    `KNOWN_LIMITATIONS.md` L-17 — so this docstring used to say that reading the
    row per request would make a future revocation immediate rather than making
    one immediate today.
    """
    user_id = account(database, username="ada")
    token, _grant = tokens.issue_access(user_id)

    assert client.get("/auth/me", headers=bearer(token)).status_code == 200
    assert (
        client.get(f"/projects/{project.project_id}", headers=bearer(token)).status_code == 404
    )
