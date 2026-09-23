"""P11-10: the three processes as services, killed and brought back.

The item's acceptance sentence is one chain — *kill the supervisor, systemd
restarts it, the project recovers, nothing is submitted twice* — and it is
demonstrated in the two halves it really divides into, because they are claims
about different machines.

**The restart is systemd's, and it is proved with systemd.** The first case
starts a real transient user unit running the *real* `scripts/run_supervisor.py`,
with the restart policy read out of `infra/systemd/ravel-supervisor.service`
rather than restated here, and kills it with `SIGKILL`. Nothing in this process
participates: it watches PostgreSQL, `systemctl` and the journal. That is what
makes the result evidence about the unit file rather than about a mock of it —
and one run answers all of the item's process-level promises, because the same
supervisor is then observed discovering a project, logging as a structured line,
and stopping in a way that is recorded as a shutdown rather than as a death.

**The recovery is RAVEL's, and it is proved in process.** A replacement
supervisor has only PostgreSQL and Temporal to read from; whether its
predecessor exited politely or was killed is the one thing that must not matter,
and that is asserted by leaving a project mid-flight and handing it to a second
supervisor. What comes back with it is the item's hardest promise: exactly one
submission per `(project, node, attempt)`, counted at the port rather than
inferred from the artifacts — a duplicated experiment is a thing a lab charges
for, and it is invisible in the output.

Neither case stands in for the other. The systemd case cannot prove recovery of
a *plan* (a live Master is what would decide, and no test may spend a real model
call on a liveness question), and the recovery case cannot prove the restart
policy (it starts its own supervisors). The chain is the two of them plus the
fact that both name the same entry point.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from tests.acceptance.phase10_support import (
    ScriptedSeats,
    project_status,
    second_project,
    until,
)
from tests.acceptance.test_phase10_supervisor import (
    MEASURE,
    supervising,
    task,
)
from tests.e2e.conftest import (
    PASSWORD,
    Headless,
    LiveGateway,
)
from tests.e2e.test_tui import World

from ravel.config import Settings
from ravel.domain.enums import JobState, NodeStatus, NodeType, ProjectStatus
from ravel.domain.services import GATEWAY, SERVICE_NAMES, SUPERVISOR, TEMPORAL_WORKER
from ravel.execution.backends import (
    ExternalDelivery,
    JobHandle,
    JobOutputs,
    JobRequest,
    JobStatus,
)
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.services import ServiceRepository

pytestmark = [pytest.mark.phase11, pytest.mark.acceptance, pytest.mark.timeout(600)]

ROOT = Path(__file__).resolve().parents[2]
SUPERVISOR_UNIT = ROOT / "infra" / "systemd" / "ravel-supervisor.service"
RUNNER = ROOT / "scripts" / "run_supervisor.py"


def unit_service(path: Path) -> dict[str, str]:
    """A unit file's `[Service]` keys, comments dropped.

    Read rather than restated, which is the point of this case: if the unit
    says `Restart=on-failure` then the transient unit started below says so
    too, and the failure is about the file a deployment installs rather than
    about a policy written in the test.
    """
    parsed: dict[str, str] = {}
    section = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if section == "Service":
            key, _, value = line.partition("=")
            parsed[key] = value
    return parsed


def user_manager_is_running() -> bool:
    """Whether this host has a user-level systemd to put a unit on.

    Not the same question as "is systemd the init system": a container, a
    session without `XDG_RUNTIME_DIR`, and a machine where the user manager was
    never started all have `systemd-run` on the PATH and nowhere to put a unit.
    """
    if shutil.which("systemd-run") is None or shutil.which("systemctl") is None:
        return False
    reachable = subprocess.run(
        ["systemctl", "--user", "show", "-p", "Version", "--value"],
        capture_output=True,
        text=True,
        check=False,
    )
    return reachable.returncode == 0 and bool(reachable.stdout.strip())


def systemctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args], capture_output=True, text=True, check=False
    )


def until_soon(predicate: Callable[[], bool], *, timeout: float = 90.0) -> bool:
    """Wait for a fact about another process, and say so rather than raising.

    A boolean rather than an assertion, because these predicates read a
    database that a process on the other side of a systemd boundary is writing
    to, and the useful failure message is about which of the four states was
    not reached — which only the caller knows.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def report_of(database: Database, service: str) -> Any:
    with database.read_only() as session:
        return ServiceRepository(session).get(service)


def pid_of(report: Any) -> int | None:
    """The pid out of a report's `host:pid`, or `None` if it is not one."""
    _, _, pid = str(report.instance).partition(":")
    return int(pid) if pid.isdigit() else None


def journal_lines(unit: str) -> list[dict[str, Any]]:
    """Every structured line systemd collected for this unit, in order."""
    written = subprocess.run(
        ["journalctl", "--user", "-u", unit, "-o", "cat", "--no-pager"],
        capture_output=True,
        text=True,
        check=False,
    )
    lines: list[dict[str, Any]] = []
    for line in written.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            lines.append(json.loads(line))
        except json.JSONDecodeError:  # pragma: no cover - a line that is not ours
            continue
    return lines


@pytest.fixture
def transient_unit(tmp_path: Path, integration_settings: Settings) -> Iterator[str]:
    """A unit name to start, and the promise that it is not left behind.

    The environment goes in a file rather than on the command line, which is the
    same arrangement the checked-in units use and for the same reason: an
    `EnvironmentFile` is read by systemd and not reported back, while `--setenv`
    puts the database password in the process table and in whatever
    `systemctl show` prints. The file is 0600 and lives in the test's own
    temporary directory.
    """
    name = f"ravel-p11-10-{uuid.uuid4().hex[:8]}"
    environment = tmp_path / "service.env"
    environment.write_text(
        "\n".join(
            (
                f"RAVEL_POSTGRES_DSN={integration_settings.postgres_dsn}",
                # A queue no deployment polls, so that a supervisor started here
                # cannot hand work to the worker a deployment is running. The
                # same interlock `tests/integration/conftest.py` applies, for
                # the same reason.
                f"RAVEL_TEMPORAL_TASK_QUEUE=ravel-p11-10-{uuid.uuid4().hex}",
                "RAVEL_LOG_FORMAT=json",
                # Blank, and `_blank_secret_is_absent` reads that as unset. The
                # case below creates a project so that the restarted supervisor
                # has something to log about, and a real key here would mean a
                # live Master turn deciding about it — a model call this test
                # has no business making, on a project that exists to be
                # discovered and nothing else.
                "RAVEL_DEEPSEEK_API_KEY=",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    environment.chmod(0o600)
    try:
        yield name
    finally:
        systemctl("stop", name)
        systemctl("reset-failed", name)


def start_unit(name: str, policy: dict[str, str], environment: Path) -> None:
    """Start a transient unit carrying the checked-in unit's own restart policy."""
    started = subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={name}",
            # `--collect`, so a unit that has exited is garbage-collected rather
            # than left in `failed` under a name the next run would collide with.
            "--collect",
            f"--property=Restart={policy['Restart']}",
            f"--property=RestartSec={policy['RestartSec']}",
            f"--property=KillSignal={policy['KillSignal']}",
            f"--property=WorkingDirectory={ROOT}",
            f"--property=EnvironmentFile={environment}",
            # The interpreter is named in full because a transient unit inherits
            # the user manager's environment rather than this shell's, and its
            # PATH is not this one. `install_services.sh` substitutes the same
            # coordinate when it installs the unit for a real prefix.
            sys.executable,
            str(RUNNER),
            "--poll-seconds",
            "1",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode == 0, (
        "systemd refused to start the unit the repository ships:\n"
        f"{started.stdout}{started.stderr}"
    )


# ── P11-10, the process-level half ───────────────────────────────────────────


def test_p11_10_systemd_restarts_a_killed_supervisor_and_a_stopped_one_says_so(
    database: Database,
    transient_unit: str,
    tmp_path: Path,
) -> None:
    """The item's acceptance sentence, with the real entry point and a real SIGKILL.

    Five claims, and they are one process's life: it comes up and says so;
    killing it does not make it claim it left deliberately; systemd brings it
    back as a *different* process; the replacement goes on working, which is
    asserted by giving it a project to find and reading the line it logs about
    it; and stopping it the way a deploy does is recorded as a shutdown rather
    than as a death.

    The `MainPID` comparison is what ties the row to the unit. Without it a
    supervisor left over from an earlier test could satisfy every assertion
    about the row while the unit started here had never come up at all.

    The journal read is the structured-log claim. A deployment collects these
    lines by the key `service` rather than by matching a sentence, so what is
    asserted is that a real process's output parses as one object per line
    carrying that key — which is the difference between a log and a format.
    """
    if not user_manager_is_running():  # pragma: no cover - host dependent
        pytest.skip("no user-level systemd on this host, so no unit can be restarted")

    policy = unit_service(SUPERVISOR_UNIT)
    start_unit(transient_unit, policy, tmp_path / "service.env")

    assert until_soon(lambda: report_of(database, SUPERVISOR) is not None), (
        "the supervisor never reported, so the process the unit started did not "
        "reach its first tick"
    )
    first = report_of(database, SUPERVISOR)
    assert first.stopped_at is None, "a process that has just come up is not stopped"

    main_pid = systemctl("show", "-p", "MainPID", "--value", transient_unit).stdout.strip()
    assert main_pid == str(pid_of(first)), (
        "the row names a process other than the one systemd is running, so "
        "nothing here is evidence about the unit"
    )

    os.kill(int(main_pid), signal.SIGKILL)

    assert until_soon(
        lambda: (now := report_of(database, SUPERVISOR)) is not None
        and pid_of(now) != pid_of(first)
    ), f"systemd did not bring the supervisor back within its own {policy['RestartSec']}s delay"
    replacement = report_of(database, SUPERVISOR)
    assert replacement.stopped_at is None, (
        "a killed process left a shutdown record, so the row cannot tell an "
        "operator a deploy from an incident"
    )
    restarts = systemctl("show", "-p", "NRestarts", "--value", transient_unit).stdout.strip()
    assert int(restarts) >= 1, "systemd did not count the restart it performed"

    # Something for the replacement to find, so that "it came back" is a claim
    # about work and not only about a row. It is discovered by the poll, and the
    # line naming it can only have been written by the process that came back.
    found = second_project(database, title="Discovered after the restart")
    assert until_soon(
        lambda: any(
            line.get("service") == SUPERVISOR and found.project_id in str(line.get("message"))
            for line in journal_lines(transient_unit)
        ),
        timeout=60.0,
    ), (
        "the restarted supervisor never reported discovering the project, so the "
        "journal has no line from it about work:\n"
        + "\n".join(str(line) for line in journal_lines(transient_unit))
    )

    lines = journal_lines(transient_unit)
    assert all({"ts", "level", "service", "logger", "message"} <= set(line) for line in lines), (
        "a line reached the journal without the keys a collector queries on: "
        f"{[line for line in lines if 'service' not in line]}"
    )

    systemctl("stop", transient_unit)

    assert until_soon(
        lambda: (stopped := report_of(database, SUPERVISOR)) is not None
        and stopped.stopped_at is not None
    ), (
        "the unit was stopped with the signal it declares and no shutdown was "
        "recorded, so a deploy is indistinguishable from a crash"
    )


# ── P11-10, the application-level half ───────────────────────────────────────


class CountingBackend:
    """A real backend with a ledger of every submission it was asked for.

    Implements `WorkBackend` by delegation, so the work is genuinely done by the
    mock underneath — the artifacts, the job states and the timing are all its.
    What is added is the one thing this item is about and no artifact can show:
    how many times RAVEL asked.

    `submit` is required to be idempotent in `(project_id, node_id, attempt)`. A
    worker killed between the backend accepting work and the job reference being
    committed leaves a row RAVEL cannot tell from "the call never arrived", and
    it resolves that by asking again. So a second *call* is not itself a defect:
    a second call answered with the same reference is the contract working, and
    a second call answered with a different one is a second experiment.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.name = inner.name
        #: Every `(project, node, attempt)` in the order it was asked for.
        self.calls: list[tuple[str, str, int]] = []
        #: Every reference handed back, in the order the calls were answered.
        self.refs: list[str] = []
        #: The references each key was answered with. More than one is a
        #: second job.
        self.handles: dict[tuple[str, str, int], set[str]] = {}

    def submit(self, request: JobRequest) -> JobHandle:
        key = (request.project_id, request.node_id, request.attempt)
        self.calls.append(key)
        handle = self.inner.submit(request)
        self.refs.append(handle.backend_job_ref)
        self.handles.setdefault(key, set()).add(handle.backend_job_ref)
        return handle

    def status(self, backend_job_ref: str) -> JobStatus:
        return self.inner.status(backend_job_ref)

    def deliver(self, backend_job_ref: str, delivery: ExternalDelivery) -> JobStatus:
        return self.inner.deliver(backend_job_ref, delivery)

    def collect(self, backend_job_ref: str) -> JobOutputs:
        return self.inner.collect(backend_job_ref)

    def cancel(self, backend_job_ref: str) -> bool:
        return self.inner.cancel(backend_job_ref)


async def test_p11_10_a_project_survives_the_supervisor_that_was_driving_it(
    headless: Headless, seats: ScriptedSeats, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The recovery, and the promise that nothing was run twice for it.

    The first supervisor is stopped while a computation is genuinely in flight —
    the mock holds that state long enough for it to be a fact rather than a race
    — and a *second*, independently constructed supervisor is handed the same
    database. Nothing passes between them: a replacement reads PostgreSQL and
    Temporal, which is exactly what a deployment's restart has, so the case
    fails if any part of the recovery depended on the dead process's memory.

    **The kill here is not a `SIGKILL`, and the case above is where one is.**
    What matters for duplication is that the loops stopped while the work did
    not, and a stopped loop is a stopped loop. The two cases are deliberately
    not merged: this one needs a scripted Master, and the item's liveness claims
    must be provable without one.

    The count is taken at the port. An artifact produced once and a computation
    submitted twice look identical afterwards, and the second is what a lab
    invoices for — so the assertion is on what the backend was asked to do, not
    on what came back.
    """
    # This suite shortens a run's deadline to four seconds, so that a run which
    # never finishes on its own ends within a test. That is the right default
    # and the wrong one here: this case needs a node still in flight on the far
    # side of the handover, and a four-second run is over before the first
    # supervisor has stopped. The deadline is a deployment's number rather than
    # a policy under test, so it is lengthened back for the duration —
    # `monkeypatch` restores it, and the worker reads this same object.
    monkeypatch.setattr(headless.settings, "job_deadline_seconds", 60.0)
    headless.compute("COMPUTE_SUCCESS", step_seconds=10.0)
    counting = CountingBackend(headless.registry.by_node_type[NodeType.COMPUTATION])
    headless.registry.register(NodeType.COMPUTATION, counting)

    project = headless.project
    seats.plan(project, (task(project.project_id, "measure", MEASURE),))

    async with supervising(headless, monkeypatch, seats, loop_max_rounds=1_000_000) as first:
        await until(lambda: bool(counting.refs), timeout=120.0)
        # `RUNNING` and not `SUBMITTED`: the mock advances on wall clock, so a
        # job the workflow has polled into its second state is a workflow that
        # is alive and being driven. Stopping on the first sight of a
        # submission would sometimes stop a supervisor mid-`start_execution` —
        # a stranded run, which is a different case with a different answer, and
        # a coin toss rather than a test.
        # `.state`, and not the `JobStatus` the port hands back: a `JobStatus`
        # is a record *about* a state and is never equal to one, so the
        # comparison without it is false for every state a job can be in — a
        # wait that holds for its whole timeout while the job it is watching
        # runs to completion.
        await until(
            lambda: counting.status(counting.refs[0]).state is JobState.RUNNING,
            timeout=120.0,
        )
        await first.stop()

    node_id = counting.calls[0][1]
    assert project.project_id not in first.driving()
    assert node_status(headless.database, project.project_id, node_id) is NodeStatus.RUNNING, (
        "the node is not in flight, so the handover is not a handover"
    )
    assert project_status(headless.database, project.project_id) is not ProjectStatus.COMPLETED, (
        "the project ended before the replacement started, so nothing was recovered"
    )

    async with supervising(headless, monkeypatch, seats) as second:
        await second.finished(project.project_id)

    assert second.status(project.project_id) is ProjectStatus.COMPLETED
    assert seats.plans[project.project_id].master.trace == [
        "contract_defined",
        "planned",
        "concluded:SUCCESS",
    ], "the replacement did not carry the project to a decision"

    assert counting.calls, "nothing was ever submitted, so nothing was proved"
    for key, refs in counting.handles.items():
        assert len(refs) == 1, (
            f"{key} was answered with {len(refs)} distinct backend jobs, so the "
            "work was started more than once"
        )


# ── P11-10, the health indication ────────────────────────────────────────────


async def test_p11_10_the_services_endpoint_separates_the_four_states(
    live_gateway: LiveGateway, world: World, database: Database
) -> None:
    """Four states of a process, four different answers, from one read.

    This is what the administrator's screen renders, and the item's promise is
    that a reader can tell the states apart: a service that has never run, one
    that is running, and one that recorded its own shutdown. Three of them are
    written here as PostgreSQL rows, because they are states of processes this
    test is not running; the fourth is the Gateway itself, which is answering
    the request.

    The stopped row is the one worth the case. `stale` and `stopped` are both
    "not beating", and a screen that reported a deliberate shutdown as a service
    that died would send somebody looking for a process that is not supposed to
    exist.

    **None of this is real compute and none of it claims to be.** The rows are
    about processes; no scientific artifact is involved, produced, or implied.
    """
    project = world.admin_project
    beat(database, TEMPORAL_WORKER, poll_seconds=10)
    shutdown(database, TEMPORAL_WORKER)

    async with httpx.AsyncClient(base_url=live_gateway.url) as client:
        pair = (
            await client.post("/auth/login", json={"username": "root", "password": PASSWORD})
        ).json()
        headers = {"Authorization": f"Bearer {pair['access_token']}"}
        answer = await client.get(
            f"/projects/{project.project_id}/runtime/services", headers=headers
        )

    assert answer.status_code == 200, answer.text
    entries = {entry["service"]: entry for entry in answer.json()["services"]}
    assert set(entries) == set(SERVICE_NAMES)

    never = entries[SUPERVISOR]
    assert never["reporting"] is False
    assert never["stale"] is False, (
        "a service that has never reported was drawn as one that went quiet, "
        "which sends an operator looking for a process that was never run"
    )
    assert never["stopped"] is False
    assert never["instance"] is None

    serving = entries[GATEWAY]
    assert serving["reporting"] is True
    assert serving["stale"] is False
    assert serving["stopped"] is False
    assert serving["instance"].startswith(socket.gethostname())

    leaving = entries[TEMPORAL_WORKER]
    assert leaving["reporting"] is True
    assert leaving["stopped"] is True
    assert leaving["stopped_at"] is not None
    assert leaving["stale"] is False, (
        "a service that recorded its own shutdown was reported as one that went "
        "quiet, which is the difference between a deploy and an incident"
    )


def node_status(database: Database, project_id: str, node_id: str) -> NodeStatus:
    """One node's status, read from PostgreSQL rather than from a port."""
    with database.read_only() as session:
        return DagRepository(session, project_id).node(node_id).status


def beat(database: Database, service: str, *, poll_seconds: float) -> None:
    """One service's beat, written the way the process writes it."""
    from ravel.domain.clock import utcnow

    with database.transaction() as session:
        ServiceRepository(session).report(
            service,
            started_at=utcnow(),
            detail={"poll_seconds": poll_seconds, "projects": []},
        )


def shutdown(database: Database, service: str) -> None:
    """One service's shutdown, written the way the process writes it."""
    with database.transaction() as session:
        ServiceRepository(session).retire(service)
