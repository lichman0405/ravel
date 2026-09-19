"""The operator's screen, and the honesty of what it reports.

`docs/08` closes the administrator's section with *Admin does not make scientific
decisions*, so the question these tests ask is not whether the routes work but
whether they tell the truth. Two answers matter more than the rest.

**A pool that was never built is not a pool with nothing in it.** `pool_started`
is `false` and the counts are `null` before anything has needed a runtime, and
that is a different fact from a live pool that has reaped everything — the first
says the Gateway has not reached the harness at all. A screen that rendered both
as zero would be telling an operator that a deployment they never configured is
healthy.

**A service that is down is reported, not raised.** The Temporal probe answers
`reachable: false` and the route still returns 200. A health endpoint that
returns 500 when the thing it reports on is broken is an endpoint that is only
readable when it has nothing to say, which is the opposite of when it is needed.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import PASSWORD, bearer, sign_in

from ravel.config import Settings
from ravel.domain.clock import utcnow
from ravel.domain.dag import DagNode
from ravel.domain.enums import FailureClass, JobState, NodeType, UserRole
from ravel.domain.execution import BackendJob
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.gateway.app import create_app
from ravel.gateway.auth.passwords import hash_password
from ravel.gateway.runtime import PIN_PATH, harness_home
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import BackendJobRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def administered(database: Database, clean: None) -> tuple[Project, str]:
    """A project whose only member is an administrator.

    Built as the project's *first* membership, which is the only way an ADMIN
    can arise: `MembershipRepository.grant` refuses to let anybody confer
    authority they do not hold, and an owner does not hold ADMIN. So the
    administrator here is the one who created the project, which is the
    bootstrap the rule leaves open on purpose.

    Committed before it is returned, like every other fixture that builds a
    project: the tests that use it open transactions of their own, and a
    project still inside this fixture's uncommitted transaction is a project
    they cannot see — which shows up as a foreign key violation rather than as
    a fixture bug.
    """
    with database.transaction() as session:
        admin = UserRepository(session).create(
            username="root", password_hash=hash_password(PASSWORD)
        )
        project = ProjectRegistry(session).create(
            title="Runtime", objective="Keep the machine running.", created_by=admin.user_id
        )
        MembershipRepository(session, project.project_id).grant(
            user_id=admin.user_id, role=UserRole.ADMIN
        )
        return project, admin.user_id


def _a_node(database: Database, project: Project, objective: str = "Measure it.") -> str:
    """One ordinary node, enough for the counts to have something to count."""
    with database.transaction() as session:
        node = DagRepository(session, project.project_id).add_node(
            DagNode.create(
                project_id=project.project_id,
                node_type=NodeType.COMPUTATION,
                objective=objective,
                created_by="master",
            ),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )
    return node.node_id


# ── Who may look ────────────────────────────────────────────────────────────


def test_an_owner_may_not_read_the_runtime_screen(
    client: TestClient, database: Database, project: Project
) -> None:
    """An administrator's authority is over the runtime, and it is a role.

    The owner of a project is the most powerful *user* credential there is and
    still not an administrator, which is the split `may_direct_project` draws:
    directing research and operating the estate are different jobs.
    """
    from tests.integration.gateway.conftest import account

    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    for path in ("runtime", "runtime/jobs/whatever"):
        refused = client.get(f"/projects/{project.project_id}/{path}", headers=headers)
        assert refused.status_code == 403, path


def test_an_administrator_reads_this_project_and_not_the_estate(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """Scoped to a project they are a member of, like every other project route.

    An administrator is not a superuser: a project they were never added to
    answers 404, the same as it would to a stranger. `docs/08` gives them
    "project runtime diagnostics", and a diagnostic about somebody else's
    research is not that.
    """
    project, _ = administered
    with database.transaction() as session:
        stranger_project = ProjectRegistry(session).create(
            title="Elsewhere", objective="Not this administrator's.", created_by="someone"
        )
    headers = bearer(sign_in(client, "root")["access_token"])

    assert (
        client.get(f"/projects/{project.project_id}/runtime", headers=headers).status_code == 200
    )
    assert (
        client.get(
            f"/projects/{stranger_project.project_id}/runtime", headers=headers
        ).status_code
        == 404
    )


# ── What the screen says ────────────────────────────────────────────────────


def test_the_runtime_screen_counts_what_is_there(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    project, _ = administered
    node_id = _a_node(database, project)
    _a_node(database, project, objective="And again.")
    with database.transaction() as session:
        BackendJobRepository(session, project.project_id).add(
            BackendJob(
                project_id=project.project_id,
                node_id=node_id,
                attempt=1,
                execution_contract_ref="c-1",
                execution_contract_version=1,
                backend="mock-compute",
                state=JobState.RUNNING,
            )
        )
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime", headers=headers).json()

    assert body["project_id"] == project.project_id
    assert body["nodes"] == {"total": 2, "by_status": {"PLANNED": 2}}
    assert body["backend_jobs"]["total"] == 1
    assert body["backend_jobs"]["unfinished"] == 1
    assert body["backend_jobs"]["by_state"] == {"RUNNING": 1}


def test_a_pool_that_was_never_built_is_not_a_pool_with_nothing_in_it(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """The distinction the whole screen turns on.

    Nothing in this test has needed a runtime, so the Gateway has never built a
    pool. Reporting `live_runtimes: 0` would say the harness is running and idle;
    the truth is that nobody has asked it for anything.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    harness = client.get(f"/projects/{project.project_id}/runtime", headers=headers).json()[
        "harness"
    ]

    assert harness["pool_started"] is False
    assert harness["live_runtimes"] is None
    assert harness["live_sessions"] is None
    assert harness["total_turns"] is None
    assert harness["scopes"] == []


def test_the_log_paths_are_named_and_not_created(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """A diagnostic may not create the thing it reports on.

    `Settings.dsh_home_path` makes the directory on the way to returning it,
    which would make `home_exists` true from the second read onwards — exactly
    when the answer has stopped being worth having. Resolving the path without
    creating it is what keeps this field a measurement.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    logs = client.get(f"/projects/{project.project_id}/runtime", headers=headers).json()["logs"]

    home = harness_home(client.app.state.ravel.settings)  # type: ignore[attr-defined]
    assert logs["dsh_home"] == str(home)
    assert logs["session_logs"] == str(home / "sessions")
    assert logs["runtime_directory"]


def test_the_harness_screen_reports_the_pin_that_is_checked_in(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """Read from `vendor/DSH_PIN.json` rather than repeated in the source.

    So that this screen and the test suite cannot disagree about which harness
    is running. `patches_required` is on it because it is the one field whose
    being `true` means the deployment is not the one that was verified.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    reported = client.get(
        f"/projects/{project.project_id}/runtime/harness", headers=headers
    ).json()
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))

    assert reported["name"] == pin["harness"]
    assert reported["tag"] == pin["pin"]["tag"]
    assert reported["commit"] == pin["pin"]["commit"]
    assert reported["patches_required"] is False
    assert isinstance(reported["home_exists"], bool)


def test_a_nodes_backend_attempts_are_readable_and_a_stranger_is_not(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """The retry history, which the node record does not keep.

    A node submitted to a backend four times looks from the outside exactly like
    a node that is slow, so this is the field an operator actually needs.
    """
    project, _ = administered
    node_id = _a_node(database, project)
    with database.transaction() as session:
        for attempt in (1, 2):
            BackendJobRepository(session, project.project_id).add(
                BackendJob(
                    project_id=project.project_id,
                    node_id=node_id,
                    attempt=attempt,
                    execution_contract_ref="c-1",
                    execution_contract_version=1,
                    backend="mock-compute",
                    state=JobState.FAILED if attempt == 1 else JobState.RUNNING,
                    failure_class=FailureClass.INFRA_RETRYABLE if attempt == 1 else None,
                    # A job that has ended has ended at some point, and the
                    # record refuses to be built without one.
                    ended_at=utcnow() if attempt == 1 else None,
                )
            )
    headers = bearer(sign_in(client, "root")["access_token"])

    jobs = client.get(
        f"/projects/{project.project_id}/runtime/jobs/{node_id}", headers=headers
    ).json()
    assert [job["attempt"] for job in jobs] == [1, 2]
    assert jobs[0]["failure_class"] == "INFRA_RETRYABLE"

    missing = client.get(
        f"/projects/{project.project_id}/runtime/jobs/no-such-node", headers=headers
    )
    assert missing.status_code == 404


def test_no_token_is_refused(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    project, _ = administered
    assert client.get(f"/projects/{project.project_id}/runtime").status_code == 401
    assert (
        client.get(f"/projects/{project.project_id}/runtime/temporal").status_code == 401
    )


# ── Temporal, reachable and not ─────────────────────────────────────────────


def test_the_temporal_probe_says_what_it_found(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """A project member may ask, not only an administrator.

    Whether the estate's queue is up is a fact about the deployment, and a
    member waiting on a node that has not moved is one of the people most
    entitled to it. What it does not reveal is anything about another project —
    the probe names none and reads no record.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(
        f"/projects/{project.project_id}/runtime/temporal", headers=headers
    ).json()

    assert body["host"]
    assert body["namespace"]
    assert body["task_queue"]
    assert isinstance(body["reachable"], bool)
    assert body["detail"]


def test_a_temporal_that_is_not_listening_is_reported_rather_than_raised(
    database: Database, administered: tuple[Project, str], gateway_settings: Settings
) -> None:
    """The estate whose Temporal is stopped is the estate whose Gateway must answer.

    Pointed at a port nothing is on, which is the failure an operator is most
    likely to be looking at. The route must return 200 with `reachable: false`:
    a 500 here would mean the health screen is unreadable precisely when it is
    needed, and the operator would be left with a broken endpoint instead of a
    diagnosis.
    """
    project, _ = administered
    settings = gateway_settings.model_copy(deep=True)
    settings.temporal_host = "127.0.0.1:1"
    app = create_app(settings=settings, database=database)

    with TestClient(app) as isolated:
        headers = bearer(sign_in(isolated, "root")["access_token"])
        response = isolated.get(
            f"/projects/{project.project_id}/runtime/temporal", headers=headers
        )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reachable"] is False
    assert "127.0.0.1:1" in body["host"]
