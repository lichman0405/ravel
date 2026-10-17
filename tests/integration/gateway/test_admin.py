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
import socket
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from tests.integration.gateway.conftest import PASSWORD, bearer, sign_in

from ravel.config import Settings
from ravel.domain.clock import utcnow
from ravel.domain.dag import DagNode
from ravel.domain.enums import FailureClass, JobState, NodeStatus, NodeType, UserRole
from ravel.domain.execution import BackendJob
from ravel.domain.project import Project
from ravel.domain.reconciliation import (
    RunFailureClass,
    RunReconciliation,
    WorkflowLiveness,
)
from ravel.domain.roles import AgentRole
from ravel.domain.services import GATEWAY, SERVICE_NAMES, SUPERVISOR, TEMPORAL_WORKER
from ravel.gateway.app import create_app
from ravel.gateway.auth.passwords import hash_password
from ravel.gateway.routes.admin import MINIMUM_SILENCE_SECONDS, MISSED_BEATS, UNFINISHED
from ravel.gateway.runtime import PIN_PATH, harness_home
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.reconciliation import RunReconciliationRepository
from ravel.state.repositories.records import BackendJobRepository
from ravel.state.repositories.services import ServiceRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def administered(database: Database, clean: None) -> tuple[Project, str]:
    """A project whose only member is an administrator.

    Built as the project's *first* membership, which is the one that names no
    granter. That is not the only way an administrator can arise — an owner may
    confer `ADMIN`, since `may_confer` allows a director to hand out an
    operational role they do not hold — but it is the way that needs no second
    account, and it is the bootstrap an operator uses on a fresh deployment.

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


def _report_to(
    database: Database,
    service: str,
    *,
    minutes_ago: int = 0,
    **detail: object,
) -> None:
    """A service's beat, written into its own committed transaction.

    `minutes_ago` moves the heartbeat into the past rather than the `started_at`
    alone, because the route judges silence by the beat — a report written now
    that claimed to have started an hour ago is a process that just restarted,
    which is a different thing from one that stopped answering.
    """
    beat = utcnow() - timedelta(minutes=minutes_ago)
    with database.transaction() as session:
        ServiceRepository(session).report(
            service, started_at=beat - timedelta(seconds=1), detail=dict(detail)
        )
        if minutes_ago:
            session.execute(
                text(
                    "UPDATE runtime_services SET heartbeat_at = :beat WHERE service = :service"
                ),
                {"beat": beat, "service": service},
            )


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


# ── The processes ───────────────────────────────────────────────────────────


def test_a_service_that_never_reported_is_not_a_service_that_died(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """The panel's central distinction, and the reason its answer is trusted.

    Every named service is in the answer whether or not it has ever written a
    row, because a deployment that has not started a supervisor has not lost
    one. `reporting: false` and `stale: false` together say exactly that; a
    screen that rendered both absences as a failure would send an operator
    looking for a crash that never happened.

    **The Gateway is the one service this request proves is up**, since Phase
    11: the application under test beats from its own lifespan, so serving this
    answer is itself the evidence that the `gateway` row is fresh. That is not
    a hole in the test — it is the test asserting the thing it can know. The
    other two are asserted absent, and the Gateway is asserted present, from
    the same read.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/services", headers=headers).json()

    assert [entry["service"] for entry in body["services"]] == list(SERVICE_NAMES)
    absent = [entry for entry in body["services"] if entry["service"] != GATEWAY]
    assert {entry["service"] for entry in absent} == {SUPERVISOR, TEMPORAL_WORKER}
    for entry in absent:
        assert entry["reporting"] is False
        assert entry["stale"] is False
        assert entry["instance"] is None
        assert entry["heartbeat_at"] is None
        assert entry["silent_for_seconds"] is None
        assert entry["holds_this_project"] is False
        assert entry["projects_held"] == 0
        assert entry["silence_budget_seconds"] == MINIMUM_SILENCE_SECONDS

    serving = {entry["service"]: entry for entry in body["services"]}[GATEWAY]
    assert serving["reporting"] is True
    assert serving["stale"] is False
    assert serving["stopped"] is False
    assert serving["stopped_at"] is None
    assert serving["instance"].startswith(socket.gethostname())


def test_a_reporting_service_says_how_long_it_has_been_quiet(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """The age is computed here, from a clock this process owns.

    What is not taken on trust is the service's word for its own health: a
    report is evidence that something beat, and how long ago it did is
    arithmetic this process does against its own clock rather than a number the
    reporting process supplied.
    """
    project, _ = administered
    _report_to(database, SUPERVISOR, poll_seconds=20, projects=[project.project_id])
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/services", headers=headers).json()
    reported = {entry["service"]: entry for entry in body["services"]}[SUPERVISOR]

    assert reported["reporting"] is True
    assert reported["stale"] is False
    assert reported["silent_for_seconds"] < 5.0
    assert reported["instance"].startswith(socket.gethostname())
    # Three missed beats, from the cadence the service itself reported: a
    # supervisor held up by a slow reconciliation sweep must not be called dead,
    # and a service that polls slowly must not be called dead for polling at the
    # rate it was configured to.
    assert reported["silence_budget_seconds"] == MISSED_BEATS * 20
    assert reported["holds_this_project"] is True
    assert reported["projects_held"] == 1


def test_a_service_polling_faster_than_the_floor_gets_the_floor(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """The other branch of the budget, and the reason there is a floor at all.

    Three beats of a five-second poll is fifteen seconds, and fifteen seconds
    of silence is not a fault on any machine somebody is using — a supervisor
    held up by one slow query would be reported as dead. The floor is the
    conservative reading of an unknown cadence too: the shorter the budget, the
    sooner a silence is called a fault, and a false "stale" sends an operator to
    look at a process, which is the cheap mistake of the two.
    """
    project, _ = administered
    _report_to(database, SUPERVISOR, poll_seconds=5)
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/services", headers=headers).json()
    reported = {entry["service"]: entry for entry in body["services"]}[SUPERVISOR]

    assert reported["silence_budget_seconds"] == MINIMUM_SILENCE_SECONDS
    assert reported["silence_budget_seconds"] > MISSED_BEATS * 5


def test_a_report_with_no_cadence_in_it_gets_the_floor(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """`detail` is JSONB, which is to say it is whatever was stored.

    A report written by an older build, or by a process with a different idea
    of what to put in its detail, must not take the route down and must not
    make the budget infinite — an unparseable cadence is read as "unknown",
    which is the conservative answer.
    """
    project, _ = administered
    _report_to(database, SUPERVISOR, poll_seconds="every so often")
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/services", headers=headers).json()
    reported = {entry["service"]: entry for entry in body["services"]}[SUPERVISOR]

    assert reported["silence_budget_seconds"] == MINIMUM_SILENCE_SECONDS
    assert reported["holds_this_project"] is False
    assert reported["projects_held"] == 0


def test_a_service_that_stopped_beating_is_stale_and_named(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """A process that died leaves the row it wrote, and the row is the evidence.

    The heartbeat is written an hour into the past and the budget is thirty
    seconds, because the point is that the age is *measured* rather than
    believed — a report claiming to be healthy would be a report from a process
    that is not there to make it.
    """
    project, _ = administered
    _report_to(database, SUPERVISOR, minutes_ago=60)
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/services", headers=headers).json()
    reported = {entry["service"]: entry for entry in body["services"]}[SUPERVISOR]

    assert reported["reporting"] is True
    assert reported["stale"] is True
    assert reported["silent_for_seconds"] >= 3599.0
    assert reported["instance"], "a stale service must name the process that stopped"


def test_the_supervisors_disclosure_about_other_projects_is_a_count(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """A supervisor's report names every project it holds, and that is other
    people's research.

    The standing check that admits a caller here is a membership in *this*
    project, so what the answer may carry about the others is that they exist.
    `holds_this_project` is the only project-scoped field, and it is a boolean
    about the caller's own project rather than a list.
    """
    project, _ = administered
    with database.transaction() as session:
        elsewhere = ProjectRegistry(session).create(
            title="Elsewhere", objective="Not this caller's business.", created_by="someone"
        )
    _report_to(database, SUPERVISOR, projects=[project.project_id, elsewhere.project_id])
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/services", headers=headers).json()
    reported = {entry["service"]: entry for entry in body["services"]}[SUPERVISOR]

    assert reported["projects_held"] == 2
    assert reported["holds_this_project"] is True
    assert elsewhere.project_id not in json.dumps(body), (
        "the answer named another project to a caller who is not a member of it"
    )


def test_a_member_may_ask_whether_the_supervisor_is_alive(
    client: TestClient, database: Database, project: Project
) -> None:
    """Membership rather than the ADMIN role, like the Temporal probe.

    "The process that drives this project is down" is a fact a member waiting
    on a node that has not moved is entitled to, and the route reveals nothing
    about any other project.
    """
    from tests.integration.gateway.conftest import account

    account(database, username="bench", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "bench")["access_token"])

    response = client.get(f"/projects/{project.project_id}/runtime/services", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json()["services"]


def test_no_token_is_refused_by_the_new_reads(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """All four, because a route added later is a route that can forget."""
    project, _ = administered
    for path in ("services", "backends", "jobs", "reconciliations"):
        assert client.get(f"/projects/{project.project_id}/runtime/{path}").status_code == 401, path


# ── The cluster ─────────────────────────────────────────────────────────────


def test_the_slurm_panel_names_the_setting_and_never_the_secret(
    database: Database, administered: tuple[Project, str], gateway_settings: Settings
) -> None:
    """The `slurm_` rule, asserted on the response rather than on the source.

    An administrator is entitled to know *whether* the deployment can reach a
    cluster, and that is a different fact from knowing the password. The route
    answers by naming the setting that holds it — `RAVEL_SLURM_PASSWORD` — and
    the test checks the value is nowhere in the body, because a route that
    leaked it would look exactly like one that did not to every reader who
    never thought to look.

    Its own application, built from settings this test owns, rather than the
    shared one: a secret assigned into the running Gateway's settings would
    still be there for whatever ran next.
    """
    project, _ = administered
    secret = "not-a-real-slurm-password"
    settings = gateway_settings.model_copy(deep=True)
    settings.slurm_password = SecretStr(secret)
    settings.slurm_host = "cluster.invalid"
    app = create_app(settings=settings, database=database)

    with TestClient(app) as isolated:
        headers = bearer(sign_in(isolated, "root")["access_token"])
        response = isolated.get(
            f"/projects/{project.project_id}/runtime/backends", headers=headers
        )

    assert response.status_code == 200, response.text
    serialised = json.dumps(response.json())
    assert "RAVEL_SLURM_PASSWORD" in serialised, serialised
    assert secret not in serialised, "the cluster password reached a response"
    assert response.json()["slurm"]["host"] == "cluster.invalid"


def test_the_gateway_does_not_probe_the_cluster_and_says_so(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """Not a gap — the same rule the credential split exists for.

    Opening an SSH session from here to answer a health question would mean the
    Gateway holding the cluster password, which is what `_LAUNCHER_ONLY`
    prevents. The route prints that sentence rather than paraphrasing it,
    because it is the one thing on the panel a reader might otherwise assume
    had been tested.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    slurm = client.get(
        f"/projects/{project.project_id}/runtime/backends", headers=headers
    ).json()["slurm"]

    assert slurm["reachability"] == "not probed from the Gateway"
    assert "backend process" in slurm["why"]
    assert slurm["trust_unknown_host"] is False
    assert isinstance(slurm["configured"], bool)


def test_the_backends_panel_reports_what_the_work_was_handed_to(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """Which backends are *registered* is not answerable from here, and this is
    the fact that is.

    The registry is built by the Temporal worker from its own command line, in
    another process the Gateway cannot ask. So the answer is read from
    `backend_jobs` — what actually happened — and a project with no jobs gets
    an empty list rather than an invented inventory.
    """
    project, _ = administered
    node_id = _a_node(database, project)
    with database.transaction() as session:
        BackendJobRepository(session, project.project_id).add(
            BackendJob(
                project_id=project.project_id,
                node_id=node_id,
                attempt=1,
                execution_contract_ref="c-1",
                execution_contract_version=1,
                backend="slurm",
                state=JobState.FAILED,
                failure_class=FailureClass.INFRA_RETRYABLE,
                ended_at=utcnow(),
            )
        )
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/backends", headers=headers).json()

    assert body["used"] == [
        {
            "backend": "slurm",
            "jobs": 1,
            "states": {"FAILED": 1},
            "node_types": ["COMPUTATION"],
        }
    ]


def test_a_project_with_no_jobs_has_an_empty_list_and_not_an_inventory(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/backends", headers=headers).json()

    assert body["used"] == []


# ── The jobs ────────────────────────────────────────────────────────────────


def test_jobs_are_split_by_the_transition_table_and_not_by_a_list_here(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """"Has this job ended" is a question the domain already answers.

    A terminal state is a state with no way out of it, so the split is derived
    from `JOB_TRANSITIONS`. A state added to the machine would otherwise join
    neither half and become invisible to an operator counting what needs
    attention, which is the failure this asserts against.
    """
    project, _ = administered
    node_id = _a_node(database, project)
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        repository.add(
            BackendJob(
                project_id=project.project_id,
                node_id=node_id,
                attempt=1,
                execution_contract_ref="c-1",
                execution_contract_version=1,
                backend="mock-compute",
                state=JobState.FAILED,
                failure_class=FailureClass.INFRA_RETRYABLE,
                ended_at=utcnow(),
            )
        )
        repository.add(
            BackendJob(
                project_id=project.project_id,
                node_id=node_id,
                attempt=2,
                execution_contract_ref="c-1",
                execution_contract_version=1,
                backend="mock-compute",
                state=JobState.RUNNING,
            )
        )
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/jobs", headers=headers).json()

    assert body["total"] == 2
    assert [job["attempt"] for job in body["open"]] == [2]
    assert [job["attempt"] for job in body["ended"]] == [1]
    assert body["failed_by_class"] == {"INFRA_RETRYABLE": 1}
    # The node's name travels with the job, because an operator reading a
    # failure wants the label Master gave it rather than a uuid.
    assert body["open"][0]["display_id"]
    assert set(UNFINISHED) & {job["state"] for job in body["ended"]} == set()


def test_a_project_with_no_jobs_answers_with_zeroes_rather_than_an_empty_body(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """"Nothing has been submitted here" is an answer, and a screen shows it.

    A panel that received `{}` could not tell a project with no jobs from a
    route that failed to answer.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(f"/projects/{project.project_id}/runtime/jobs", headers=headers).json()

    assert body == {
        "project_id": project.project_id,
        "total": 0,
        "open": [],
        "ended": [],
        "failed_by_class": {},
    }


# ── The recoveries ──────────────────────────────────────────────────────────


def test_a_project_with_no_recoveries_says_zero_rather_than_nothing(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """The healthy answer, and it is the answer somebody came to the panel for.

    An empty list would be indistinguishable from a route that had not been
    asked, and "RAVEL has never had to recover a run here" is a fact worth
    being able to read.
    """
    project, _ = administered
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(
        f"/projects/{project.project_id}/runtime/reconciliations", headers=headers
    ).json()

    assert body["total"] == 0
    assert body["recent"] == []
    assert body["by_class"] == {}
    assert body["by_observation"] == {}


def test_a_recovery_is_reported_with_what_the_probe_actually_saw(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """`observed` is on the row because only one of its values is evidence.

    A run that was *found* gone is a recovery. A run Temporal could not be
    asked about is a probe that failed, and reporting the second as the first
    would turn RAVEL's own blindness into a claim about somebody's experiment.
    """
    project, _ = administered
    node_id = _a_node(database, project)
    with database.transaction() as session:
        RunReconciliationRepository(session, project.project_id).record(
            RunReconciliation(
                project_id=project.project_id,
                node_id=node_id,
                execution_contract_version=1,
                workflow_id="run-under-test",
                observed=WorkflowLiveness.UNKNOWN,
                failure_class=RunFailureClass.WORKFLOW_LOST,
                node_status_before=NodeStatus.RUNNING,
                node_status_after=NodeStatus.WAITING_DECISION,
                detail="the frontend did not answer",
            )
        )
    headers = bearer(sign_in(client, "root")["access_token"])

    body = client.get(
        f"/projects/{project.project_id}/runtime/reconciliations", headers=headers
    ).json()

    assert body["total"] == 1
    assert body["by_observation"] == {"UNKNOWN": 1}
    assert body["by_class"] == {"WORKFLOW_LOST": 1}
    record = body["recent"][0]
    assert record["workflow_id"] == "run-under-test"
    assert record["node_status_before"] == "RUNNING"
    assert record["node_status_after"] == "WAITING_DECISION"
    assert record["detail"] == "the frontend did not answer"


# ── Who may not look ────────────────────────────────────────────────────────


def test_a_lab_user_may_see_the_processes_and_not_the_estate(
    client: TestClient, database: Database, project: Project
) -> None:
    """The split between the two halves of this module, on one caller.

    A bench user waiting on a task is entitled to know whether the supervisor
    is alive. The job list and the recovery count are a project's execution
    history, which is an operator's view of somebody's research — and the
    refusal is the role's, not membership's, since this caller *is* a member.
    """
    from tests.integration.gateway.conftest import account

    account(database, username="bench", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "bench")["access_token"])

    assert (
        client.get(
            f"/projects/{project.project_id}/runtime/services", headers=headers
        ).status_code
        == 200
    )
    for path in ("backends", "jobs", "reconciliations"):
        refused = client.get(f"/projects/{project.project_id}/runtime/{path}", headers=headers)
        assert refused.status_code == 403, path


def test_a_stranger_is_told_the_project_is_not_theirs(
    client: TestClient, database: Database, administered: tuple[Project, str]
) -> None:
    """404 rather than 403, which is how this Gateway says "not yours".

    The distinction matters to a client: re-authenticating would not help, and
    a screen that told somebody to sign in again would be sending them round a
    loop. The new reads are scoped like every other project route.
    """
    project, _ = administered
    with database.transaction() as session:
        UserRepository(session).create(username="bench", password_hash=hash_password(PASSWORD))
    headers = bearer(sign_in(client, "bench")["access_token"])

    for path in ("services", "backends", "jobs", "reconciliations"):
        refused = client.get(f"/projects/{project.project_id}/runtime/{path}", headers=headers)
        assert refused.status_code == 404, path
