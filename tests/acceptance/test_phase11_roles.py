"""P11-09: three genuinely different screens, and what each of them cannot do.

The item's own claim is that the surfaces are *genuinely different* — not one
screen with fields hidden, but three screens whose actions differ because the
authority behind them differs. So the cases below are organised by person, and
each one walks the console the way that person would: press the keys, read the
panels, and check the writes in PostgreSQL rather than on the screen.

**The prohibitions are half of it.** `docs/08` closes the administrator's
section with *Admin does not make scientific decisions*, and a screen that
merely did not *offer* an approval would not be the claim — an administrator's
token reaching the route would. So the operator's case asserts both: no such
action exists on the screen, and the routes those actions would call refuse the
administrator's own credentials.

**The bench's case runs the real chain.** A bench's screen is mostly about what
was handed over, and the only honest way to check that panel is to have RAVEL
actually hand something over: the node is prepared by the real materializer, the
run is a real workflow, and the package the screen names is the one the
materializer wrote. The two files are then sent *from the console* — typed into
the path field, with the output chosen from the contract's own list — and the
run ends because of what was recorded, not because the screen said so.

**What is scripted**: the Master turn, and nothing else. `live_gateway` hands
`create_app` a `ScriptedMasterPort`, so the composer reaches a `MasterPort`
without a model. Every keystroke below travels a real socket to a real
PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.e2e.conftest import Headless, LiveGateway
from tests.e2e.test_tui import World, console, notice_of, settle
from tests.integration.conftest import Prepared
from tests.integration.gateway.conftest import PASSWORD
from tests.support.lab import (
    LOG_BYTES,
    OUTPUTS,
    RAW_BYTES,
    a_bench_user,
    prepare_for_the_bench,
    start_and_wait,
)
from textual.pilot import Pilot
from textual.widgets import Input, Select, TabbedContent
from textual.widgets._select import InvalidSelectValueError

from ravel.domain.clock import utcnow
from ravel.domain.enums import NodeStatus, ProjectStatus, UserRole
from ravel.domain.services import SERVICE_NAMES, SUPERVISOR
from ravel.state.database import Database
from ravel.state.repositories.identity import MembershipRepository
from ravel.state.repositories.lab import LabHandoverRepository, arrived_outputs
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.repositories.services import ServiceRepository
from ravel.state.store import S3ArtifactStore
from ravel.tui.app import screen_for_role
from ravel.tui.widgets import StatusTable

pytestmark = [pytest.mark.phase11, pytest.mark.acceptance, pytest.mark.timeout(600)]

#: The person at the bench, by account name. A *user* with an account of their
#: own, because the console signs in the way a person does and the uploads the
#: lab door attributes are attributed to whoever holds the token.
BENCH = "bench-ada"

#: The two files the bench sends, keyed by the output each answers. The names
#: are the contract's — the ones `tests/support/lab.py` builds a run from.
DELIVERED = {OUTPUTS[0]: LOG_BYTES, OUTPUTS[1]: RAW_BYTES}


# ── The owner ───────────────────────────────────────────────────────────────


async def test_p11_09_the_owners_screen_carries_every_thing_the_item_lists(
    live_gateway: LiveGateway, world: World, database: Database
) -> None:
    """The item's owner list, panel by panel, and the four things it can *do*.

    Every panel is asserted through the text the widget holds, so what is
    checked is what somebody would read. The DAG table is checked by row count
    as well as by contents, because a table that silently rendered nothing
    would otherwise pass an `in` test against an empty string.

    The actions are here because a read-only console would satisfy the list of
    panels and be useless: Master's conversation, pause and resume, membership,
    and opening and switching projects. Pause and resume are confirmed against
    PostgreSQL rather than against the console's own redraw, and the keys are
    pressed in the order their focus allows — the membership keys before the
    composer takes the focus, `ctrl+n` after, because it is the one binding an
    `Input` does not swallow.
    """
    async with console(
        live_gateway,
        username="ada",
        project_id=world.owner_project.project_id,
        first_panel="master-focus",
    ) as (app, pilot, screen):
        # The project, Master, and what wants a person.
        focus = screen.panel("master-focus").plain
        assert world.owner_project.display_id in focus, focus
        assert "Find a dopant that raises conductivity by 15%." in focus, focus
        assert screen.panel("approvals").plain
        assert screen.panel("attention").plain

        # Execution, and the graph.
        assert screen.panel("execution").plain
        summary = screen.panel("dag-summary").plain
        assert "EXECUTING" in summary, summary
        assert "your role: PROJECT_OWNER" in summary, summary
        assert screen.query_one("#dag", StatusTable).row_count == 2

        # The record: decisions, reviews, research results, evidence.
        for name in ("decisions", "reviews", "research", "evidence"):
            assert screen.panel(name).plain, name

        # Membership, and who conferred it.
        assert "PROJECT_OWNER" in screen.panel("members").plain
        assert screen.query_one("#member-table", StatusTable).row_count == 2

        # Membership is on its own tab, and a key pressed elsewhere refuses
        # rather than acting on a tab nobody is looking at.
        screen.focus()
        await pilot.press("x")
        await settle(pilot, lambda: "press m" in notice_of(screen), what="the guard")
        await pilot.press("m")
        await settle(
            pilot,
            lambda: screen.query_one("#record", TabbedContent).active == "tab-membership",
            what="the membership tab",
        )
        await pilot.press("a")
        await settle(pilot, lambda: "Name somebody" in notice_of(screen), what="the grant guard")

        # Pause and resume, as keys, read back from the project row.
        await pilot.press("p")
        await settle(
            pilot,
            lambda: project_status_is(database, world.owner_project.project_id, "PAUSED"),
            what="the project to be paused",
        )
        await pilot.press("r")
        await settle(
            pilot,
            lambda: project_status_is(database, world.owner_project.project_id, "EXECUTING"),
            what="the project to be running again",
        )

        # Master's conversation, which is the panel the section says must feel
        # present: what is typed reaches a Master port and the answer comes back.
        live_gateway.master.reply = "The control sample is the place to start."
        screen.focus()
        await pilot.press("e")
        await settle(
            pilot,
            lambda: screen.query_one("#composer", Input).has_focus,
            what="the composer to take the focus",
        )
        screen.query_one("#composer", Input).value = "Where should the bench begin?"
        await pilot.press("enter")
        await settle(
            pilot,
            lambda: "The control sample is the place to start."
            in screen.panel("master-focus").plain,
            what="Master's answer",
        )
        assert live_gateway.master.heard == ["Where should the bench begin?"]

        # Opening a project is on the membership tab, and the console can then
        # reach it: `ctrl+n` moves between the memberships the app holds, so a
        # project opened afterwards is unreachable until they are read again.
        screen.query_one("#new-project-title", Input).value = "Second screen"
        await screen.action_new_project()
        await settle(
            pilot,
            lambda: "Press ctrl+n to move to it." in notice_of(screen),
            what="the new project to be opened",
        )
        assert app.project_id == world.owner_project.project_id, (
            "opening a project moved the reader out of the one they were in"
        )
        await pilot.press("ctrl+n")
        await settle(
            pilot,
            lambda: app.project_id != world.owner_project.project_id,
            what="the console to move to the project just opened",
        )

    # The new project's owner is the person who opened it, which is the one
    # membership nobody confers.
    opened = str(app.project_id)
    with database.read_only() as session:
        owners = MembershipRepository(session, opened).owners()
        project = ProjectRegistry(session).get(opened)
    assert [owner.user_id for owner in owners] == [world.owner]
    assert project.title == "Second screen"
    assert project.status is ProjectStatus.CREATED


def project_status_is(database: Database, project_id: str, wanted: str) -> bool:
    """Whether the project row says so, read from PostgreSQL and not the screen."""
    with database.read_only() as session:
        return ProjectRegistry(session).get(project_id).status.value == wanted


# ── The bench ───────────────────────────────────────────────────────────────


async def test_p11_09_the_benchs_screen_hands_the_work_back_through_the_console(
    headless: Headless,
    prepare: Callable[..., Prepared],
    live_gateway: LiveGateway,
    database: Database,
    artifact_store: S3ArtifactStore,
    tmp_path: Path,
) -> None:
    """The bench's whole surface, on a run that has genuinely reached a bench.

    The chain is walked for real: a contract built from terms the materializer
    accepts, a run started through the activities a run uses, a durable wait,
    and a package written to this machine. Then the console is opened on it and
    what the prepared panel shows is the package's own manifest, the outputs the
    contract owes, and the fact that none of them have arrived — which is the
    item's *prepared protocol* and *required outputs* as one reading.

    **The bench then does its work through the console.** Two files, named in
    the path field, each attached to an output chosen from the contract's own
    list. After the first, the panel splits — one sent, one still owed — and the
    node is *still waiting*: a file that answers half of what was asked does not
    finish anything, which is the directive's rule that a delivery is measured
    against `required_outputs` and not against having received something. After
    the second, the real workflow reads what is recorded, closes the handover and
    moves the node to the seat that judges it. Nothing here is asserted from the
    screen alone: the artifacts are read back out of the store, and the verdict
    is the run's.
    """
    prepared = prepare_for_the_bench(headless, prepare)
    await start_and_wait(headless, prepared)
    bench_id = a_bench_user(
        database, prepared.project_id, username=BENCH, password=PASSWORD
    )

    first, second = OUTPUTS
    async with console(
        live_gateway,
        username=BENCH,
        project_id=prepared.project_id,
        first_panel="instruction",
    ) as (_app, pilot, screen):
        # The task, and the contract it is worked under.
        tasks = screen.query_one("#tasks", StatusTable)
        assert tasks.row_count == 1, "the bench's task list is not the experiment"
        assert prepared.node.display_id in str(tasks.get_row_at(0)[1])

        instruction = screen.panel("instruction").plain
        assert prepared.contract.objective in instruction, instruction
        assert "required outputs: " in instruction, instruction

        # The prepared protocol: written by the materializer, named by its
        # manifest, and split into what has arrived and what has not.
        handed = screen.panel("prepared").plain
        assert "prepared by" in handed, handed
        for output in OUTPUTS:
            assert output in handed, handed
        assert handed.count("still owed") == len(OUTPUTS), handed
        assert "sent" not in handed, "nothing has been delivered yet"
        assert "WAITING_EXTERNAL" in screen.panel("status").plain, screen.panel("status").plain

        # Nothing has been said to this bench, and nothing reported to it.
        assert screen.panel("messages").plain == (
            "The Worker has not sent anything about this task."
        )
        assert screen.panel("reported").plain == (
            "Nothing has been reported against this task."
        )

        # The upload control offers exactly the contract's outputs — no more and
        # no less. Asserted by what the field *accepts*, which is the public
        # half of it: every name the contract required can be chosen, and a
        # filename the contract never asked for cannot be, so a file can only be
        # attached to an output that was actually owed.
        choosing = screen.query_one("#upload-output", Select)
        for output in OUTPUTS:
            choosing.value = output
            assert choosing.value == output
        with pytest.raises(InvalidSelectValueError):
            choosing.value = "conductivity.csv"

        # ── One file, sent from the console ────────────────────────────────
        await send(pilot, screen, tmp_path, first, DELIVERED[first])
        after_one = screen.panel("prepared").plain
        assert f"✓ {first} — sent" in after_one, after_one
        assert f"◐ {second} — still owed" in after_one, after_one
        assert headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL, (
            "a file that answers half of what the contract owed finished the run"
        )

        # ── And the other, which is what finishes it ───────────────────────
        await send(pilot, screen, tmp_path, second, DELIVERED[second])
        after_two = screen.panel("prepared").plain
        assert "still owed" not in after_two, (
            f"the panel still shows work outstanding after both files: {after_two}"
        )
        await settle(
            pilot,
            lambda: headless.status_of(prepared.node) is NodeStatus.REVIEWING,
            what="the run to read the delivery and hand the work to a verdict",
        )

    # What the console did, read from where it was written rather than from the
    # screen: the artifacts answer the handover's own owed names, and they are
    # attributed to the person who sent them.
    with database.read_only() as session:
        handover = LabHandoverRepository(session, prepared.project_id).latest_for_node(
            prepared.node_id
        )
        assert handover is not None
        arrived = arrived_outputs(session, handover)
        repository = ArtifactRepository(session, prepared.project_id, artifact_store)
        for name, artifact in arrived:
            assert artifact.created_by == bench_id, (
                "the record does not say who sent what the bench produced"
            )
            versions = repository.versions(artifact.artifact_id)
            assert repository.read(versions[-1]) == DELIVERED[name]

    assert [name for name, _artifact in arrived] == list(OUTPUTS), (
        "the handover's own read of what arrived does not match what the contract owed"
    )


async def send(
    pilot: Pilot[None], screen: Any, directory: Path, output: str, body: bytes
) -> None:
    """Type a file and an output into the bench's two controls and press the key.

    The file is written to disk first because that is what the screen's field
    names — a path, not a blob — and the round trip through a real filesystem is
    part of what is being checked. The key is pressed rather than the action
    called, so what is exercised is the binding a person actually has.

    Two things are waited for and the second is not a formality. The notice comes
    back as soon as the Gateway answers; the *panel* is redrawn afterwards by a
    second read, so a case that stopped at the notice would be reading the panel
    as it stood before the file arrived — and would then be asserting about a
    stale screen rather than about the one a person is looking at.
    """
    written = directory / f"{output}.csv"
    written.write_bytes(body)
    screen.query_one("#upload-path", Input).value = str(written)
    screen.query_one("#upload-output", Select).value = output
    screen.focus()
    await pilot.press("u")
    await settle(
        pilot, lambda: "Sent " in notice_of(screen), what=f"the console to send {output}"
    )
    assert "refused" not in notice_of(screen), notice_of(screen)
    await settle(
        pilot,
        lambda: f"✓ {output} — sent" in screen.panel("prepared").plain,
        what=f"the prepared panel to show {output} as sent",
    )


# ── The operator ────────────────────────────────────────────────────────────


async def test_p11_09_the_operators_screen_carries_every_thing_the_item_lists(
    live_gateway: LiveGateway, world: World, database: Database
) -> None:
    """The admin's questions — the DSH runtime, Temporal, the supervisor, the
    backends, the cluster, the jobs, the recoveries and the logs — each on its
    own panel.

    Two of them are about absences and both say so in words, which is the point
    rather than a shortcut: on a project nothing has run in, "no backend job has
    ever been submitted" and "no run has had to be recovered" are the answers,
    and a panel that rendered blank would be indistinguishable from one whose
    route had failed.

    The processes panel is the one answer that comes from a process rather than
    from a record, and it is asserted with a report actually written: the row is
    inserted the way the supervisor inserts it, so what the panel draws is the
    Gateway's judgement about a real heartbeat — and the other two services,
    which have reported nothing, are still named rather than read as dead.
    """
    with database.transaction() as session:
        ServiceRepository(session).report(
            SUPERVISOR,
            started_at=utcnow(),
            detail={"projects": [world.admin_project.project_id], "poll_seconds": 5},
        )
    async with console(
        live_gateway,
        username="root",
        project_id=world.admin_project.project_id,
        first_panel="harness",
    ) as (_app, _pilot, screen):
        services = screen.panel("services").plain
        assert "supervisor — alive" in services, services
        assert "including this one" in services, services
        assert "has never reported here" in services, (
            "a service that has never reported is not a service that died"
        )
        for name in SERVICE_NAMES:
            assert name in services, services

        harness = screen.panel("harness").plain
        assert "DeepSeek Harness" in harness, harness
        assert "dsh-v" in harness, harness

        assert "task queue" in screen.panel("temporal").plain

        backends = screen.panel("backends").plain
        assert "No backend has been handed any of this project's work yet." in backends
        assert "reachability: not probed from the Gateway" in backends, backends
        assert "authentication" in backends, backends
        assert "no Slurm cluster is configured" in backends, backends

        assert screen.panel("jobs").plain == (
            "No backend job has ever been submitted for this project."
        )
        assert screen.panel("reconciliation").plain == (
            "No run has had to be recovered in this project."
        )
        assert "nodes 0" in screen.panel("runtime").plain
        assert "dsh home" in screen.panel("logs").plain


# ── What the operator may not do ────────────────────────────────────────────


async def test_p11_09_an_administrator_can_neither_approve_nor_mutate_anything(
    live_gateway: LiveGateway, world: World
) -> None:
    """*Admin does not make scientific decisions*, from both ends of the wire.

    From the screen: no method on the administrator's console writes, asserted by
    there being no such name rather than by a comment saying so. From the routes:
    the administrator's *own token* is refused the project's life cycle, the
    approval door, the envelope, the membership and the DAG's own mutation — the
    half that matters, because a screen can always be worked around and a route
    cannot.

    The DAG is read back afterwards and compared whole, so that "did not mutate"
    covers a node that was cancelled or re-bound as well as one that was added.
    """
    forbidden = (
        "action_approve",
        "action_resolve_approval",
        "action_pause",
        "action_resume",
        "action_set_envelope",
        "action_add_member",
        "action_withdraw_member",
        "action_new_project",
        "action_say",
        "action_mutate",
    )
    async with console(
        live_gateway,
        username="root",
        project_id=world.admin_project.project_id,
        first_panel="harness",
    ) as (_app, _pilot, screen):
        for name in forbidden:
            assert not hasattr(screen, name), f"the operator's screen grew {name}"

    project_id = world.admin_project.project_id
    async with httpx.AsyncClient(base_url=live_gateway.url) as admin_client:
        pair = (
            await admin_client.post(
                "/auth/login", json={"username": "root", "password": PASSWORD}
            )
        ).json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        before = (await admin_client.get(f"/projects/{project_id}/dag", headers=headers)).json()

        for path, body in (
            (f"/projects/{project_id}/pause", {"reason": "trying it on"}),
            (f"/projects/{project_id}/resume", {}),
            (f"/projects/{project_id}/envelope", {}),
            (f"/projects/{project_id}/members", {"username": "ada", "role": "LAB_USER"}),
            (f"/projects/{project_id}/messages", {"body": "do this instead"}),
            (f"/projects/{project_id}/dag/nodes", {"node_type": "EXPERIMENT"}),
        ):
            refused = await admin_client.post(path, headers=headers, json=body)
            assert refused.status_code in {403, 404}, (
                f"{path}: {refused.status_code} {refused.text}"
            )

        after = (await admin_client.get(f"/projects/{project_id}/dag", headers=headers)).json()

    assert after == before, "an administrator's token changed the Scientific DAG"


# ── And the three are not the same screen ───────────────────────────────────


def test_p11_09_no_two_roles_are_drawn_by_the_same_screen() -> None:
    """The item's word is *genuinely different*, and this is that word checked.

    A mapping of three roles onto three classes would still be satisfied by a
    mapping onto one class three times, which is what "one screen with fields
    hidden" looks like from the outside. So the classes are asserted distinct —
    and each one is asserted to bring its own bindings, because two subclasses
    that overrode nothing would be the same screen wearing two names.
    """
    drawn = {role: screen_for_role(role.value) for role in UserRole}

    assert set(drawn) == set(UserRole), "a role exists that no screen is drawn for"
    assert len({screen_type for screen_type in drawn.values()}) == len(UserRole), (
        "two roles are drawn by the same screen, so the surfaces are not different"
    )
    for screen_type in drawn.values():
        assert screen_type is not None
        assert screen_type.BINDINGS, screen_type
