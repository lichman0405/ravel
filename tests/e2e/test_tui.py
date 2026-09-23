"""The Phase 8 gate: the console, driven headlessly, against a real Gateway.

`IMPLEMENTATION_PLAN.md` states the gate as two sentences — *`tests/e2e/test_tui.py`
drives the TUI headlessly through the Owner, Lab, and Admin paths; a test asserts
that no route accepts a DAG mutation from a user* — and this file is both.

**What is real here.** A real uvicorn serving the real `create_app` on a real
port; a real PostgreSQL under it; real tokens signed with a real secret; real
HTTP and a real WebSocket, because `RavelTUI` reaches the Gateway the way a
person's terminal does rather than through an in-process transport that would
skip the server entirely. Every claim below is read back out of the database or
off the Gateway, never out of the console's own memory: the console saying a
project is paused proves the console drew a word, and the project row saying
`PAUSED` proves the keypress arrived.

**What is scripted.** Master. `live_gateway` hands `create_app` a
`ScriptedMasterPort`, so a turn through the composer reaches a `MasterPort`
without a model, a network, or an API key — the same bargain
`tests/e2e/conftest.py` documents for the loop, made one layer up. Everything
between the keystroke and that call is production's.

**The screen is driven, not poked.** Bindings are pressed with `pilot.press`
where a person would press them, so the binding table is part of what is under
test. The two actions that need a filled-in field are called directly, because
typing into an `Input` and then invoking the action is what the binding does,
and a keypress there would be testing `Input` rather than RAVEL.

**This file is named, so it may not be empty.** `ravel/gateway/app.py` and
`ravel/gateway/routes/projects.py` both point at it as the place where "no route
changes the Scientific DAG" is checked, and a claim resting on a file nobody
wrote is worse than no claim. The last two tests are that check; the rest are
the three paths A18 describes.
"""

from __future__ import annotations

import ast
import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.e2e.conftest import LiveGateway
from tests.integration.conftest import DEFAULT_OUTPUTS, build_prepared
from tests.integration.gateway.conftest import PASSWORD
from tests.support.routes import EXPECTED_ROUTE_FLOOR, effective_routes, probe_path
from textual.pilot import Pilot
from textual.widgets import Input

from ravel.domain.contracts import ProjectSuccessContract
from ravel.domain.enums import NodeType, ProjectStatus, UserRole
from ravel.domain.project import Project
from ravel.gateway.auth.passwords import hash_password
from ravel.state.database import Database
from ravel.state.repositories.contracts import SuccessContractRepository
from ravel.state.repositories.conversation import ConversationRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DeviationRepository
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import S3ArtifactStore
from ravel.tui.app import ROLE_SCREENS, RavelTUI, screen_for_role
from ravel.tui.screens.admin import AdminScreen
from ravel.tui.screens.lab import LabScreen
from ravel.tui.screens.owner import OwnerScreen
from ravel.tui.widgets import Notice, StatusTable

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(300)]

#: How long to wait for the console to reach a state. Generous, because one
#: pass covers a sign-in, seven HTTP requests and a WebSocket handshake, and the
#: only cost of being wrong in this direction is a slow failure rather than a
#: flaky one.
SETTLE_SECONDS = 20.0

#: Where `src/ravel/gateway` lives, for the source check at the bottom.
GATEWAY_ROOT = Path(__file__).resolve().parents[2] / "src" / "ravel" / "gateway"

#: The methods of `DagRepository` that write.
#:
#: Named rather than derived, because nothing in the class says which of its
#: methods mutate — and pinned here so that a rename fails the check below
#: loudly instead of quietly shrinking what it searches for.
MUTATING_DAG_METHODS = frozenset(
    {
        "add_node",
        "bind_acceptance_contract",
        "bind_execution_contract",
        "record_artifact",
        "refresh_readiness",
    }
)

#: Where the lab's upload is written, and what is in it.
UPLOAD_NAME = "ravel-tui-upload.csv"
UPLOAD_BODY = b"angle,counts\n12.5,4102\n13.0,4098\n"

#: The node objective `build_prepared` gives every node it builds, and the
#: objective on the contract it freezes. Two different strings, deliberately:
#: the task list shows the first and the instruction panel shows the second, so
#: a test that used one string for both would not notice a screen reading the
#: wrong one.
NODE_OBJECTIVE = "Measure conductivity across the dopant series."
CONTRACT_OBJECTIVE = "Measure the conductivity of each sample."


# ── The world the console is pointed at ─────────────────────────────────────


@dataclass(frozen=True, slots=True)
class World:
    """Two projects and the three people `docs/08` §5 describes.

    Two, because the three roles cannot all be held in one.
    `MembershipRepository` refuses a granter who does not outrank the role being
    granted, so an owner can never make an administrator — the only way an
    `ADMIN` membership arises is as a project's *first*. That is a real rule
    with a real consequence for whoever sets RAVEL up, and this fixture follows
    it rather than working around it.
    """

    owner_project: Project
    admin_project: Project
    owner: str
    lab: str
    admin: str
    computation: str
    experiment: str


@pytest.fixture
def world(database: Database, clean: None) -> World:
    """A project with a DAG in it, and an owner, a lab user and an admin.

    The project is taken to `EXECUTING` with a success contract in force,
    because that is the state a person watches: a `CREATED` project cannot be
    paused, and a screen whose pause binding could never work would make that
    binding untestable for a reason that has nothing to do with the console.

    Two nodes, one of each kind a worker runs, so the DAG view has more than one
    row and the lab screen has something addressed to it. Both are built through
    `build_prepared`, which is the path production takes, so the contracts they
    run under are frozen rather than merely written.
    """
    with database.transaction() as session:
        users = UserRepository(session)
        owner = users.create(username="ada", password_hash=hash_password(PASSWORD))
        lab = users.create(username="bench", password_hash=hash_password(PASSWORD))
        admin = users.create(username="root", password_hash=hash_password(PASSWORD))

        registry = ProjectRegistry(session)
        project = registry.create(
            title="Catalyst screen",
            objective="Find a dopant that raises conductivity by 15%.",
            created_by=owner.user_id,
        )
        members = MembershipRepository(session, project.project_id)
        # The first membership names no granter because there was nobody to
        # name; the second names the owner, who does outrank a lab user.
        members.grant(user_id=owner.user_id, role=UserRole.PROJECT_OWNER)
        members.grant(user_id=lab.user_id, role=UserRole.LAB_USER, granted_by=owner.user_id)

        SuccessContractRepository(session, project.project_id).add_version(
            ProjectSuccessContract(
                project_id=project.project_id,
                success_criteria=("The dopant series shows a 15% conductivity gain.",),
                failure_criteria=("No sample exceeds the control beyond noise.",),
                unresolved_uncertainty_policy="Conclude inconclusive rather than guess.",
            )
        )
        for status, reason in (
            (ProjectStatus.CONTRACT_DEFINED, "Success and failure criteria are frozen."),
            (ProjectStatus.EXECUTING, "The first stage is ready to run."),
        ):
            registry.transition(project.project_id, status, actor_id="ada", reason=reason)
        # Re-read, because `transition` returns a copy and the object `create`
        # handed back still says CREATED. A fixture that returned the stale one
        # would hand every test below a project in the wrong status.
        project = registry.get(project.project_id)

        computation = build_prepared(
            session, project_id=project.project_id, node_type=NodeType.COMPUTATION
        )
        experiment = build_prepared(
            session,
            project_id=project.project_id,
            node_type=NodeType.EXPERIMENT,
            required_outputs=DEFAULT_OUTPUTS,
            allowed_ranges={"temperature_c": "18..24"},
        )

        # The administrator's own project, where the administrator is the
        # *first* member. Nothing else is in it: the administrator's screen is
        # about the machine, and a project with a DAG in it would only be a
        # distraction from that.
        admin_project = registry.create(
            title="Runtime", objective="Keep the machine running.", created_by=admin.user_id
        )
        MembershipRepository(session, admin_project.project_id).grant(
            user_id=admin.user_id, role=UserRole.ADMIN
        )

        return World(
            owner_project=project,
            admin_project=admin_project,
            owner=owner.user_id,
            lab=lab.user_id,
            admin=admin.user_id,
            computation=computation.node_id,
            experiment=experiment.node_id,
        )


# ── Driving the console ─────────────────────────────────────────────────────


async def settle(
    pilot: Pilot[None],
    ready: Callable[[], bool],
    *,
    what: str,
    timeout: float = SETTLE_SECONDS,
) -> None:
    """Let the application run until `ready`, or fail saying what was waited for.

    `pilot.pause` drains the message queue, which is what lets a coroutine that
    is awaiting a socket make progress; the sleep between passes keeps this from
    spinning the event loop while a real request is in flight.

    `ready` is asked of mounted widgets only, so a caller has to check
    `is_mounted` before it queries — the alternative is a predicate that raises
    `NoMatches` inside a wait loop, which reads as a broken test rather than as
    a widget that has not arrived yet.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if ready():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"the console never reached {what} within {timeout}s")


@asynccontextmanager
async def console(
    gateway: LiveGateway,
    *,
    username: str,
    project_id: str,
    first_panel: str,
) -> AsyncIterator[tuple[RavelTUI, Pilot[None], Any]]:
    """Sign in, wait for the role's screen to be filled, and hand back both.

    `first_panel` is the panel that role's screen fills first, and it is the
    readiness signal rather than a courtesy: `show_project` records
    `role_screen` *before* it mounts the screen and reads the routes, so "the
    right screen exists" and "the right screen has read anything" are two
    different moments, and a test that assumed the second would read empty
    panels on a slow machine.

    The screen must also hold the focus, because that is the state a person's
    terminal is in and the state every `pilot.press` below depends on: the
    bindings live on the screen, and a console whose screen is not focused is
    showing a footer with no keys on it.
    """
    app = RavelTUI(
        base_url=gateway.url,
        username=username,
        password=PASSWORD,
        project_id=project_id,
    )
    async with app.run_test() as pilot:
        await settle(
            pilot,
            lambda: app.role_screen is not None and app.role_screen.is_mounted,
            what=f"a screen for {username}",
        )
        screen = app.role_screen
        assert screen is not None
        await settle(
            pilot,
            lambda: bool(screen.panel(first_panel).plain),
            what=f"the {first_panel} panel to be filled",
        )
        await settle(pilot, lambda: screen.has_focus, what="the screen to take the focus")
        yield app, pilot, screen


def notice_of(screen: Any) -> str:
    """The one line the screen is saying about what just happened."""
    return str(screen.query_one("#notice", Notice).message)


def project_status(database: Database, project_id: str) -> ProjectStatus:
    """The project's status, read from the database rather than from a screen."""
    with database.read_only() as session:
        return ProjectRegistry(session).get(project_id).status


def node_status(database: Database, project_id: str, node_id: str) -> str:
    with database.read_only() as session:
        return DagRepository(session, project_id).node(node_id).status.value


# ── The owner's path ────────────────────────────────────────────────────────


async def test_an_owner_sees_the_project_its_graph_and_its_master(
    live_gateway: LiveGateway, world: World
) -> None:
    """A18's first sentence, as far as it can be checked from outside.

    Every panel is asserted through the text the widget is holding, so what is
    being checked is what somebody would read — not that a request succeeded.
    The DAG table is checked by its row count as well as by its contents,
    because a table that silently rendered nothing would otherwise pass an `in`
    test against an empty string.
    """
    async with console(
        live_gateway,
        username="ada",
        project_id=world.owner_project.project_id,
        first_panel="master-focus",
    ) as (_app, _pilot, screen):
        focus = screen.panel("master-focus").plain
        assert world.owner_project.display_id in focus, focus
        assert "Catalyst screen" in focus, focus
        assert "Find a dopant that raises conductivity by 15%." in focus, focus

        summary = screen.panel("dag-summary").plain
        assert "EXECUTING" in summary, summary
        assert "your role: PROJECT_OWNER" in summary, summary
        assert "READY 2" in summary, summary

        table = screen.query_one("#dag", StatusTable)
        assert table.row_count == 2, "the DAG view is missing rows"
        assert NODE_OBJECTIVE in str(table.get_row_at(0)[2])

        assert screen.panel("attention").plain
        assert screen.panel("execution").plain == "Nothing has been submitted to a backend yet."
        assert screen.panel("decisions").plain == "No decisions yet."


async def test_the_owner_can_stop_and_start_the_project(
    live_gateway: LiveGateway, world: World, database: Database
) -> None:
    """Pause and resume, pressed as keys and confirmed against PostgreSQL.

    The status is read from the database rather than from the console, which is
    the whole point: a screen that redrew itself optimistically would pass a
    test that asked the screen.
    """
    async with console(
        live_gateway,
        username="ada",
        project_id=world.owner_project.project_id,
        first_panel="master-focus",
    ) as (_app, pilot, screen):
        assert project_status(database, world.owner_project.project_id) is ProjectStatus.EXECUTING

        await pilot.press("p")
        await settle(
            pilot,
            lambda: project_status(database, world.owner_project.project_id)
            is ProjectStatus.PAUSED,
            what="the project to be paused",
        )
        await settle(pilot, lambda: "Paused." in notice_of(screen), what="the notice")

        await pilot.press("r")
        await settle(
            pilot,
            lambda: project_status(database, world.owner_project.project_id)
            is ProjectStatus.EXECUTING,
            what="the project to be running again",
        )


async def test_what_the_owner_types_reaches_master_and_the_answer_comes_back(
    live_gateway: LiveGateway, world: World, database: Database
) -> None:
    """The composer, end to end: a keystroke, a route, a `MasterPort`, a row.

    The transcript is asserted from the database and the panel from the screen,
    because those are two different claims — that Master was asked, and that the
    person can see what Master said. Asserting only the screen would pass on a
    screen that drew the question twice and never asked anything.
    """
    live_gateway.master.reply = "The control sample is the place to start."
    async with console(
        live_gateway,
        username="ada",
        project_id=world.owner_project.project_id,
        first_panel="master-focus",
    ) as (_app, pilot, screen):
        await pilot.press("e")  # the composer's own binding, which focuses it
        composer = screen.query_one("#composer", Input)
        assert composer.has_focus, "the binding meant to focus the composer did not"
        composer.value = "Where should the bench begin?"
        await pilot.press("enter")

        await settle(
            pilot,
            lambda: "The control sample is the place to start."
            in screen.panel("master-focus").plain,
            what="Master's answer on the screen",
        )
        assert live_gateway.master.heard == ["Where should the bench begin?"]

    with database.read_only() as session:
        transcript = [
            (message.author_type.value, message.body)
            for message in ConversationRepository(
                session, world.owner_project.project_id
            ).transcript()
        ]
    assert ("USER", "Where should the bench begin?") in transcript
    assert ("AGENT", "The control sample is the place to start.") in transcript


async def test_the_screen_follows_the_project_while_nobody_presses_anything(
    live_gateway: LiveGateway, world: World
) -> None:
    """The live stream, which §7 asks the console to hold a sequence number for.

    The change is made by *another client* over HTTP, so the only path by which
    the screen can learn of it is the WebSocket. That is what makes this a test
    of the stream rather than of the refresh binding.

    The cursor is asserted as well as the panel, because a screen that redrew
    itself on a timer would pass the panel assertion and fail this one — and
    redrawing on a timer is exactly what §7's sequence number exists to avoid.
    """
    async with console(
        live_gateway,
        username="ada",
        project_id=world.owner_project.project_id,
        first_panel="master-focus",
    ) as (app, pilot, screen):
        assert "PAUSED" not in screen.panel("dag-summary").plain

        async with httpx.AsyncClient(base_url=live_gateway.url) as other:
            pair = (
                await other.post("/auth/login", json={"username": "ada", "password": PASSWORD})
            ).json()
            stopped = await other.post(
                f"/projects/{world.owner_project.project_id}/pause",
                json={"reason": "the bench is being recalibrated"},
                headers={"Authorization": f"Bearer {pair['access_token']}"},
            )
            assert stopped.status_code == 200, stopped.text

        await settle(
            pilot,
            lambda: "PAUSED" in screen.panel("dag-summary").plain,
            what="the pause to arrive over the stream",
        )
        assert app.client.credentials.last_event_seq > 0, (
            "the console drew the change without advancing its event cursor"
        )


# ── The lab's path ──────────────────────────────────────────────────────────


async def test_a_lab_user_sees_the_contract_they_work_under(
    live_gateway: LiveGateway, world: World
) -> None:
    """A18's second sentence: the assigned task, and the instruction for it.

    `build_prepared` freezes a contract with `run_measurement` allowed and a
    temperature range, so the screen has something specific to show. The range
    is asserted and not only the actions, because a screen that showed the
    actions and dropped the ranges would look complete — and the ranges are the
    half a bench actually works inside.

    One task, not two: the COMPUTATION node belongs to a worker, and showing it
    here would be showing somebody work they cannot do.
    """
    async with console(
        live_gateway,
        username="bench",
        project_id=world.owner_project.project_id,
        first_panel="instruction",
    ) as (_app, _pilot, screen):
        tasks = screen.query_one("#tasks", StatusTable)
        assert tasks.row_count == 1, "only the experiment is a lab task"
        assert NODE_OBJECTIVE in str(tasks.get_row_at(0)[2])

        instruction = screen.panel("instruction").plain
        assert CONTRACT_OBJECTIVE in instruction, instruction
        assert "allowed actions: run_measurement" in instruction, instruction
        assert "allowed ranges:  temperature_c 18..24" in instruction, instruction
        assert "required outputs: conductivity.csv, notes.json" in instruction, instruction
        assert "frozen " in instruction, instruction

        status = screen.panel("status").plain
        assert "no backend job has been submitted for this task" in status, status
        assert screen.panel("reported").plain == "Nothing has been reported against this task."


async def test_a_lab_user_uploads_a_result_and_the_bytes_reach_the_store(
    live_gateway: LiveGateway,
    world: World,
    database: Database,
    artifact_store: S3ArtifactStore,
    tmp_path: Path,
) -> None:
    """The upload, driven from the screen, ending in the object store.

    The file is written to disk first because that is what the screen's field
    names — a path, not a blob — and the round trip through a real filesystem is
    part of what is being checked. The bytes are then read back out of MinIO
    through the repository, which is the claim that matters: RAVEL received
    them, and PostgreSQL holds the record saying where they went.
    """
    result = tmp_path / UPLOAD_NAME
    result.write_bytes(UPLOAD_BODY)

    async with console(
        live_gateway,
        username="bench",
        project_id=world.owner_project.project_id,
        first_panel="instruction",
    ) as (_app, pilot, screen):
        screen.query_one("#upload-path", Input).value = str(result)
        await screen.action_upload()
        await settle(
            pilot,
            lambda: f"Sent {UPLOAD_NAME}" in notice_of(screen),
            what="the upload to be accepted",
        )

    with database.read_only() as session:
        repository = ArtifactRepository(session, world.owner_project.project_id, artifact_store)
        artifacts = repository.all()
        assert [artifact.name for artifact in artifacts] == [UPLOAD_NAME]
        versions = repository.versions(artifacts[0].artifact_id)
        assert [version.version for version in versions] == [1]
        # The bytes, by the key the record names rather than one this test
        # guessed at, and through the repository so the project scope is
        # checked on the way out as it was on the way in.
        assert repository.read(versions[-1]) == UPLOAD_BODY


async def test_a_lab_user_reports_a_deviation_and_nothing_moves(
    live_gateway: LiveGateway, world: World, database: Database
) -> None:
    """The deviation, and the two things it must not do.

    A report is an observation: the node does not move, and what was recorded is
    `permitted=False` because the *reporter* permitted nothing. Whether the
    action was in fact allowed is Master's ruling, written later as a decision —
    so a test that found the node had moved here would have found the three-way
    separation broken.
    """
    before = node_status(database, world.owner_project.project_id, world.experiment)

    async with console(
        live_gateway,
        username="bench",
        project_id=world.owner_project.project_id,
        first_panel="instruction",
    ) as (_app, pilot, screen):
        screen.query_one("#deviation-action", Input).value = "hold at 30 C for an hour"
        screen.query_one("#deviation-description", Input).value = (
            "the contract's range stops at 24 C and the sample needs longer"
        )
        await screen.action_report_deviation()
        await settle(
            pilot,
            lambda: "Master decides what happens next." in notice_of(screen),
            what="the report to be accepted",
        )
        await settle(
            pilot,
            lambda: "hold at 30 C" in screen.panel("reported").plain,
            what="the report to appear on the screen",
        )

    assert node_status(database, world.owner_project.project_id, world.experiment) == before
    with database.read_only() as session:
        deviations = DeviationRepository(session, world.owner_project.project_id).all(
            node_id=world.experiment
        )
    assert len(deviations) == 1
    assert deviations[0].permitted is False
    assert deviations[0].is_open is True
    assert "hold at 30 C for an hour" in deviations[0].requested_action


# ── The admin's path ────────────────────────────────────────────────────────


async def test_an_admin_sees_runtime_health(live_gateway: LiveGateway, world: World) -> None:
    """A18's third sentence, checked against the pin rather than a literal.

    The harness panel is asserted against the tag in `vendor/DSH_PIN.json`
    through the same route the screen reads, so a deployment pinned to something
    else fails here rather than showing a console that agrees with itself.
    Temporal is asserted as *reported*, not as *up*: a service that is down is
    information a screen must be able to carry, and whether its container is
    running is not this test's business.
    """
    async with console(
        live_gateway,
        username="root",
        project_id=world.admin_project.project_id,
        first_panel="harness",
    ) as (_app, _pilot, screen):
        harness = screen.panel("harness").plain
        assert "DeepSeek Harness" in harness, harness
        assert "dsh-v0.1.5-rc.1" in harness, harness
        assert "No runtime has been started in this process." in harness, harness

        temporal = screen.panel("temporal").plain
        assert "task queue" in temporal, temporal

        runtime = screen.panel("runtime").plain
        assert "nodes 0" in runtime, runtime
        assert "backend jobs 0" in runtime, runtime

        assert "dsh home" in screen.panel("logs").plain


async def test_an_admin_can_read_the_runtime_but_not_direct_the_project(
    live_gateway: LiveGateway, world: World, database: Database
) -> None:
    """An administrator is not a director.

    `may_direct_project` is the owner's, so an administrator's own token is
    refused the project's life cycle. Asserted here *as well as* in the route
    tests because the consequence is about a screen: an administrator's console
    that offered a pause binding would be offering one that could never work,
    and an absent action is the honest rendering of absent authority.
    """
    async with console(
        live_gateway,
        username="root",
        project_id=world.admin_project.project_id,
        first_panel="harness",
    ) as (_app, _pilot, screen):
        assert not hasattr(screen, "action_pause"), (
            "the administrator's screen has grown a way to direct a project"
        )

    async with httpx.AsyncClient(base_url=live_gateway.url) as admin_client:
        pair = (
            await admin_client.post(
                "/auth/login", json={"username": "root", "password": PASSWORD}
            )
        ).json()
        refused = await admin_client.post(
            f"/projects/{world.admin_project.project_id}/pause",
            json={"reason": "trying it on"},
            headers={"Authorization": f"Bearer {pair['access_token']}"},
        )
    assert refused.status_code in {403, 404}, refused.text
    assert project_status(database, world.admin_project.project_id) is ProjectStatus.CREATED


# ── Which role gets which screen ────────────────────────────────────────────


def test_the_role_decides_the_screen() -> None:
    """A18's three sentences, as a mapping.

    `docs/08` §5 gives each role a view, and nothing here takes a role from a
    client — `RavelTUI` looks it up from what `GET /projects` reported. A role
    this program does not know is `None` rather than a default, because the only
    defensible default would be the *most* restricted screen, and getting that
    wrong in the other direction shows a lab user the whole project's DAG.
    """
    assert screen_for_role("PROJECT_OWNER") is OwnerScreen
    assert screen_for_role("LAB_USER") is LabScreen
    assert screen_for_role("ADMIN") is AdminScreen
    assert screen_for_role("project_owner") is OwnerScreen
    assert screen_for_role("SOMETHING_ELSE") is None
    assert set(ROLE_SCREENS) == {role.value for role in UserRole}, (
        "a role exists that no screen is drawn for"
    )


# ── The gate: no route takes a DAG mutation from a user ─────────────────────


def test_the_gateway_never_reaches_for_a_dag_mutation() -> None:
    """The source of `ravel.gateway`, read for the two ways it could mutate.

    Two checks, because there are two ways in. `DagMutationService` is how
    Master changes the graph, and importing it here would be the mistake in its
    plainest form. `DagRepository` is imported legitimately — the read routes
    need it — and it also carries `add_node`, the binder calls and the readiness
    sweep, so the second check is that nothing under this package *calls* one of
    those.

    Neither check is the proof; the probe below is, because it asks the server.
    This one is the tripwire that fails at the point of the mistake with the
    offending file named, instead of at the point of the assertion — and it is
    an AST walk rather than a substring search, so a module that mentions the
    name in a docstring is not an offender and a module that reaches a
    repository's `add_node` is.
    """
    assert set(dir(DagRepository)) >= MUTATING_DAG_METHODS, (
        "DagRepository no longer has the methods this check looks for, so it is "
        "searching for names that cannot occur and would pass while a mutation "
        "went unnoticed"
    )

    files = sorted(GATEWAY_ROOT.rglob("*.py"))
    assert files, f"no gateway sources under {GATEWAY_ROOT}"

    imported: list[str] = []
    called: list[str] = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        where = path.relative_to(GATEWAY_ROOT).as_posix()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "ravel.state.services.dag"
                and any(alias.name == "DagMutationService" for alias in node.names)
            ):
                imported.append(where)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in MUTATING_DAG_METHODS
            ):
                called.append(f"{where}:{node.lineno} {node.func.attr}")

    assert not imported, f"the Gateway imports Master's DAG mutation service: {imported}"
    assert not called, f"the Gateway calls a method that writes the DAG: {called}"


def test_no_route_lets_a_user_change_the_dag(live_gateway: LiveGateway, world: World) -> None:
    """Every route, probed over a real socket, with the DAG read back after.

    The owner is the strongest credential a person can hold — `docs/09` says an
    owner cannot direct-mutate the DAG, and if it is true of them it is true of
    the other two. Every route the application serves is addressed, with every
    method it answers, and the graph is compared afterwards over the whole
    response rather than by a count: not only that no node was added, but that
    none was cancelled, re-bound or re-planned either.

    **The probe refuses to be vacuous**, which is the failure its sibling in
    `tests/integration/gateway/test_projects.py` fell into once. A route whose
    placeholders this function has not been taught fails the test rather than
    being sent to a literal `{node_id}` URL, where the 404 would read like a
    refusal; `EXPECTED_ROUTE_FLOOR` catches the walk itself coming back empty;
    and the three assertions after the loop check the probe went where the claim
    is about.
    """
    routes = effective_routes(live_gateway.app)
    assert len(routes) >= EXPECTED_ROUTE_FLOOR, (
        f"enumerated only {len(routes)} routes, which means the walk is broken "
        "and this test is about to assert nothing"
    )

    with httpx.Client(base_url=live_gateway.url, timeout=30.0) as client:
        pair = client.post("/auth/login", json={"username": "ada", "password": PASSWORD}).json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        project_id = world.owner_project.project_id

        before = client.get(f"/projects/{project_id}/dag", headers=headers).json()
        assert len(before) == 2, before

        probed: list[str] = []
        for route in routes:
            if not route.is_http:
                # A WebSocket is connected to, not requested, and this one
                # reads. It is covered by the stream test above.
                continue
            path = probe_path(
                route.path,
                project_id=project_id,
                node_id=world.experiment,
                task_id=world.experiment,
            )
            for method in route.methods - {"HEAD", "OPTIONS"}:
                client.request(method, path, headers=headers, json={})
                probed.append(f"{method} {path}")

        after = client.get(f"/projects/{project_id}/dag", headers=headers).json()

    assert any("/dag" in entry for entry in probed), probed
    assert any("lab/tasks" in entry for entry in probed), probed
    assert any(entry.startswith("POST") for entry in probed), probed
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True), (
        "a route reachable with an owner's token changed the Scientific DAG"
    )
