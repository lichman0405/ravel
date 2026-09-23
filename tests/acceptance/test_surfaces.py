"""A15 and A18: what survives a process dying, and what a person can see.

A15 is about where a project lives. Master is a session in a process, and a
process ends — a crash, a deploy, an operator's `kill`. If anything a recovery
needs lived only in that session, the project would end with it. So the test
kills the session and then asks the *record* what the project was doing: the
checkpoint the dead session left, the state it was reading, and the event
sequence it named as its anchor. Then it lets the project finish, because
"recovered" that cannot be continued is not recovery.

A18 is the three people `docs/08` §5 describes, at the console. Each sentence of
the item is one test, and each is asserted against PostgreSQL or the Gateway
rather than against the screen: a console that drew a project as paused would
pass a test that asked the console, and the claim is about what the keypress
did. The console itself is real — a real `RavelTUI` against a real uvicorn over
real HTTP and a real WebSocket — and the world it is pointed at comes from
`tests/e2e/test_tui.py` rather than being rebuilt here.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from tests.dsh.mcp_probe import ProbeResult, ToolCall, probe
from tests.e2e.conftest import Headless, LiveGateway
from tests.e2e.test_tui import World, console, notice_of, project_status, settle
from tests.integration.conftest import Prepared
from tests.integration.gateway.conftest import PASSWORD
from tests.integration.roles.conftest import RoleEnvironment
from tests.support.lab import (
    LOG_BYTES,
    OUTPUTS,
    a_bench_user,
    start_and_wait,
    terms,
)
from textual.widgets import Input, Select

from ravel.backends.lab import HumanLabBackend
from ravel.config import REPO_ROOT, Settings
from ravel.domain.contracts import ProjectSuccessContract
from ravel.domain.enums import NodeStatus, NodeType, ProjectStatus
from ravel.domain.roles import AgentRole
from ravel.dsh.pool import DshRuntimePool
from ravel.state.database import Database
from ravel.state.repositories.contracts import SuccessContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.lab import LabHandoverRepository, arrived_outputs
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DeviationRepository
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import S3ArtifactStore

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

#: Read off the state before and after the kill, so the comparison is between
#: two readings of PostgreSQL rather than between a reading and a memory.
#: `latest_checkpoint` is the one field the kill is *supposed* to change —
#: writing one is what the session did — and `last_event_seq` moves with it,
#: because writing a checkpoint is itself an event.
_VOLATILE = frozenset({"latest_checkpoint", "last_event_seq"})


def _payload(result: ProbeResult, index: int, what: str) -> dict[str, Any]:
    """One call's structured payload, with the ways it can be absent ruled out."""
    call: ToolCall = result.calls[index]
    assert not call.failed, f"{what} failed: {call.error}"
    assert call.payload is not None, f"{what} returned no structured payload"
    return call.payload


def _settled(state: dict[str, Any]) -> dict[str, Any]:
    """A `read_project_state` reply without the two fields a checkpoint moves."""
    return {key: value for key, value in state.items() if key not in _VOLATILE}


def _status(database: Database, project_id: str, node_id: str) -> NodeStatus:
    """A node's status, read from PostgreSQL rather than from a return value."""
    with database.read_only() as session:
        return DagRepository(session, project_id).node(node_id).status


def dsh_pin_tag() -> str:
    """The harness tag the console is required to report, read from the pin."""
    pin = json.loads((REPO_ROOT / "vendor" / "DSH_PIN.json").read_text(encoding="utf-8"))
    tag = str(pin["pin"]["tag"])
    assert tag, "the pin names no tag, so there is nothing for the console to agree with"
    return tag


# ── A15 ─────────────────────────────────────────────────────────────────────


async def test_a15_the_project_survives_the_master_session_and_is_recovered_from_it(
    headless: Headless,
    integration_settings: Settings,
    prepare: Callable[..., Prepared],
    tmp_path: Path,
) -> None:
    """A15: "Kill Master DSH session/process. Project survives. Recover Master
    from Project State + checkpoint; continue correctly."

    The three sentences are three moments, and the test stops at each one to
    look. *Before the kill*: a Master session reads the project and writes a
    checkpoint, which is the whole of what a session leaves behind that is not
    already in the record. *The kill*: the scope's runtime is closed the way
    RAVEL closes one — the runtime is gone, and the session binding that said
    which work the session served is released with it, because a binding to a
    dead session is a falsified record. *After*: a process that has never seen
    the project reads the checkpoint the dead session wrote and the same state
    the dead session read, field for field.

    And then it continues, which is the part that makes the rest mean anything:
    the project runs its node and reaches an ending. A recovery that restored
    the ability to *read* a project and not to finish one would satisfy every
    assertion above and none of the item.

    What is killed is the scope — RAVEL's own unit of session death, and the
    thing `reap_idle` acts on. Killing the harness process outright takes a
    model turn to have a process at all, which is why that half is
    `tests/dsh/test_spike.py`'s recovery test, under a credential.
    """
    project_id = headless.project.project_id
    # A project mid-flight: one node ready to run, its contracts already frozen,
    # and a success contract in force — which is the state a session would be
    # killed in, and the state a recovery has to be able to pick up.
    prepared = prepare()
    with headless.database.transaction() as session:
        SuccessContractRepository(session, project_id).add_version(
            ProjectSuccessContract(
                project_id=project_id,
                success_criteria=("The dopant series shows a 15% conductivity gain.",),
                failure_criteria=("No sample exceeds the control beyond noise.",),
                unresolved_uncertainty_policy="Conclude inconclusive rather than guess.",
            )
        )
        registry = ProjectRegistry(session)
        for status, reason in (
            (ProjectStatus.CONTRACT_DEFINED, "Success and failure criteria are frozen."),
            (ProjectStatus.EXECUTING, "The first stage is ready to run."),
        ):
            registry.transition(
                project_id, status, actor_id=AgentRole.MASTER.value, reason=reason
            )

    environment = RoleEnvironment(settings=integration_settings, brief_dir=tmp_path / "briefs")
    scope = environment.for_project(headless.project, AgentRole.MASTER)

    # ── The session that is going to die ────────────────────────────────────
    first = await probe(
        scope,
        calls=(
            ("read_project_state", {}),
            (
                "write_master_checkpoint",
                {
                    "current_focus": "The series has been frozen and not yet run.",
                    "waiting_on": [prepared.node.display_id],
                    "pending_questions": ["Is the control sample representative?"],
                },
            ),
        ),
    )
    before = _payload(first, 0, "read_project_state")
    checkpoint = _payload(first, 1, "write_master_checkpoint")

    assert before["status"] == ProjectStatus.EXECUTING.value
    assert before["latest_checkpoint"] is None, "the project starts with no checkpoint"
    assert checkpoint["checkpoint_id"]
    assert checkpoint["last_event_seq"] >= 0

    # ── The kill ────────────────────────────────────────────────────────────
    pool = DshRuntimePool(settings=integration_settings)
    runtime, binding = pool.start_session(project_id, AgentRole.MASTER, brief={"phase": "run"})
    assert pool.live_runtime(project_id, AgentRole.MASTER) is runtime

    assert pool.close_scope(project_id, AgentRole.MASTER) is True
    assert runtime.is_closed, "the session's runtime outlived the kill"
    assert pool.live_runtime(project_id, AgentRole.MASTER) is None
    assert pool.bindings.get(binding.session_id) is None, (
        "the binding to the dead session is still on the books; it names work a "
        "session that no longer exists was doing"
    )

    # ── What survived ───────────────────────────────────────────────────────
    second = await probe(
        scope,
        calls=(("read_master_checkpoint", {}), ("read_project_state", {})),
    )
    recovered = _payload(second, 0, "read_master_checkpoint")
    after = _payload(second, 1, "read_project_state")

    assert recovered["found"] is True, "the checkpoint did not survive the session"
    assert recovered["checkpoint"]["checkpoint_id"] == checkpoint["checkpoint_id"]
    assert recovered["checkpoint"]["last_event_seq"] == checkpoint["last_event_seq"], (
        "the recovery anchor moved, so a replay from it would start somewhere "
        "other than where the session stopped"
    )
    assert recovered["checkpoint"]["current_focus"] == (
        "The series has been frozen and not yet run."
    )
    assert recovered["checkpoint"]["master_identity_id"] == checkpoint["master_identity_id"]

    assert after["latest_checkpoint"] == recovered["checkpoint"], (
        "reading the project does not surface the checkpoint a recovery would use"
    )
    assert _settled(after) == _settled(before), (
        "the project changed while no session was directing it"
    )
    assert after["status"] == ProjectStatus.EXECUTING.value
    assert after["dag"]["ready_to_run"] == [prepared.node.display_id]

    # ── And it continues ────────────────────────────────────────────────────
    headless.compute("COMPUTE_SUCCESS")
    run = await headless.drive(headless.master(()), headless.review())

    assert run.finished, f"the project did not continue; it halted at {run.status.value}"
    assert run.status is ProjectStatus.COMPLETED
    assert _status(headless.database, project_id, prepared.node_id) is NodeStatus.PASSED, (
        "the recovered project ran the node the dead session left ready, and the "
        "node came to a result"
    )


# ── A18 ─────────────────────────────────────────────────────────────────────


async def test_a18_an_owner_sees_the_project_and_can_stop_and_start_it(
    live_gateway: LiveGateway,
    world: World,
    database: Database,
) -> None:
    """A18's first sentence: "Owner can view Master/current state/read-only DAG/
    Decision/Review/task status and execute allowed pause/resume/approval."

    The panels are asserted by their text, because a panel is what a person
    reads; the pause is asserted against the project row, because a status is
    what a keypress did. The two are different claims and a console that drew
    optimistically would satisfy only the first.

    The DAG view is read-only by construction — there is no binding on this
    screen that writes one, and `tests/e2e/test_tui.py` asserts the same thing
    from the Gateway's side by searching the routes.
    """
    project_id = world.owner_project.project_id
    async with console(
        live_gateway,
        username="ada",
        project_id=project_id,
        first_panel="master-focus",
    ) as (_app, pilot, screen):
        focus = screen.panel("master-focus").plain
        assert world.owner_project.display_id in focus, focus
        assert "Find a dopant that raises conductivity by 15%." in focus, focus

        summary = screen.panel("dag-summary").plain
        assert "EXECUTING" in summary, summary
        assert "READY 2" in summary, summary
        assert screen.query_one("#dag").row_count == 2, "the DAG view is missing rows"

        # Decision and Review are the two panels a project's reasoning shows up
        # in, and both are present and answering — empty, at this point, because
        # nothing has been decided or judged yet.
        assert screen.panel("attention").plain
        assert screen.panel("decisions").plain
        assert screen.panel("execution").plain

        assert project_status(database, project_id) is ProjectStatus.EXECUTING
        await pilot.press("p")
        await settle(
            pilot,
            lambda: project_status(database, project_id) is ProjectStatus.PAUSED,
            what="the project to be paused",
        )
        await settle(
            pilot,
            lambda: "Paused." in notice_of(screen),
            what="the console to say so",
        )

        await pilot.press("r")
        await settle(
            pilot,
            lambda: project_status(database, project_id) is ProjectStatus.EXECUTING,
            what="the project to be running again",
        )


async def test_a18_a_lab_user_sees_their_task_uploads_and_reports_a_deviation(
    live_gateway: LiveGateway,
    headless: Headless,
    prepare: Callable[..., Prepared],
    database: Database,
    artifact_store: S3ArtifactStore,
    tmp_path: Path,
) -> None:
    """A18's second sentence: "Lab user can view assigned task/upload/report
    deviation."

    All three, in the order a bench does them. The task list holds the
    experiment and not the computation, because showing somebody work they
    cannot do is not a view of their work. The upload ends in the object store,
    read back through the repository with the project scope checked on the way
    out. And the deviation is checked for the two things it must not do: a
    report is an observation, so the node does not move and the record says
    `permitted=False` — whether the action was allowed is Master's ruling,
    written later as a decision.

    **The project is one a bench has really been given.** A18 was written when
    the screen's upload door was the generic artifact route and any file could
    be sent under any name; P11-09 made the control answer a *required output*,
    which is what the contract's own split of sent-and-owed is about, and a
    world nothing has been handed to has no output to answer. So the contract
    here is built by the same fixture the laboratory suites use and run through
    the real workflow, and the console is opened on the bench it left waiting —
    which is also the only version of this case in which the file a bench sends
    is an answer to something.
    """
    headless.registry.register(
        NodeType.EXPERIMENT, HumanLabBackend(database=headless.database)
    )
    prepared = prepare(
        **terms(
            allowed_actions=("run_measurement",),
            allowed_ranges={"temperature_c": "18..24"},
        )
    )
    await start_and_wait(headless, prepared)
    bench_id = a_bench_user(
        database, prepared.project_id, username="a18-bench", password=PASSWORD
    )

    upload = tmp_path / f"{OUTPUTS[0]}.csv"
    upload.write_bytes(LOG_BYTES)
    project_id = prepared.project_id

    async with console(
        live_gateway,
        username="a18-bench",
        project_id=project_id,
        first_panel="instruction",
    ) as (_app, pilot, screen):
        tasks = screen.query_one("#tasks")
        assert tasks.row_count == 1, "only the experiment is a lab task"

        instruction = screen.panel("instruction").plain
        assert "allowed actions: run_measurement" in instruction, instruction
        assert "allowed ranges:  temperature_c 18..24" in instruction, instruction
        assert "required outputs: " in instruction, instruction

        screen.query_one("#upload-path", Input).value = str(upload)
        screen.query_one("#upload-output", Select).value = OUTPUTS[0]
        await screen.action_upload()
        await settle(
            pilot,
            lambda: "That was everything owed." not in notice_of(screen)
            and "Still owed:" in notice_of(screen),
            what="the upload to be accepted and to say what is still outstanding",
        )
        await settle(
            pilot,
            lambda: f"✓ {OUTPUTS[0]} — sent" in screen.panel("prepared").plain,
            what="the file to appear as an answer to the output it named",
        )

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

    with database.read_only() as session:
        handover = LabHandoverRepository(session, project_id).latest_for_node(
            prepared.node_id
        )
        assert handover is not None
        repository = ArtifactRepository(session, project_id, artifact_store)
        # The artifact is named for the *output*, and it is scoped to the
        # handover: two files that happened to share a filename would be two
        # answers, and this one is the answer to `experiment_log`.
        answered = dict(arrived_outputs(session, handover))
        assert list(answered) == [OUTPUTS[0]], list(answered)
        artifact = answered[OUTPUTS[0]]
        assert artifact.created_by == bench_id, (
            "the record does not say who sent what the bench produced"
        )
        versions = repository.versions(artifact.artifact_id)
        assert repository.read(versions[-1]) == LOG_BYTES

        deviations = DeviationRepository(session, project_id).all(
            node_id=prepared.node_id
        )

    assert len(deviations) == 1
    assert deviations[0].permitted is False, (
        "reporting is not permitting; the report says what the reporter did, "
        "and what was permitted is Master's to rule on"
    )
    assert deviations[0].is_open is True


async def test_a18_an_admin_can_inspect_runtime_health(
    live_gateway: LiveGateway, world: World
) -> None:
    """A18's third sentence: "Admin can inspect runtime health."

    Checked against the pin rather than against a literal. `vendor/DSH_PIN.json`
    is where the harness version is recorded, and a console showing a different
    one would be a console agreeing with itself; the panel has to agree with the
    pin. Temporal is asserted as *reported* and not as *up*, because a service
    being down is information a health screen must be able to carry.
    """
    async with console(
        live_gateway,
        username="root",
        project_id=world.admin_project.project_id,
        first_panel="harness",
    ) as (_app, _pilot, screen):
        harness = screen.panel("harness").plain
        assert "DeepSeek Harness" in harness, harness
        assert dsh_pin_tag() in harness, harness

        assert "task queue" in screen.panel("temporal").plain
        assert "nodes 0" in screen.panel("runtime").plain
        assert "dsh home" in screen.panel("logs").plain
