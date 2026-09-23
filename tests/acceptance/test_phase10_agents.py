"""Phase 10 items about the Workers: live turns, bounded authority, dormancy.

Phase 9's loop sequenced three powers and left the Workers as Temporal
activities. Phase 10 puts a *session* in each Worker seat: an agent that reads a
live node's frozen contract and record, and is where a `request_action` or a
`send_message` comes from.

These cases are about what that agent is and is not. It is dispatched for a node
that is in somebody's hands, one turn per episode rather than one per poll; its
tools are the contract's and not the plan's; and a session that goes idle is
closed and recovered rather than resident for the life of a deployment.

The backend stays a mock and the run stays Temporal's, which is the other half
of the same item: putting an agent in the seat does not move execution into the
model.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from tests.acceptance.phase10_support import (
    FakeHarness,
    project_status,
    seat_scope,
    seat_tool,
    second_project,
)
from tests.e2e.conftest import Headless
from tests.e2e.test_headless_loop import Nodes
from tests.integration.conftest import Prepared, build_prepared
from tests.integration.temporal.conftest import await_state

from ravel.backends import MockComputeBackend, catalogue
from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    CompletionStatus,
    DecisionType,
    FailureClass,
    JobState,
    NodeStatus,
    NodeType,
    ProjectStatus,
    ReviewCheckpoint,
    ReviewOutcome,
    WorkerMessageKind,
)
from ravel.domain.planning import HorizonError
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import SEATED_NODE_TYPES, unexecutable_reason
from ravel.dsh import runtime as runtime_module
from ravel.dsh.agents import HarnessAgent, WorkerAgent
from ravel.dsh.pool import create_pool
from ravel.dsh.roles import definition_for
from ravel.execution.loop import ProjectLoop, Situation, read_situation
from ravel.execution.node_runs import ExecutionService
from ravel.execution.temporal.contracts import ExternalResult
from ravel.mcp.context import ToolContext
from ravel.mcp.registry import DAG_MUTATION_TOOLS, mutating_tools_for, tools_for
from ravel.mcp.tools import IMPLEMENTATIONS
from ravel.state.database import Database
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
    ResearchContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import RoadmapRepository
from ravel.state.repositories.records import (
    BackendJobRepository,
    DecisionRepository,
    DeviationRepository,
    RecordRepositories,
    ReviewRepository,
)
from ravel.state.repositories.research import ResearchRecordRepository

pytestmark = [pytest.mark.phase10, pytest.mark.timeout(600)]

#: What each kind of node is asked to produce.
COMPUTE_OUTPUTS = ("conductivity.csv", "notes.json")
LAB_OUTPUTS = ("experiment_log", "raw_data")

#: One COMPUTATION node as a model states it, for the cases about planning
#: rather than about the work: the criteria are the shape a plan has to have,
#: and nothing here runs.
A_BASELINE_NODE: dict[str, Any] = {
    "node_type": "COMPUTATION",
    "objective": "Measure the baseline series.",
    "criteria": [
        {
            "statement": "The baseline is measured on the same instrument across the series.",
            "provenance": "user_requirement",
        }
    ],
    "allowed_actions": ["run_simulation"],
    "required_outputs": list(COMPUTE_OUTPUTS),
}

ACTOR = "phase10-worker"

#: How long the mock is told to take between states, in the cases about a node
#: that is *still* live. `Headless.compute` steps every fifth of a second, which
#: is right for a test that waits a scenario out and wrong for these: what they
#: assert is what a Worker is handed while a run is in flight, and a backend
#: that finished while the test was connecting to Temporal would answer a
#: different question. The scenario is still the real one; only its clock is
#: slower.
STILL_RUNNING_SECONDS = 600.0


class RecordingWorker:
    """An `ExecutorPort` that records the turns the loop gives it.

    The cases here are about which seat is handed which node, so the seat
    records and does nothing. It still implements `start`, because that is what
    a Worker does rather than something it may decline to do — a seat without
    one is not a Worker — and a case that wants runs to actually begin drives
    them itself, which is how these tests keep the loop out of the picture.
    """

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.started: list[str] = []

    async def start(self, node: DagNode) -> None:
        self.started.append(node.node_id)

    async def act(self, node: DagNode, situation: Situation) -> None:
        _ = situation
        self.turns.append((node.node_id, node.status.value))

    def close(self) -> None:
        """A recording port holds no runtime, so there is nothing to close."""


class Idle:
    """A seat this case is not about, scripted to do nothing at all.

    Master and Review are idle in the dispatch cases because the question there
    is who the *Workers* are handed: a scripted Master would plan nodes of its
    own and a scripted Review would move them on, and both would make an
    assertion about a Worker's turn read as an assertion about a script.
    """

    async def act(self, situation: Situation) -> None:
        _ = situation


def a_run_that_stays_live(headless: Headless, scenario: str) -> None:
    """Send computation nodes to the real mock, in a state that does not move on.

    The same scenario the catalogue names, registered the way `Headless.compute`
    registers it and with one number changed: the backend's own clock.
    """
    headless.registry.register(
        NodeType.COMPUTATION,
        MockComputeBackend(
            database=headless.database,
            store=headless.store,
            scenario=scenario,
            step_seconds=STILL_RUNNING_SECONDS,
        ),
    )


async def loop_for(
    headless: Headless,
    compute: RecordingWorker,
    experimental: RecordingWorker,
    research: RecordingWorker | None = None,
) -> ProjectLoop:
    """The real loop, with the two agent seats idle and the seats recording."""
    return ProjectLoop(
        database=headless.database,
        project_id=headless.project.project_id,
        master=Idle(),
        review=Idle(),
        compute_worker=compute,
        experimental_worker=experimental,
        research=research or RecordingWorker(),
        poll_seconds=0.01,
    )


def worker_context(
    headless: Headless, role: AgentRole, project_id: str | None = None
) -> ToolContext:
    """A Worker's tool context over the real stack, at the real task queue.

    The service is the same object a deployed tool server builds from its
    environment; what the test supplies is the settings, because it is they
    that name the queue this test's Temporal worker is polling.
    """
    return seat_scope(
        headless.database,
        project_id or headless.project.project_id,
        role,
        execution=ExecutionService(database=headless.database, settings=headless.settings),
    )


async def start_run(headless: Headless, prepared: Prepared) -> None:
    """Start a node's run the way the runtime starts one.

    The caller waits for the state it is about; a run that is *not* yet in the
    state a case asserts is that case's to notice, and a helper that waited for
    one of them would be waiting for the wrong one in the other.
    """
    await headless.client.start_node_run(
        project_id=headless.project.project_id,
        node_id=prepared.node_id,
        actor_id=ACTOR,
        execution_contract_version=prepared.contract.version,
    )


# ── P10-04 ─────────────────────────────────────────────────────────────────────


async def test_p10_04_the_compute_worker_takes_a_live_turn(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-04: the Compute Worker seat is a real turn on a node that is running.

    The node is started the way the runtime starts one and left running, so that
    what the loop sees is a computation in somebody's hands. The Compute Worker
    is given exactly that node, once; its turn is a look rather than an act, so
    the node does not move and no record appears that the run did not write; and
    the Experimental Worker — the other seat that could have been dispatched —
    is not given it at all.
    """
    a_run_that_stays_live(headless, "COMPUTE_TIMEOUT")
    prepared = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    await start_run(headless, prepared)
    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.RUNNING)

    compute, experimental = RecordingWorker(), RecordingWorker()
    loop = await loop_for(headless, compute, experimental)
    await loop.step()

    assert compute.turns == [(prepared.node_id, NodeStatus.RUNNING.value)], (
        "the Compute Worker was not given the live computation"
    )
    assert experimental.turns == [], "the Experimental Worker was given a computation"
    assert headless.status_of(prepared.node) is NodeStatus.RUNNING, "a look moved the node"

    with headless.database.read_only() as session:
        deviations = DeviationRepository(session, headless.project.project_id).open()
    assert deviations == [], "a monitoring turn recorded a question nobody asked"

    # And it is asked once per episode, not once per round.
    await loop.step()
    assert len(compute.turns) == 1, "the Worker was asked again with nothing changed"


# ── P10-05 ─────────────────────────────────────────────────────────────────────


async def test_p10_05_the_experimental_worker_takes_a_waiting_turn(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-05: the Experimental Worker seat is a turn on a lab that is holding.

    `LAB_LONG_WAIT` is a lab that answers when something outside RAVEL says so.
    While it holds, the node is WAITING_EXTERNAL and the Experimental Worker is
    the role that may speak to it. After the turn the wait is exactly as it was:
    the node is still waiting, the job is still waiting, and no Execution Record
    exists, because a monitoring turn is not a result.
    """
    headless.lab("LAB_LONG_WAIT")
    prepared = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)
    await start_run(headless, prepared)
    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL)

    compute, experimental = RecordingWorker(), RecordingWorker()
    loop = await loop_for(headless, compute, experimental)
    await loop.step()

    assert experimental.turns == [(prepared.node_id, NodeStatus.WAITING_EXTERNAL.value)], (
        "the Experimental Worker was not given the waiting experiment"
    )
    assert compute.turns == [], "the Compute Worker was given a lab task"
    assert headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL

    with headless.database.read_only() as session:
        records = RecordRepositories(session, headless.project.project_id)
        executions = records.executions.for_node(prepared.node_id)
        waiting = records.jobs.for_node(prepared.node_id)

    assert executions == [], (
        "the Worker's turn produced an Execution Record, so it acted rather than looked"
    )
    assert waiting and all(not job.is_terminal for job in waiting), (
        "the Worker's turn ended the lab's job, which belongs to the run"
    )


# ── P10-09 ─────────────────────────────────────────────────────────────────────

#: The whole envelope this case's contract names. Every term is one a Worker
#: could want widened, and each is asked for below both inside and outside it.
ENVELOPE: dict[str, Any] = {
    "allowed_actions": ("run_measurement", "retry"),
    "allowed_ranges": {"pressure": "1..5"},
    "allowed_substitutions": ("Pd/C -> Pt/C",),
    "allowed_retries": 1,
}

#: One request of each of the four kinds Phase 10D names — a retry, a parameter
#: change, a substitution, an action — written so that the contract permits it.
#: A retry asked through this tool is an action like any other and is checked by
#: name; the permitted *number* of retries is the durable layer's, which W12
#: demonstrates.
INSIDE: tuple[tuple[str, dict[str, Any]], ...] = (
    ("a retry the contract names", {"requested_action": "retry"}),
    (
        "a pressure inside the window it declares",
        {"requested_action": "set pressure", "parameter": "pressure", "value": 5.0},
    ),
    (
        "the substitution it lists",
        {"requested_action": "swap catalyst", "substitute": ["Pd/C", "Pt/C"]},
    ),
    ("an action it names", {"requested_action": "run_measurement"}),
)

#: The same four, each one step outside the envelope: a different name, a value
#: past the end of the range, a pair that is not on the list, an action the
#: contract has never heard of.
OUTSIDE: tuple[tuple[str, dict[str, Any]], ...] = (
    (
        "a retry under a name the contract does not list",
        {"requested_action": "retry_now"},
    ),
    (
        "a pressure past the end of the window",
        {"requested_action": "set pressure", "parameter": "pressure", "value": 6.0},
    ),
    (
        "a substitution the contract does not list",
        {"requested_action": "swap catalyst", "substitute": ["Pd/C", "Ir/C"]},
    ),
    (
        "an action the contract does not name",
        {"requested_action": "skip the calibration"},
    ),
)


async def test_p10_09_the_contract_is_what_decides(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-09: every request a Worker can make is a lookup, and the model has no vote.

    Phase 10D names four dangerous requests — retry, parameter change,
    substitution, action — and requires each to pass `Execution Contract →
    Contract Validator → allowed / refused` rather than being carried out
    because a model said it was fine. So each of the four is asked twice: once
    inside the envelope this contract declares, and once outside it. Inside, the
    answer is yes, the Worker says so, and nothing moves. Outside, the answer is
    no, a deviation appears for Master, and the task stops.

    Two things make that enforcement rather than negotiation, and the case ends
    on both. The verdict is not an argument the caller can supply: `request_action`
    takes what was asked for and nothing else, so the terms it is checked against
    can only come from the contract the run was started under. And an
    out-of-envelope request that *argues its own case* in `description` is
    refused exactly as the bare one was, with the argument recorded after the
    finding rather than read before it. A system where the second were otherwise
    would be one where an agent's confidence set the contract's terms.
    """
    prepared = prepare(
        node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS, **ENVELOPE
    )
    context = worker_context(headless, AgentRole.COMPUTE_WORKER)
    ask = seat_tool("request_action", context)

    assert set(inspect.signature(ask).parameters) == {
        "node_id",
        "requested_action",
        "description",
        "parameter",
        "value",
        "substitute",
    }, (
        "the Worker's one question now takes something other than what was asked "
        "for; anything envelope-shaped among its arguments would be a Worker "
        "supplying the terms it is checked against"
    )

    for why, request in INSIDE:
        answer = await ask(node_id=prepared.node_id, **request)
        assert answer["permitted"] is True, f"{why} was refused: {answer['reason']}"
        assert answer["deviation_id"] is None, (
            "a permitted request raised a deviation, which is a question nobody asked"
        )
        assert answer["reason"], "a yes with no reason is a yes nobody can check"
        assert answer["node_status"] == NodeStatus.READY.value, (
            f"{why} moved the node; a permitted request changes nothing"
        )

    with headless.database.read_only() as session:
        records = RecordRepositories(session, headless.project.project_id)
        confirmations = records.messages.for_node(prepared.node_id)
        deviations = records.deviations.open()
        contract = ExecutionContractRepository(
            session, headless.project.project_id
        ).for_node(prepared.node_id)

    assert [message.kind for message in confirmations] == [
        WorkerMessageKind.CONFIRM
    ] * len(INSIDE), (
        f"the four permitted requests were not each confirmed: {confirmations}"
    )
    assert deviations == [], "a request the contract permits produced a deviation"
    assert (
        contract.allowed_actions == ENVELOPE["allowed_actions"]
        and contract.allowed_ranges == ENVELOPE["allowed_ranges"]
        and contract.allowed_substitutions == ENVELOPE["allowed_substitutions"]
        and contract.allowed_retries == ENVELOPE["allowed_retries"]
    ), "the contract was rewritten by the requests it permitted"

    # Keyed by the action that was asked for, so the assertions below read as
    # what the contract was silent about rather than as an index into a list.
    refused: dict[str, str] = {}
    for why, request in OUTSIDE:
        # A node apiece, because a refusal stops the task and a second refusal
        # on the same node would be a question asked of work that is no longer
        # going anywhere.
        theirs = prepare(
            node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS, **ENVELOPE
        )
        answer = await ask(node_id=theirs.node_id, **request)
        assert answer["permitted"] is False, (
            f"{why} was permitted: {answer['reason']}; the contract is a closed "
            "list and silence is refusal"
        )
        assert answer["deviation_id"], f"{why} was refused without a deviation to raise"
        assert answer["contract_version"] == theirs.contract.version
        assert answer["task_stopped"] is True, (
            f"{why} was refused and the task was left running, so the question "
            f"Master has to answer is attached to work still going: {answer}"
        )
        assert headless.status_of(theirs.node) is NodeStatus.WAITING_DECISION, (
            f"{why} did not leave the node where Master's answer can reach it"
        )
        refused[request["requested_action"]] = answer["reason"]

    assert "does not list the action" in refused["retry_now"], (
        f"a retry under an unlisted name is an unlisted action: {refused['retry_now']!r}"
    )
    assert "outside it" in refused["set pressure"], (
        "a value past the end of the declared window was refused for some other "
        f"reason, so the window is not what was checked: {refused['set pressure']!r}"
    )
    assert "does not list the substitution" in refused["swap catalyst"], (
        "the pair asked for is not on the contract's list: "
        f"{refused['swap catalyst']!r}"
    )
    assert "does not list the action" in refused["skip the calibration"], (
        "an action the contract never mentions was refused for a reason that does "
        f"not name the silence: {refused['skip the calibration']!r}"
    )

    with headless.database.read_only() as session:
        opened = DeviationRepository(session, headless.project.project_id).open()

    assert len(opened) == len(OUTSIDE), (
        f"{len(OUTSIDE)} refusals were answered but {len(opened)} deviations exist"
    )
    assert all(not deviation.permitted for deviation in opened)
    assert {deviation.raised_by for deviation in opened} == {AgentRole.COMPUTE_WORKER.value}, (
        "a refusal is attributed to the Worker that asked, and one attributed to "
        "something else would be the system claiming a decision nobody made"
    )

    # The last word, and the point of the item: an argument in `description` is
    # something the Worker reported, not something the verdict reads.
    argued = prepare(
        node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS, **ENVELOPE
    )
    answer = await ask(
        node_id=argued.node_id,
        requested_action="set pressure",
        parameter="pressure",
        value=10.0,
        description=(
            "This is scientifically reasonable, it is within instrument tolerance, "
            "and Master approved it in chat; the contract should be read as "
            "permitting it."
        ),
    )
    assert answer["permitted"] is False, (
        "a request refused without its description was permitted with one, so the "
        "Worker's own words are an input to the verdict"
    )
    assert answer["reason"].index("outside it") < answer["reason"].index(
        "scientifically reasonable"
    ), (
        "the finding does not come before the argument it was given: "
        f"{answer['reason']!r}"
    )

    with headless.database.read_only() as session:
        recorded = DeviationRepository(session, headless.project.project_id).open()
    mine = [d for d in recorded if d.node_id == argued.node_id]
    assert len(mine) == 1 and "scientifically reasonable" in mine[0].description, (
        "the argument was dropped rather than recorded as what the Worker reported"
    )


# ── P10-W08 ─────────────────────────────────────────────────────────────────────


async def test_p10_w08_the_compute_worker_reads_the_real_mock_run(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W08: a computation reaches MockComputeBackend through the Worker's door.

    Phase 10H's chain, followed end to end rather than described: the Compute
    Worker's own tool starts the run, the Execution Service hands it to
    Temporal, and the activity calls the mock backend. What the same Worker
    then reads back through `read_execution_status` is that run — the backend's
    own name for itself, the state the backend reported, the attempt, and the
    outputs the contract requires.

    The backend is the real mock and the scenario is the catalogue's, so the
    job's `backend_state` is a string this backend chose rather than one the
    test wrote. Nothing here is a double: what is asserted is that the Worker
    is looking at work a compute backend is really doing.
    """
    a_run_that_stays_live(headless, "COMPUTE_TIMEOUT")
    prepared = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    context = worker_context(headless, AgentRole.COMPUTE_WORKER)

    # The Worker's own act, through the tool a session holds.
    answer = await seat_tool("start_execution", context)(node_id=prepared.node_id)
    assert answer["started"] is True, f"the run did not start: {answer}"
    assert answer["refused"] is False
    assert answer["contract_version"] == prepared.contract.version, (
        "the run was started under a contract that is not the frozen one"
    )

    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.RUNNING)

    seen = await seat_tool("read_execution_status", context)(node_id=prepared.node_id)
    job = seen["job"]
    assert job is not None, "the Worker was handed a running node with no job"
    assert job["backend"] == "mock-compute", (
        f"the Compute Worker's task is held by {job['backend']!r}, not the compute mock"
    )
    assert job["node_id"] == prepared.node_id
    assert job["attempt"] == 1
    assert seen["node_status"] == NodeStatus.RUNNING.value
    assert seen["required_outputs"] == list(COMPUTE_OUTPUTS)
    assert seen["execution"] is None, (
        "the run has not ended, so there is no Execution Record to read"
    )

    # And the run is Temporal's: the Worker started it and is not holding it.
    with headless.database.read_only() as session:
        jobs = RecordRepositories(session, headless.project.project_id).jobs.for_node(
            prepared.node_id
        )
    assert jobs and all(not job.is_terminal for job in jobs)

    # Starting again is the same run rather than a second one.
    again = await seat_tool("start_execution", context)(node_id=prepared.node_id)
    assert again["refused"] is True, (
        "a node already RUNNING was started a second time, so the DAG's own "
        "guard is not the door the Worker goes through"
    )


# ── P10-W09 ─────────────────────────────────────────────────────────────────────


async def test_p10_w09_the_experimental_worker_starts_the_real_mock_lab(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W09: an experiment reaches MockLabBackend through the Worker's door.

    The same chain as W08 for the other seat, and the lab is the harder case
    because it does not finish on its own: the run holds, the node becomes
    WAITING_EXTERNAL, and the Worker reads a job whose backend is the lab mock
    and whose state is the lab's own word for holding. Nothing about the wait
    is the Worker's to end — it reads it, and the event that ends it arrives
    from outside RAVEL.
    """
    headless.lab("LAB_LONG_WAIT")
    prepared = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)
    context = worker_context(headless, AgentRole.EXPERIMENTAL_WORKER)

    answer = await seat_tool("start_execution", context)(node_id=prepared.node_id)
    assert answer["started"] is True, f"the experiment did not start: {answer}"

    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL)

    seen = await seat_tool("read_execution_status", context)(node_id=prepared.node_id)
    job = seen["job"]
    assert job is not None, "a waiting experiment with no job at the lab"
    assert job["backend"] == "mock-lab", (
        f"the Experimental Worker's task is held by {job['backend']!r}, not the lab mock"
    )
    assert job["state"] == JobState.WAITING_EXTERNAL.value, (
        "the lab is not reporting a wait, so the Worker is reading something else"
    )
    assert seen["node_status"] == NodeStatus.WAITING_EXTERNAL.value
    assert seen["required_outputs"] == list(LAB_OUTPUTS)
    assert seen["execution"] is None, "a lab that has not answered has delivered nothing"


# ── P10-W05 ─────────────────────────────────────────────────────────────────────


def test_p10_w05_a_worker_reaches_only_its_own_contract() -> None:
    """P10-W05: the two Worker seats hold five tools between them, and no more.

    The role model as a checkable fact rather than a paragraph. Neither Worker
    can reach the DAG, the plan, a verdict, or the Evidence Ledger. What it can
    do is begin the one task it was convened for, read the contract it acts
    under, read what has happened, ask RAVEL whether something is permitted,
    and — if it is the role that talks to a lab — say one of the four permitted
    things.

    `start_execution` is the one that has to be read carefully, because
    "starts a run" sounds like the authority the rest of this list denies. It
    is not: it carries no node, no method and no parameter — only the id of a
    task that already exists — and the DAG decides whether it may run. What the
    Worker holds is the door, not the judgement.
    """
    compute = set(tools_for(AgentRole.COMPUTE_WORKER))
    experimental = set(tools_for(AgentRole.EXPERIMENTAL_WORKER))

    assert compute == {
        "whoami",
        "start_execution",
        "read_execution_contract",
        "read_execution_status",
        "request_action",
    }
    assert experimental == compute | {"send_message"}, (
        "the Experimental Worker is the role that talks to a lab, and it is the "
        "only one that may say one of the four permitted things"
    )

    for role in (AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER):
        held = set(tools_for(role))
        assert not held & DAG_MUTATION_TOOLS, f"{role.value} can change the plan"
        assert "read_project_state" not in held, f"{role.value} can read the whole plan"
        assert "submit_review" not in held, f"{role.value} can judge a result"
        assert "record_evidence" not in held, f"{role.value} can write evidence"
        assert not held & {"read_research_task", "search_web", "open_source"}, (
            f"{role.value} can reach research"
        )


# ── P10-W06 / P10-W07 ───────────────────────────────────────────────────────────


async def test_p10_w06_a_worker_cannot_change_the_plan(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W06: the plan is Master's, and the refusal is a guard rather than a roster.

    W05 reads the roster; this reads the door. A Worker session is handed the
    real `add_dag_node` handler — the one a mis-edited registry would leave
    reachable — and calls it. What must happen is a refusal, and the DAG must be
    exactly as it was: an authorization check that ran after the write would
    leave a node behind that no plan accounts for.
    """
    for role in (
        AgentRole.COMPUTE_WORKER,
        AgentRole.EXPERIMENTAL_WORKER,
        AgentRole.REVIEW,
        AgentRole.RESEARCH,
    ):
        assert mutating_tools_for(role) == (), f"{role.value} holds a DAG mutation"
    assert set(mutating_tools_for(AgentRole.MASTER)) == DAG_MUTATION_TOOLS, (
        "the mutation tools are Master's, and Master holds all of them"
    )

    prepared = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    context = worker_context(headless, AgentRole.COMPUTE_WORKER)

    with headless.database.read_only() as session:
        before = DagRepository(session, headless.project.project_id).nodes()

    with pytest.raises(PermissionError) as refused:
        await IMPLEMENTATIONS["add_dag_node"](context)(
            node_type=NodeType.COMPUTATION.value,
            objective="A node the Worker would like to exist.",
            rationale="The Worker would rather this node existed than the one it has.",
        )

    assert AgentRole.COMPUTE_WORKER.value in str(refused.value), (
        f"the refusal does not say which session was refused: {refused.value}"
    )
    assert headless.project.project_id in str(refused.value), (
        "the refusal names a role but not the project it was refused in, so a "
        f"multi-project deployment cannot tell the two sessions apart: {refused.value}"
    )
    with headless.database.read_only() as session:
        after = DagRepository(session, headless.project.project_id).nodes()

    assert [node.node_id for node in after] == [node.node_id for node in before], (
        "the refused call changed the plan anyway, so the node was written before "
        "the authority was checked"
    )
    assert prepared.node_id in {node.node_id for node in after}


async def test_p10_w07_a_worker_cannot_touch_what_it_is_judged_against(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W07: a Worker cannot move the criteria it will be measured by.

    The subtler half of W06. Changing the plan is visibly a change to the
    project; changing what "passed" means is a change to the *measurement*, and
    a Worker that could make it would be one that could pass itself. So the
    Worker does everything it can — begins the task, asks its contract for a
    change, is refused, and stops the task — and the frozen criteria are then
    read back and compared against what they were, criterion by criterion,
    because a version number that did not move is not on its own evidence that
    the statements did not.
    """
    headless.compute("COMPUTE_SUCCESS")
    prepared = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    context = worker_context(headless, AgentRole.COMPUTE_WORKER)

    def criteria_of(node_id: str) -> tuple[Any, Any]:
        """The frozen contract and the node's binding to it, read from the store."""
        with headless.database.read_only() as session:
            return (
                AcceptanceContractRepository(
                    session, headless.project.project_id
                ).frozen_for_node(node_id),
                DagRepository(session, headless.project.project_id).node(node_id),
            )

    before, node_before = criteria_of(prepared.node_id)
    assert before is not None, "the node was prepared without frozen criteria"
    assert node_before.acceptance_contract_ref == before.contract_id, (
        "the fixture bound the node to a contract other than the one it froze"
    )

    started = await seat_tool("start_execution", context)(node_id=prepared.node_id)
    assert started["started"] is True, f"the Worker could not begin its own task: {started}"
    refused = await seat_tool("request_action", context)(
        node_id=prepared.node_id,
        requested_action="reword the acceptance criteria",
        description="The criterion as written is ambiguous; say 'roughly 15%'.",
    )
    assert refused["permitted"] is False, (
        "a Worker asked to change what it is judged against and RAVEL permitted it"
    )

    after, node_after = criteria_of(prepared.node_id)
    assert after is not None, "the node's criteria disappeared while it ran"
    assert after.contract_id == before.contract_id, (
        "a Worker's request produced a second Acceptance Contract for the node it "
        "was working under"
    )
    assert after.version == before.version, "a Worker revised the Acceptance Contract"
    assert after.criteria == before.criteria, (
        "the criteria are not the ones the node was frozen against: "
        f"{[criterion.statement for criterion in after.criteria]}"
    )
    assert after.is_frozen, "a Worker unfroze the criteria it was being judged against"
    assert node_after.acceptance_contract_ref == node_before.acceptance_contract_ref, (
        "the node now points at different criteria than the ones it was measured by"
    )


# ── P10-W18 ─────────────────────────────────────────────────────────────────────


async def test_p10_w18_a_dormant_worker_session_is_reaped_and_rebuilt(
    database: Database,
    project: Project,
    execution_settings: Settings,
    prepare: Callable[..., Prepared],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P10-W18: a dead Worker session is rebuilt from the record, not mourned.

    A Worker's session is a process that can die, and Phase 10F says the right
    answer is not to keep one alive for days but to treat it as recoverable. The
    real pool, the real runtime, and a real Worker agent are here with only the
    harness process stood in for.

    What is asserted is the whole of what makes that affordable. A scope nobody
    has asked anything of is closed — process, runtime and session binding
    together. The agent notices it holds nothing. Its next turn puts a *new*
    process and a new session in the seat, under the same `(project, role)`
    identity. And the state that turn acts on is the record's, which never went
    anywhere: what a reap costs is the session's short-term memory, and that was
    never the authoritative state.
    """
    FakeHarness.instances.clear()
    monkeypatch.setattr(runtime_module, "DeepSeekHarness", FakeHarness)

    prepared = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    situation = read_situation(database, project.project_id)

    # A runtime root of this test's own, so the working directory a runtime
    # writes its overlay into lands in a temporary directory rather than in the
    # checkout.
    settings = execution_settings.model_copy(update={"runtime_dir": tmp_path / "runtime"})
    pool = create_pool(settings)
    try:
        worker = WorkerAgent(
            pool=pool, project_id=project.project_id, role=AgentRole.COMPUTE_WORKER
        )
        await worker.act(prepared.node, situation)

        first_session = worker.session_id
        assert first_session is not None, "the turn ran without a session"
        assert pool.binding_for(first_session) is not None, (
            "the session is not bound to the work it serves"
        )
        assert len(FakeHarness.instances) == 1
        assert FakeHarness.instances[0].sessions == [first_session]

        # Nobody asks this scope anything for long enough that it goes dormant.
        runtime = pool.live_runtime(project.project_id, AgentRole.COMPUTE_WORKER)
        assert runtime is not None
        runtime.last_used_at = datetime.now(UTC) - timedelta(hours=1)
        assert pool.reap_idle() == 1, "the idle scope was not reaped"
        assert FakeHarness.instances[0].closed, "the process was forgotten, not stopped"

        assert not worker.live, "the agent still believes it holds a runtime"
        assert pool.live_runtime(project.project_id, AgentRole.COMPUTE_WORKER) is None
        assert pool.binding_for(first_session) is None, (
            "the session binding outlived the runtime that could serve it"
        )

        # The next turn is the same agent, on a new process and a new session.
        # The situation is read again rather than reused, because that is what
        # the replacement gets: nothing reaches it through the dead process, so
        # everything it acts on comes out of PostgreSQL.
        recovered = read_situation(database, project.project_id)
        assert recovered.project.project_id == project.project_id
        assert prepared.node_id in {node.node_id for node in recovered.nodes}
        with database.read_only() as session:
            contract = ExecutionContractRepository(
                session, project.project_id
            ).for_node(prepared.node_id)
        assert contract.version == prepared.contract.version, (
            "the contract the recovered session acts under is not the frozen one"
        )

        await worker.act(prepared.node, recovered)
        second_session = worker.session_id
        assert second_session is not None and second_session != first_session, (
            "the recovered turn reused an identifier only the dead process knew"
        )
        assert len(FakeHarness.instances) == 2, "no new process was started"
        assert pool.binding_for(second_session) is not None
        assert pool.live_runtime(project.project_id, AgentRole.COMPUTE_WORKER) is not None
    finally:
        pool.close()


# ── P10-W13 ─────────────────────────────────────────────────────────────────────


async def test_p10_w13_an_out_of_contract_request_is_refused(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W13: asking is not acting, and the backend stays the only thing that acts.

    The Worker asks its contract for something the contract does not permit,
    through the tool a real session holds. Four things follow and all four are
    the point: a deviation is recorded for Master, the escalation says the
    Worker did not answer the question itself, the contract is not amended, and
    the run — which is in flight and belongs to Temporal — is left exactly where
    it was. A Worker that had answered the question itself, or stopped the job,
    would show up as a different contract version, a moved node, or a cancelled
    job.
    """
    a_run_that_stays_live(headless, "COMPUTE_TIMEOUT")
    prepared = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    await start_run(headless, prepared)
    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.RUNNING)

    context = seat_scope(
        headless.database,
        headless.project.project_id,
        AgentRole.COMPUTE_WORKER,
        settings=headless.settings,
    )
    answer = await seat_tool("request_action", context)(
        node_id=prepared.node_id,
        requested_action="raise_temperature",
        description="The backend says it needs 900 K.",
    )

    assert answer["permitted"] is False, "the contract permits temperatures?"
    assert answer["deviation_id"], "a refusal that records no deviation is a dead end"
    assert answer["task_stopped"] is False, (
        "the Worker stopped a run that is in flight, and where a run ends is the "
        "run's own ending to report"
    )

    with headless.database.read_only() as session:
        records = RecordRepositories(session, headless.project.project_id)
        deviations = DeviationRepository(session, headless.project.project_id).open()
        messages = records.messages.for_node(prepared.node_id)
        jobs = records.jobs.for_node(prepared.node_id)
        contract = ExecutionContractRepository(session, headless.project.project_id).for_node(
            prepared.node_id
        )

    assert [deviation.requested_action for deviation in deviations] == ["raise_temperature"]
    assert deviations[0].permitted is False
    assert deviations[0].raised_by == AgentRole.COMPUTE_WORKER.value
    assert any(message.kind is WorkerMessageKind.ESCALATE for message in messages), (
        "the refusal was recorded without the escalation that says the Worker did "
        "not answer the question itself"
    )
    assert contract.version == prepared.contract.version, "the Worker widened its own contract"
    assert headless.status_of(prepared.node) is NodeStatus.RUNNING, (
        "the Worker moved a node whose ending belongs to the run"
    )
    assert any(not job.is_terminal for job in jobs), "the in-flight job was cancelled"


# ── P10-W04 ─────────────────────────────────────────────────────────────────────


def test_p10_w04_a_worker_is_told_about_one_task(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W04: a Worker's context is its node, not the project's plan.

    The turn's input is the whole of what a Worker is told, so it is where
    "minimal context" is either true or not. Two nodes are in the project and
    one of them is being served: the input names that one, its contract and its
    status, and the other node appears nowhere.

    The count is over *identifiers* rather than over objectives, and it is
    written as a count rather than as a pair of `not in` checks so that it says
    what the item says: of everything the project holds, exactly one task is in
    the Worker's turn. The fixture builds both nodes from one objective, so an
    assertion about a second node's text would be an assertion about the fixture
    — the identifier is what makes a node that node, and it is what a Worker
    that had been handed the plan would be able to see.

    Master's turn is the contrast, and the quantity that differs is the
    project's: Master is told what the project is for, and a Worker serving one
    task is not. What a Worker cannot do is discover the rest — it holds no tool
    that lists the plan (P10-W05), and this is what that costs it.
    """
    mine = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    theirs = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    situation = read_situation(headless.database, headless.project.project_id)
    assert {node.node_id for node in situation.nodes} >= {mine.node_id, theirs.node_id}

    worker = WorkerAgent(
        pool=None,  # type: ignore[arg-type]
        project_id=headless.project.project_id,
        role=AgentRole.COMPUTE_WORKER,
    )
    prompt = worker._prompt(mine.node, situation)

    assert f"Node ID: {mine.node_id}" in prompt, "the Worker was not told its own task"
    assert mine.contract.contract_id in prompt or mine.node.objective in prompt
    assert theirs.node_id not in prompt, (
        "the Worker's turn named another task, which is context it has no work to do"
    )
    assert situation.project.objective not in prompt, (
        "the Worker was handed the project's objective, which is Master's to hold"
    )

    named = [node.node_id for node in situation.nodes if node.node_id in prompt]
    assert named == [mine.node_id], (
        f"the Worker's turn named {len(named)} of the project's tasks; a turn is "
        "about one, and the rest is the plan a Worker does not hold"
    )

    master = HarnessAgent(
        pool=None,  # type: ignore[arg-type]
        project_id=headless.project.project_id,
        role=AgentRole.MASTER,
    )
    master_prompt = master._master_prompt(situation)
    assert situation.project.objective in master_prompt, (
        "Master is not told what the project is for, which is the one thing it "
        "decides in terms of"
    )


# ── P10-03 ──────────────────────────────────────────────────────────────────


def test_p10_03_the_five_roles_are_five_different_authorities() -> None:
    """P10-03: five roles exist, and no two of them hold the same authority.

    "Five agents" is a claim about the harness, and the way it fails quietly is
    by having five *names* over one authority: a role whose prompt differs and
    whose tools do not is a role the model can be talked out of. So the roster
    is what is checked — each role ships its own behavioral contract, and the
    five tool rosters are pairwise different, with exactly one of them holding
    the DAG.

    `whoami` is in all five on purpose. Every role can be asked what it is, and
    the answer comes from the scope RAVEL bound to its process rather than from
    anything the model supplied.
    """
    roles = list(AgentRole)
    assert len(roles) == 5, f"RAVEL has {len(roles)} roles; the product fact is five"

    rosters: dict[AgentRole, frozenset[str]] = {}
    for role in roles:
        definition = definition_for(role)
        assert definition.prompt_file.is_file(), f"{role.value} ships no behavioral contract"
        assert definition.persona.strip(), f"{role.value}'s contract is empty"
        assert definition.prompt_file.parent.name == "prompts", (
            f"{role.value}'s contract is not one of the shipped prompts"
        )
        roster = frozenset(tools_for(role))
        assert roster, f"{role.value} holds no tools, so it cannot act at all"
        assert "whoami" in roster, f"{role.value} cannot be asked what it is"
        rosters[role] = roster

    assert len(set(rosters.values())) == 5, (
        "two roles hold the same tools; a role is its authority, not its name"
    )
    holders = [role for role, roster in rosters.items() if roster & DAG_MUTATION_TOOLS]
    assert holders == [AgentRole.MASTER], (
        f"{[role.value for role in holders]} can change the Scientific DAG"
    )


# ── P10-03, the two things a new project is missing ─────────────────────────


async def test_p10_03_a_master_writes_the_question_before_the_plan(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-03: what the project is for is written by Master, not by a fixture.

    This is the third of the same finding, and the sharpest. The roadmap gap
    below was a tool that did not exist; this one was a record that nothing in
    the product ever wrote. `read_research_task` reads the project's Research
    Contract as the terms the seat's work is answered under, and the only
    writers of that row in the whole tree were two test fixtures — so on a real
    project the Research seat's first read failed, twice, in a live run, and a
    five-agent deployment could not research anything.

    So the contract is asserted as Master's act, through the tool a session
    reaches: a project with none refuses the read and says why, the write makes
    the same read answer, and the write is refused to every other role. It is
    deliberately *not* a DAG mutation — no node is created, no dependency moves
    — which is asserted by the DAG being the same one afterwards, and by the
    tool being absent from the set the roster test above watches.
    """
    project_id = headless.project.project_id
    master = seat_scope(headless.database, project_id, AgentRole.MASTER)
    research = seat_scope(headless.database, project_id, AgentRole.RESEARCH)
    task = prepare(node_type=NodeType.RESEARCH, with_acceptance=False)

    assert "commit_research_contract" in tools_for(AgentRole.MASTER)
    assert "commit_research_contract" not in mutating_tools_for(AgentRole.MASTER), (
        "writing down what the project was asked for changes no node, so it is "
        "not a DAG mutation and must not be counted as one"
    )
    holders = [role for role in AgentRole if "commit_research_contract" in tools_for(role)]
    assert holders == [AgentRole.MASTER], (
        f"{[role.value for role in holders]} may state what a project is for"
    )

    def project_state() -> dict[str, Any]:
        with headless.database.read_only() as session:
            contracts = ResearchContractRepository(session, project_id).all()
            nodes = DagRepository(session, project_id).nodes()
            decisions = DecisionRepository(session, project_id).all()
        return {"contracts": contracts, "nodes": nodes, "decisions": decisions}

    def dag_shape() -> tuple[list[str], int]:
        state = project_state()
        return sorted(node.node_id for node in state["nodes"]), len(state["decisions"])

    before = dag_shape()
    assert project_state()["contracts"] == [], (
        "a created project starts with no research contract; this case is about "
        "the act that writes the first one"
    )
    assert before[0] == [task.node_id], (
        "the fixture's research task is the only node, so the case starts from a "
        "project that has a task and no statement of what it is for"
    )

    # The seat's own read is the thing that was broken, so it is the thing
    # asserted: a task whose project has said nothing about what it wants is a
    # task nobody can serve.
    with pytest.raises(NotFound) as nothing_stated:
        await seat_tool("read_research_task", research)(node_id=task.node_id)
    assert project_id in str(nothing_stated.value), (
        f"the refusal does not name the project: {nothing_stated.value}"
    )

    # And it is not a thing a Research session may fix for itself: the seat
    # that could state its own question is the seat that sets the terms it is
    # judged against.
    with pytest.raises(PermissionError) as refused:
        await seat_tool("commit_research_contract", research)(
            original_user_goal="Something the user never asked for.",
            scientific_problem="A question this session chose for itself.",
        )
    assert AgentRole.RESEARCH.value in str(refused.value)
    assert project_id in str(refused.value), (
        f"the refusal names a role but not the project it was refused in: {refused.value}"
    )
    assert project_state()["contracts"] == [], "the refused commit wrote something anyway"

    goal = "Find a dopant that survives 500 hours under load."
    problem = "Which dopant keeps conductivity above the threshold?"
    written = await seat_tool("commit_research_contract", master)(
        original_user_goal=goal,
        scientific_problem=problem,
        hypotheses=["Niobium doping raises stability.", ""],
        target_metrics=["conductivity gain >= 15%"],
        acceptance_strategy="Measure the series against the baseline.",
        known_constraints=["Bench time is limited."],
        prohibited_actions=["No testing on live reactors."],
    )
    contract = written["contract"]
    assert contract["original_user_goal"] == goal
    assert contract["scientific_problem"] == problem
    assert contract["research_hypotheses"] == ["Niobium doping raises stability."], (
        "a blank hypothesis was kept, so the contract carries a statement that "
        f"says nothing: {contract['research_hypotheses']}"
    )
    assert contract["project_id"] == project_id

    # The read that failed now answers, and it answers with the contract and
    # with the task's own frozen terms — which is what the seat needs in order
    # to do the work at all.
    read = await seat_tool("read_research_task", research)(node_id=task.node_id)
    assert read["project"]["scientific_problem"] == problem
    assert read["project"]["original_user_goal"] == goal
    assert read["project"]["target_metrics"] == ["conductivity gain >= 15%"]
    assert read["terms"]["contract_id"] == task.contract.contract_id, (
        "the terms are the task's frozen Execution Contract, and this is another one"
    )
    assert read["ledger"] == {"sources": [], "claims": [], "conflicts": [], "records": []}, (
        f"a task that has read nothing has an empty ledger: {read['ledger']}"
    )

    # What the project is for is written once. A change to it is a new project,
    # and the refusal is what a session reads instead of writing a second one.
    with pytest.raises(RuntimeError) as already:
        await seat_tool("commit_research_contract", master)(
            original_user_goal=goal,
            scientific_problem="A different question, asked by the same project.",
        )
    assert project_id in str(already.value)

    assert dag_shape() == before, (
        "committing the research contract changed the DAG; it records what the "
        "project is for, and the plan is the next act rather than this one"
    )
    assert len(project_state()["contracts"]) == 1

    # A session that starts by reading the project is told the contract is
    # there, which is how it knows not to write a second one.
    assert (await seat_tool("read_project_state", master)())["research_contract"] == {
        "committed": True,
        "contract_id": contract["contract_id"],
        "scientific_problem": problem,
    }


async def test_p10_03_a_master_defines_success_and_can_end_the_project(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-03: the third thing a project is missing, and the act that ends it.

    The same finding as the case above, one act further along, and the one a
    live run paid for. `MasterService.conclude` is the only way any project has
    ever ended, and it was reachable from no tool a session could hold; nothing
    in production wrote the success contract that an ending is measured against,
    and nothing wrote `CONTRACT_DEFINED` — so a project could not leave CREATED
    and could not end. In the live run, Master was asked by the loop for an
    ending, had no way to record one, and added a DECISION node instead.

    Both acts are asserted here as Master's, through the tools a session
    reaches, and the refusals are asserted with them: a Master told only "no"
    learns nothing about what it still owes the project, and each refusal here
    names the thing that is outstanding.
    """
    project_id = headless.project.project_id
    master = seat_scope(headless.database, project_id, AgentRole.MASTER)
    research = seat_scope(headless.database, project_id, AgentRole.RESEARCH)
    task = prepare(node_type=NodeType.COMPUTATION, cleared=False)

    for name in ("commit_success_contract", "conclude_project"):
        assert name in tools_for(AgentRole.MASTER)
        assert name not in mutating_tools_for(AgentRole.MASTER), (
            f"{name} changes no node, so it is not a DAG mutation and must not be "
            "counted as one"
        )
        holders = [role for role in AgentRole if name in tools_for(role)]
        assert holders == [AgentRole.MASTER], (
            f"{[role.value for role in holders]} may call {name}"
        )

    # A project that has not said what answering it would look like, and the
    # read says so before anything refuses: a session that learned this from a
    # refusal at the end of the project would learn it too late.
    reading = await seat_tool("read_project_state", master)()
    assert reading["status"] == ProjectStatus.CREATED.value
    assert reading["success_contract"]["committed"] is False
    assert project_status(headless.database, project_id) is ProjectStatus.CREATED

    # And no ending is available from there — a project cannot become COMPLETED
    # without having executed anything.
    with pytest.raises(ValueError) as too_early:
        await seat_tool("conclude_project", master)(
            outcome="SUCCESS", rationale="The series answered the question."
        )
    assert ProjectStatus.CREATED.value in str(too_early.value), (
        f"the refusal does not say where the project is: {too_early.value}"
    )

    criteria = ["Conductivity rises by at least 15%.", ""]
    written = await seat_tool("commit_success_contract", master)(
        success_criteria=criteria,
        failure_criteria=["No sample exceeds the control beyond noise."],
        unresolved_uncertainty_policy="Conclude inconclusive rather than guess.",
    )
    contract = written["contract"]
    assert contract["version"] == 1
    assert written["revision"] is False
    assert contract["success_criteria"] == ["Conductivity rises by at least 15%."], (
        "a blank criterion was kept, so the definition the ending is measured "
        f"against carries a statement that says nothing: {contract['success_criteria']}"
    )
    # The move is the act's, not the caller's: the repository performs it in the
    # same transaction as the write, so a project cannot hold a frozen
    # definition of success while its own status says it has none.
    assert project_status(headless.database, project_id) is (
        ProjectStatus.CONTRACT_DEFINED
    )
    assert (await seat_tool("read_project_state", master)())["success_contract"] == {
        "committed": True,
        "contract_id": contract["contract_id"],
        "version": 1,
        "success_criteria": ["Conductivity rises by at least 15%."],
    }

    # Changing what the project is aiming at is a different act from stating it,
    # and it arrives with the reasoning that made Master change it.
    with pytest.raises(ValueError) as unexplained:
        await seat_tool("commit_success_contract", master)(
            success_criteria=["Something the project never set out to do."]
        )
    assert "reasoning" in str(unexplained.value), (
        "a revision was refused without saying that it is the reasoning which is "
        f"missing: {unexplained.value}"
    )

    # Nor may the seat that is judged against the definition write it.
    with pytest.raises(PermissionError) as refused:
        await seat_tool("commit_success_contract", research)(
            success_criteria=["A definition this seat chose for itself."]
        )
    assert AgentRole.RESEARCH.value in str(refused.value)
    assert project_status(headless.database, project_id) is (
        ProjectStatus.CONTRACT_DEFINED
    ), "the refused write moved the project"

    # Terminating is the one ending that states something about the work rather
    # than about the answer, so it is available while the work is unfinished —
    # and it is what a Master that has run out of road records.
    with pytest.raises(ValueError) as no_reason:
        await seat_tool("conclude_project", master)(
            outcome="TERMINATED", rationale="The plan cannot run."
        )
    assert "reason" in str(no_reason.value), (
        f"a termination without a reason was allowed or refused for the wrong "
        f"reason: {no_reason.value}"
    )

    ended = await seat_tool("conclude_project", master)(
        outcome="TERMINATED",
        rationale="No node in this plan can be started, so no result is coming.",
        reason="The only node has no way to begin.",
    )
    assert ended["outcome"] == "TERMINATED"
    assert ended["status"] == ProjectStatus.CANCELLED.value
    assert ended["cancelled_nodes"] == [task.node_id], (
        "termination is the ending that stops what is still running, and this "
        "one stopped nothing"
    )
    assert project_status(headless.database, project_id) is ProjectStatus.CANCELLED

    # The ending is a record rather than only a status: a reader finds why the
    # project stopped by querying the decisions, which is what the type is for.
    with headless.database.read_only() as session:
        decision = DecisionRepository(session, project_id).get(
            decision_id=ended["decision_id"]
        )
        node = DagRepository(session, project_id).node(task.node_id)
    assert decision.decision_type is DecisionType.TERMINATE_PROJECT
    assert decision.affected_nodes.cancelled == (task.node_id,)
    assert node.status is NodeStatus.CANCELLED

    # A project has one ending. A second would contradict the record that is
    # already there, and the refusal says so.
    with pytest.raises(ValueError) as already_ended:
        await seat_tool("conclude_project", master)(
            outcome="INCONCLUSIVE",
            rationale="The same project, ended a second time.",
        )
    assert "already ended" in str(already_ended.value)


async def test_p10_03_a_master_can_read_why_a_node_stopped(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-03: the seat that has to answer for a stop can read what stopped it.

    The fourth thing the live five-agent run paid for, and the subtler half of
    the third. Review withheld a pre-flight clearance — a verdict that parks the
    node at WAITING_DECISION, which nothing but Master can move — and the only
    thing Master was told was that a node had stopped. The verdict itself was
    unreadable: `read_review_package` is Review's window, and no tool Master
    holds returned a review. So the decision-maker replanned, the same node came
    back the same way, and the project ended with the computation never having
    run at all.

    What is asserted is not that the refusal parks the node — that has its own
    case in the review gate — but that the reason travels to the one role that
    may act on it, and that it travels as a reading: Master reads Review's
    record and still cannot write one.
    """
    project_id = headless.project.project_id
    master = seat_scope(headless.database, project_id, AgentRole.MASTER)
    review = seat_scope(headless.database, project_id, AgentRole.REVIEW)
    task = prepare(node_type=NodeType.COMPUTATION, cleared=False)

    diagnosis = "The criterion cannot be measured from what this contract collects."
    recommendation = "State the criterion against a metric the run records."
    refused = await seat_tool("submit_review", review)(
        node_id=task.node_id,
        checkpoint=ReviewCheckpoint.PRE_RUN.value,
        outcome=ReviewOutcome.FAIL.value,
        diagnosis=diagnosis,
        recommendations=[recommendation],
    )
    assert refused["moved_to"] == NodeStatus.WAITING_DECISION.value, (
        "a pre-flight refusal that did not park the node would not be a state "
        f"Master has to answer for: {refused}"
    )

    stopped = (await seat_tool("read_project_state", master)())["stopped"]
    assert [entry["node"] for entry in stopped] == [task.node.display_id], (
        f"the stopped node is not reported as stopped: {stopped}"
    )
    entry = stopped[0]
    assert entry["status"] == NodeStatus.WAITING_DECISION.value
    assert entry["node_type"] == NodeType.COMPUTATION.value
    assert entry["verdict"] == {
        "checkpoint": ReviewCheckpoint.PRE_RUN.value,
        "outcome": ReviewOutcome.FAIL.value,
        "diagnosis": diagnosis,
        "recommendations": [recommendation],
    }, f"the reason Master is given is not the verdict that stopped it: {entry}"

    # The turn Master is handed says where the reason is. A read that exists and
    # is never pointed at is a read the seat does not have.
    situation = read_situation(headless.database, project_id)
    prompt = HarnessAgent(
        pool=None,  # type: ignore[arg-type]
        project_id=project_id,
        role=AgentRole.MASTER,
    )._master_prompt(situation)
    assert task.node.display_id in prompt, (
        "Master's turn does not name the node waiting on it"
    )
    assert "stopped" in prompt, (
        "Master is told a node stopped without being told where the reason is, "
        f"which is how it replans blind: {prompt}"
    )

    # Reading it is Master's; writing it is not. The verdict stays Review's
    # record, which is what makes it a verdict rather than a second opinion.
    assert "read_review_package" not in tools_for(AgentRole.MASTER)
    assert "submit_review" not in tools_for(AgentRole.MASTER)
    with headless.database.read_only() as session:
        written = ReviewRepository(session, project_id).for_node(task.node_id)
    assert len(written) == 1 and written[0].diagnosis == diagnosis


async def test_l27_a_node_nobody_can_run_is_named_to_master(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """L-27: the half of the gap that was left after the type was abolished.

    A plan can still hold a node no seat is ever given — HYPOTHESIS and DECISION
    are plannable, both are Master's, and neither is handed to a Worker or to a
    Research session — so the loop puts such a node to Master
    (`Situation.needs_decision` includes the `unexecutable` bucket) and stops.
    What it did not do, until this case existed, was say so: the prompt's "what
    is waiting on you" listed only the stopped, the blocked and the escalations,
    so a round could ask Master a question with nothing under it — which reads
    as a mistake in the prompt and was taken for one twice in live runs, once
    for forty-three minutes and thirty cancelled nodes.

    Asserted on both windows Master has, and asserted to be the *same sentence*:
    a read that exists and a prompt that paraphrases it are two statements that
    can come to disagree, and the one that reaches the model is the prompt.
    """
    project_id = headless.project.project_id
    master = seat_scope(headless.database, project_id, AgentRole.MASTER)
    # A DECISION node: plannable, and the type a live Master has actually
    # committed and then had to withdraw. It needs no acceptance criteria —
    # nothing about it is measured against anything, because nothing runs it.
    task = prepare(node_type=NodeType.DECISION, with_acceptance=False)

    reason = unexecutable_reason(NodeType.DECISION)
    assert NodeType.DECISION.value in reason

    stopped = (await seat_tool("read_project_state", master)())["stopped"]
    assert [entry["node"] for entry in stopped] == [task.node.display_id], (
        "a node no seat can run is not reported as waiting on Master, so the one "
        f"role that could resolve it cannot see it: {stopped}"
    )
    entry = stopped[0]
    assert entry["status"] == NodeStatus.READY.value
    assert entry["node_type"] == NodeType.DECISION.value
    assert entry["unexecutable"] == reason, (
        "the project-state read does not carry the domain's own sentence for why "
        f"this node is not work: {entry}"
    )
    # The other two reasons are absent rather than empty strings, because a
    # node this read calls stopped has exactly one of them.
    assert entry["verdict"] is None
    assert entry["run_reconciliation"] is None

    # The turn Master is handed, read the way the loop reads it: with the seats
    # this process holds, which is what makes the node unexecutable rather than
    # merely unfinished.
    situation = read_situation(
        headless.database, project_id, executable=SEATED_NODE_TYPES
    )
    assert situation.unexecutable == (task.node,)
    assert situation.needs_decision, (
        "the loop stopped asking Master about a node nothing can run, which "
        "leaves the project holding it forever"
    )
    prompt = HarnessAgent(
        pool=None,  # type: ignore[arg-type]
        project_id=project_id,
        role=AgentRole.MASTER,
    )._master_prompt(situation)

    assert task.node.display_id in prompt, (
        f"Master is asked to decide about a node the turn never names: {prompt}"
    )
    assert reason in prompt, (
        "Master is told a node is waiting without being told what is wrong with "
        f"it, which is how a plan gets rewired around a node that cannot move: {prompt}"
    )
    assert "Nothing in the DAG is waiting on a decision." not in prompt, (
        "the turn was asked about nothing, which is the defect this case is for"
    )


async def test_p10_03_a_master_can_plan_a_project_that_has_no_roadmap(
    headless: Headless,
) -> None:
    """P10-03: planning starts with the roadmap, and Master has a way to write it.

    This is the second half of what a live Master found when it was first given
    a real project. The DAG tools require a roadmap stage — a node is filed
    under one, and `expand_dag_phase` expands a stage that already exists — and
    a project is created with no stages at all. The session refused to invent a
    plan against state it could not read and reported that no tool could commit
    a stage; it was right, and the project could not be started by any path.

    So the first planning act is asserted here as the Master's own: the stage is
    written through the tool a session reaches, the expansion that follows is
    filed under the name that was just written, and neither the refusal of a
    non-Master caller nor a repeated name leaves anything behind.

    What a stage commit deliberately does *not* write is a Decision Record. That
    is not an omission to be tidied up later: a decision records what changed
    among the nodes, and a stage on its own commits none — the decision arrives
    with the expansion, where the plan becomes checkable.
    """
    project_id = headless.project.project_id
    master = seat_scope(headless.database, project_id, AgentRole.MASTER)
    stage = "Stage 1: baseline series"

    assert "commit_roadmap_phase" in mutating_tools_for(AgentRole.MASTER), (
        "the tool that writes the roadmap is a DAG mutation, and Master is the "
        "only role that holds one"
    )

    def roadmap_state() -> tuple[list[str], int]:
        with headless.database.read_only() as session:
            names = [phase.name for phase in RoadmapRepository(session, project_id).phases()]
            decisions = len(DecisionRepository(session, project_id).all())
        return names, decisions

    assert roadmap_state() == ([], 0), (
        "a created project starts with no stages and no decisions; this case is "
        "about the first act, so it has to start from nothing"
    )

    # Nothing can be committed to a stage that does not exist yet, and the
    # refusal is what a session reads before deciding what to do next.
    with pytest.raises(HorizonError) as no_roadmap:
        await seat_tool("expand_dag_phase", master)(
            phase=stage,
            nodes=[A_BASELINE_NODE],
            rationale="Expanding a stage that has not been planned.",
        )
    assert "roadmap" in str(no_roadmap.value), (
        f"the refusal does not say the roadmap is missing: {no_roadmap.value}"
    )
    assert roadmap_state() == ([], 0), "the refused expansion wrote something anyway"

    written = await seat_tool("commit_roadmap_phase", master)(
        name=stage,
        order=0,
        intent="Measure the baseline series before anything is varied.",
    )
    assert written["phase"]["name"] == stage
    assert written["current_phase"] == stage, (
        "the only stage on the roadmap is the one the project is on"
    )
    assert written["expandable_phases"] == [stage]
    assert roadmap_state() == ([stage], 0), (
        "committing a stage recorded a decision; a decision is about the nodes a "
        "change committed, and a stage commits none"
    )

    # The same stage twice is refused with the roadmap named, because a session
    # that cannot read the refusal cannot plan around it.
    with pytest.raises(ValueError) as duplicate:
        await seat_tool("commit_roadmap_phase", master)(
            name=stage, order=1, intent="The same stage, planned again."
        )
    assert stage in str(duplicate.value), (
        f"the refusal does not name the stage already there: {duplicate.value}"
    )
    assert roadmap_state() == ([stage], 0), "the refused commit wrote something anyway"

    expansion = await seat_tool("expand_dag_phase", master)(
        phase=stage,
        nodes=[A_BASELINE_NODE],
        rationale="The stage is planned; this is the measurement it begins with.",
    )
    created = [node["node_id"] for node in expansion["nodes"]]
    assert [node["roadmap_phase"] for node in expansion["nodes"]] == [stage], (
        "the expansion was not filed under the stage it was submitted for"
    )

    with headless.database.read_only() as session:
        decisions = DecisionRepository(session, project_id).all()
        nodes = DagRepository(session, project_id).nodes()
    assert len(decisions) == 1, (
        f"expanding the stage wrote {len(decisions)} decisions; a stage's work is "
        "committed under one decision, and a stage commit writes none"
    )
    assert set(decisions[0].affected_nodes.created) == set(created)
    assert {node.node_id for node in nodes} == set(created)

    # A stage is Master's to write even though the union of every other role's
    # tools cannot reach the roadmap at all.
    research = seat_scope(headless.database, project_id, AgentRole.RESEARCH)
    with pytest.raises(PermissionError) as refused:
        await seat_tool("commit_roadmap_phase", research)(
            name="Stage 2: derived from someone else's plan",
            order=1,
            intent="A stage this session has no authority to add.",
        )
    assert AgentRole.RESEARCH.value in str(refused.value), (
        f"the refusal does not say which session was refused: {refused.value}"
    )
    assert headless.project.project_id in str(refused.value), (
        "the refusal names a role but not the project it was refused in, so a "
        f"multi-project deployment cannot tell the two sessions apart: {refused.value}"
    )
    assert roadmap_state() == ([stage], 1), "the refused commit changed the roadmap"


#: The `required_outputs` entry a live Master wrote on its fifth run: a sentence
#: describing the deliverable, with a slash in it. Kept verbatim, because the
#: case below is about what that costs and a paraphrase would be a different
#: string.
A_DESCRIPTION_INSTEAD_OF_A_NAME = (
    "A ranked series of Ti1-xNbxO2 compositions with predicted relative "
    "transport proxy, stability/phase-purity assessment, convergence evidence, "
    "and method-sensitivity analysis."
)


async def test_p10_03_a_master_output_that_is_not_a_file_name_is_refused_at_planning(
    headless: Headless,
) -> None:
    """P10-03: a required output is a file name, and the plan is refused without one.

    This is the third thing a live Master found, and the most expensive of them.
    It expanded a stage whose COMPUTATION node required the output quoted above
    — a description of a deliverable, not a name. The plan was accepted, the
    pre-flight cleared the node, the run started, and then the run could not
    record what it produced: the backend writes a file under that name, the
    completeness check compares delivered names to required ones by equality,
    and the storage key is built from it, so the activity failed five retries
    deep inside Temporal. Nothing reached a seat. The node never ended, and the
    project sat EXECUTING until the run's own budget gave out — an item meant to
    certify five autonomous agents, stopped by one slash in a plan.

    So the rule is stated once, in the domain, and asked at the moment a node is
    planned, next to the refusal that already guards a node committed with no
    criteria. What the case asserts is the shape of the fix rather than the
    wording: the refusal arrives at the tool a Master calls, it names the output
    and says what is wrong with it, and — because the plan is written in one
    transaction — the refused expansion leaves no node, no contract and no
    decision behind.

    The same stage then commits with a name in that place, which is the other
    half of the item: the gate refuses a description, not the work.
    """
    project_id = headless.project.project_id
    master = seat_scope(headless.database, project_id, AgentRole.MASTER)
    stage = "Stage 1: ranked composition series"

    def plan_state() -> tuple[list[DagNode], int, list[Any]]:
        with headless.database.read_only() as session:
            return (
                list(DagRepository(session, project_id).nodes()),
                len(DecisionRepository(session, project_id).all()),
                list(ExecutionContractRepository(session, project_id).all()),
            )

    await seat_tool("commit_roadmap_phase", master)(
        name=stage,
        order=0,
        intent="Rank candidate compositions before anything is measured.",
    )
    assert plan_state() == ([], 0, []), "the roadmap commit planned a node"

    described = dict(A_BASELINE_NODE)
    described["ref"] = "ranked-series"
    described["required_outputs"] = [A_DESCRIPTION_INSTEAD_OF_A_NAME]
    with pytest.raises(ValueError) as refusal:
        await seat_tool("expand_dag_phase", master)(
            phase=stage,
            nodes=[described],
            rationale="The series is modelled before the best of it is measured.",
        )
    message = str(refusal.value)
    assert A_DESCRIPTION_INSTEAD_OF_A_NAME in message, (
        f"the refusal does not quote the output it is about: {message}"
    )
    assert "file name" in message, (
        f"the refusal does not say what a required output is: {message}"
    )
    nodes, decisions, contracts = plan_state()
    assert (nodes, decisions, contracts) == ([], 0, []), (
        "the refused expansion left a node, a decision or a contract behind: a "
        "plan is written in one transaction, and half of one is a node nobody "
        "may start"
    )

    # The live failure came from `expand_dag_phase`, but the rule lives under
    # both planning tools — they write their terms through one function, and a
    # gate that only one of them went through would be a gate a Master could
    # walk around by choosing the other tool.
    with pytest.raises(ValueError) as single:
        await seat_tool("add_dag_node", master)(
            node_type="COMPUTATION",
            objective="Rank the candidate compositions.",
            rationale="The same output, committed one node at a time.",
            criteria=A_BASELINE_NODE["criteria"],
            required_outputs=[A_DESCRIPTION_INSTEAD_OF_A_NAME],
            roadmap_phase=stage,
        )
    assert A_DESCRIPTION_INSTEAD_OF_A_NAME in str(single.value)
    assert plan_state() == ([], 0, []), "the refused node was written anyway"

    named = dict(A_BASELINE_NODE)
    named["required_outputs"] = ["ranked_series.csv", "notes.json"]
    expansion = await seat_tool("expand_dag_phase", master)(
        phase=stage,
        nodes=[named],
        rationale="The same work, requiring outputs that can be files.",
    )
    (node,) = expansion["nodes"]
    with headless.database.read_only() as session:
        contract = ExecutionContractRepository(session, project_id).for_node(
            node["node_id"]
        )
    assert list(contract.required_outputs) == ["ranked_series.csv", "notes.json"], (
        "the committed contract does not require the outputs the plan named"
    )


# ── P10-03, the Research seat at work ───────────────────────────────────────


class ResearchSeat:
    """The Research seat, acting through the tools a live session holds.

    Implements `ExecutorPort`, and it is the same two acts as a Worker's under
    Research's names: `start` is `begin_research`, which takes the task into
    this seat's hands, and the turn is `submit_research_record`, which hands the
    result over. Both go through the real handlers, so a task that lives through
    this seat lives through the same doors a DSH session uses — the role check,
    the DAG's preconditions and the completion contract included. What the seat
    keeps is what the tools answered, because that is what the case reads.
    """

    def __init__(self, context: ToolContext) -> None:
        self.context = context
        self.started: list[str] = []
        self.turns: list[tuple[str, str]] = []
        self.beginnings: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []

    async def start(self, node: DagNode) -> None:
        self.started.append(node.node_id)
        self.beginnings.append(
            await seat_tool("begin_research", self.context)(node_id=node.node_id)
        )

    async def act(self, node: DagNode, situation: Situation) -> None:
        _ = situation
        self.turns.append((node.node_id, node.status.value))
        self.records.append(
            await seat_tool("submit_research_record", self.context)(node_id=node.node_id)
        )

    def close(self) -> None:
        """A scripted seat holds no runtime, so there is nothing to close."""


class ReviewOfTheContract:
    """Review's FINAL verdict on a node measured against its Execution Contract.

    A RESEARCH node freezes no acceptance criteria — it is not a type that runs
    an expensive backend, so there is nothing to pre-flight and nothing to
    freeze — and `ReviewService.definition_of_done` measures it against its
    Execution Contract instead. So the verdict answers no criteria, which is
    what `submit_review` does when it is not handed any: the contract it names
    comes from the node, not from here.

    It reads the package before it writes the verdict, because that is the only
    per-node view Review has and writing a verdict without it is a verdict on
    nothing. What it read is kept, so a test can ask what the judge was given
    rather than only what it said.
    """

    def __init__(self, context: ToolContext, outcome: ReviewOutcome) -> None:
        self.context = context
        self.outcome = outcome
        self.packages: list[dict[str, Any]] = []
        self.verdicts: list[dict[str, Any]] = []

    async def act(self, situation: Situation) -> None:
        for node in situation.nodes:
            if node.status is not NodeStatus.REVIEWING:
                continue
            self.packages.append(
                await seat_tool("read_review_package", self.context)(node_id=node.node_id)
            )
            self.verdicts.append(
                await seat_tool("submit_review", self.context)(
                    node_id=node.node_id,
                    checkpoint=ReviewCheckpoint.FINAL.value,
                    outcome=self.outcome.value,
                    diagnosis=(
                        "The task handed over a record with its gaps named; that is a "
                        "result to judge rather than one to accept."
                    ),
                )
            )


async def test_p10_03_a_research_task_lives_and_ends_through_the_loop(
    headless: Headless, research_task: Prepared
) -> None:
    """P10-03: the Research seat is a life a task can have, not a role on a list.

    `NODE_EXECUTOR` has always said Research executes a RESEARCH node. What was
    missing was a seat that could: nothing began the node, so it sat READY while
    the loop burned rounds and halted. This drives the whole of one task's life
    instead — the seat takes it up through `begin_research`, comes back for its
    monitoring turn, hands the record over, and the node ends in a FINAL verdict
    like every other node.

    What the assertions are about is that the *node* lived and that the *seat*
    did the two things a seat does. It is a life through the real transitions
    (READY → RUNNING → REVIEWING → PARTIAL) and not a run: there is no Execution
    Record and no BackendJob anywhere under it, because a research task's work is
    the reading and its result is a Research Record. The Workers are recording
    seats in this wiring, so a task that reached one of them would show up as a
    started node rather than as a silently successful test.
    """
    prepared = research_task
    seat = ResearchSeat(worker_context(headless, AgentRole.RESEARCH))
    review = ReviewOfTheContract(
        worker_context(headless, AgentRole.REVIEW), ReviewOutcome.PARTIAL
    )
    compute, experimental = RecordingWorker(), RecordingWorker()
    loop = ProjectLoop(
        database=headless.database,
        project_id=headless.project.project_id,
        master=Idle(),
        review=review,
        compute_worker=compute,
        experimental_worker=experimental,
        research=seat,
        poll_seconds=0.01,
    )

    # The DAG clears the node and the seat begins the task, once.
    await loop.step()
    assert seat.started == [prepared.node_id], (
        "the Research seat was not handed the node the DAG cleared"
    )
    assert seat.beginnings[0]["started"] is True, seat.beginnings[0].get("reason")
    assert headless.status_of(prepared.node) is NodeStatus.RUNNING
    assert compute.started == [] and experimental.started == [], (
        "a Worker was asked to begin a research task"
    )

    # The live task is the seat's turn, and the turn is the hand-over.
    await loop.step()
    assert seat.turns == [(prepared.node_id, NodeStatus.RUNNING.value)], (
        "the Research seat was not given a turn on the task it had begun"
    )
    handed_over = seat.records[0]
    assert handed_over["handed_over"] is True, handed_over["hand_over_reason"]
    assert handed_over["completion_status"] == CompletionStatus.INCOMPLETE.value, (
        "a task that read nothing reported itself complete"
    )
    assert headless.status_of(prepared.node) is NodeStatus.REVIEWING, (
        "the record was written and the node was not handed over with it"
    )

    # The FINAL verdict ends it, measured against the contract it ran under.
    await loop.step()
    assert review.verdicts[0]["moved_to"] == NodeStatus.PARTIAL.value, (
        f"the verdict did not end the node: {review.verdicts[0]}"
    )
    assert headless.status_of(prepared.node) is NodeStatus.PARTIAL

    # And it was written from the delivery, not from the two keys a research
    # node leaves empty. A judge given only those reads a finished task as a
    # non-delivery, which is not a hypothetical: it is what the first live
    # Review seat did, in those words, before the package carried the record.
    package = review.packages[0]
    assert package["execution"] is None and package["artifacts"] == [], (
        "the wiring gave this node a run to judge"
    )
    assert [record["research_id"] for record in package["research"]["records"]] == [
        handed_over["record"]["research_id"]
    ], "the record the seat handed over was not in the package that judged it"

    with headless.database.read_only() as session:
        written = ResearchRecordRepository(
            session, headless.project.project_id
        ).for_node(prepared.node_id)
        records = RecordRepositories(session, headless.project.project_id)
        executions = records.executions.for_node(prepared.node_id)
        jobs = records.jobs.for_node(prepared.node_id)

    assert [record.research_id for record in written] == [
        handed_over["record"]["research_id"]
    ], "the record the seat submitted is not the record the project holds"
    assert executions == [], (
        "a research task produced an Execution Record, so something ran it as a job"
    )
    assert jobs == [], "a research task left a BackendJob behind"


# ── P10-14 ──────────────────────────────────────────────────────────────────


async def test_p10_14_a_lab_that_waits_is_resumed_after_the_event(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-14: a wait that lasts days costs one turn at each end and nothing between.

    `LAB_LONG_WAIT` answers when something outside RAVEL says so, which is the
    shape of every real bench task: the Worker submits, says what it needs to
    say, and is not asked again for as long as the wait lasts. What ends it is
    the event, and after the event the node is out of the Worker's hands — the
    run collects the delivery, records it, and hands the result to Review.

    Nothing here holds a turn open across the wait. The loop's episode rule is
    what makes that true: `WAITING_EXTERNAL` is one episode, so the Worker is
    given one turn for the whole of it however long it is.
    """
    headless.lab("LAB_LONG_WAIT")
    prepared = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)
    await start_run(headless, prepared)
    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL)

    compute, experimental = RecordingWorker(), RecordingWorker()
    loop = await loop_for(headless, compute, experimental)
    await loop.step()
    assert experimental.turns == [(prepared.node_id, NodeStatus.WAITING_EXTERNAL.value)]

    # Days could pass here. The loop is asked again and again, and the Worker is
    # not: the wait is one episode, and it has been served.
    for _ in range(3):
        await loop.step()
    assert len(experimental.turns) == 1, (
        "the Worker was asked again mid-wait, so a three-day bench task would be "
        "a three-day model bill"
    )

    with headless.database.read_only() as session:
        records = RecordRepositories(session, headless.project.project_id)
        assert records.executions.for_node(prepared.node_id) == [], (
            "the run recorded an execution for a lab that has not answered"
        )

    # The event. Nothing in RAVEL could have substituted for it: the run is in a
    # durable wait and this is what ends it.
    await headless.client.deliver_external_result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
        result=ExternalResult(
            summary="The operator confirmed the run.",
            delivered_outputs=LAB_OUTPUTS,
        ),
    )
    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.REVIEWING)

    await loop.step()
    assert experimental.turns == [(prepared.node_id, NodeStatus.WAITING_EXTERNAL.value)], (
        "the Worker was handed a node that is no longer in its hands, so its turn "
        "was about work the run had already taken back"
    )

    with headless.database.read_only() as session:
        records = RecordRepositories(session, headless.project.project_id)
        executions = records.executions.for_node(prepared.node_id)
    assert executions, "the wait ended without the run recording what the lab delivered"


# ── P10-W12 ─────────────────────────────────────────────────────────────────────


async def test_p10_w12_a_retry_the_contract_permits_is_carried_out(
    headless: Headless,
) -> None:
    """P10-W12: an authorized retry happens, and the Worker is the one who sees it.

    A07 already asserts that a retryable infrastructure failure is retried under
    the *same* contract rather than a widened one. What Phase 10 adds is the
    seat: the run began because a Worker asked for it, the retry is the durable
    layer's, and the Worker reads back an attempt-2 job without having decided
    anything. So the assertion is on what the Worker sees — the attempt number
    and the failure class that permitted the retry — because a Worker that could
    not see the retry would be a seat reporting on a run it is not actually
    watching.
    """
    scenario = catalogue().compute_scenario("COMPUTE_RETRYABLE_INFRA_FAILURE")
    assert scenario.requires_contract_retry_permission, "the catalogue changed meaning"

    headless.compute("COMPUTE_RETRYABLE_INFRA_FAILURE")
    measure = "Measure conductivity across the dopant series."
    nodes = Nodes(headless.project.project_id)
    # The run is driven to a verdict rather than started and left, because what
    # the Worker reads back is what the run *recorded*, and a run still in
    # flight has recorded nothing.
    run = await headless.drive(
        headless.master((nodes.task("measure", measure, allowed_retries=1),)),
        headless.review(),
    )
    assert run.finished, f"the loop halted at {run.status.value}"
    node = nodes["measure"]
    assert headless.status_of(node) is NodeStatus.PASSED

    context = worker_context(headless, AgentRole.COMPUTE_WORKER)
    seen = await seat_tool("read_execution_status", context)(node_id=node.node_id)
    execution = seen["execution"]
    assert execution is not None, "the run ended without the Worker being able to read it"
    # `attempt_count` is computed, so what the tool serialises is the attempts
    # themselves; counting them here is the same question asked of the payload.
    attempts = execution["attempts"]
    assert len(attempts) == 2, (
        "the scenario fails the first attempt and completes the second; a run with "
        f"{len(attempts)} attempts played a different scenario"
    )
    assert [a["attempt"] for a in attempts] == [1, 2], (
        f"the attempts are not numbered from one: {[a['attempt'] for a in attempts]}"
    )

    with headless.database.read_only() as session:
        jobs = BackendJobRepository(session, headless.project.project_id).for_node(
            node.node_id
        )
        contract = ExecutionContractRepository(
            session, headless.project.project_id
        ).for_node(node.node_id)

    assert [(job.attempt, job.state) for job in jobs] == [
        (1, JobState.FAILED),
        (2, JobState.COMPLETED),
    ], f"the attempts are not a failed first and a completed second: {jobs}"
    assert jobs[0].failure_class is FailureClass.INFRA_RETRYABLE, (
        "an INFRA_RETRYABLE failure is the only kind a Worker may retry without "
        "asking, and the class is what says so"
    )
    assert {job.execution_contract_version for job in jobs} == {contract.version}, (
        "the retry ran under a different contract version from the attempt it "
        "retried, which is a revision and not a retry"
    )


# ── P10-W14 ─────────────────────────────────────────────────────────────────────


async def test_p10_w14_an_unauthorized_substitution_is_refused(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W14: a swap the contract does not list is refused in code, not in prose.

    The contract names exactly one permitted substitution, so the Worker asks
    for a different one — a literal change of scientific method dressed as an
    execution detail, which is the thing a Worker must never be able to do on
    its own authority. The refusal is the `worker_rules` lookup's, and what it
    leaves behind is what Phase 10 needs it to leave: a deviation Master can
    answer, an escalation saying the Worker did not answer it, an unchanged
    contract, and a node that did not move.
    """
    a_run_that_stays_live(headless, "COMPUTE_TIMEOUT")
    prepared = prepare(
        node_type=NodeType.COMPUTATION,
        required_outputs=COMPUTE_OUTPUTS,
        allowed_substitutions=("Pd/C -> Pt/C",),
    )
    await start_run(headless, prepared)
    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.RUNNING)

    context = worker_context(headless, AgentRole.COMPUTE_WORKER)
    answer = await seat_tool("request_action", context)(
        node_id=prepared.node_id,
        requested_action="swap catalyst",
        substitute=["Pd/C", "Ir/C"],
        description="The backend proposes iridium instead of platinum.",
    )

    assert answer["permitted"] is False, (
        "the contract lists one substitution and the Worker asked for another; a "
        "permitted answer means the lookup is not reading the list"
    )
    assert "does not list the substitution" in answer["reason"], (
        f"the refusal does not say which silence it found: {answer['reason']!r}"
    )
    assert answer["deviation_id"], "a refusal that records no deviation is a dead end"

    with headless.database.read_only() as session:
        deviations = DeviationRepository(
            session, headless.project.project_id
        ).open()
        messages = RecordRepositories(
            session, headless.project.project_id
        ).messages.for_node(prepared.node_id)
        contract = ExecutionContractRepository(
            session, headless.project.project_id
        ).for_node(prepared.node_id)

    assert [d.requested_action for d in deviations] == ["swap catalyst"], (
        "the recorded deviation is not the substitution that was asked for"
    )
    assert deviations[0].permitted is False
    assert "Ir/C" in deviations[0].description and "Pd/C" in deviations[0].description, (
        "the deviation does not record which pair was asked for, so a later reader "
        f"cannot tell what was refused: {deviations[0].description!r}"
    )
    assert any(m.kind is WorkerMessageKind.ESCALATE for m in messages), (
        "the refusal was recorded without the escalation that says the Worker did "
        "not answer the question itself"
    )
    assert contract.version == prepared.contract.version, (
        "a Worker widened the contract it was supposed to be working under"
    )
    assert contract.allowed_substitutions == ("Pd/C -> Pt/C",)
    assert headless.status_of(prepared.node) is NodeStatus.RUNNING, (
        "the Worker moved a node whose ending belongs to the run"
    )


# ── P10-W17 ─────────────────────────────────────────────────────────────────────


async def test_p10_w17_the_worker_identity_continues_after_a_wait_ends(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W17: a wait that ends does not end the seat that was holding it.

    Phase 10F's model is that a Worker's *identity* is the task lifetime and its
    session is recoverable, so the thing worth showing after a long wait is not
    that the wait ended but that the seat is still the seat. Two experiments are
    in the lab's hands at once. The event arrives for one of them; it leaves the
    wait, is judged, and is no longer the Worker's business — while the other is
    still waiting, and the same seat still takes its turn on it.

    Without that, a Worker would be consumed by the first wait it sat through,
    and the second task of a project would have nobody serving it.
    """
    headless.lab("LAB_LONG_WAIT")
    mine = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)
    theirs = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)

    context = worker_context(headless, AgentRole.EXPERIMENTAL_WORKER)
    for prepared in (mine, theirs):
        answer = await seat_tool("start_execution", context)(node_id=prepared.node_id)
        assert answer["started"] is True, f"{prepared.node_id} did not start: {answer}"
    for prepared in (mine, theirs):
        await await_state(
            lambda p=prepared: headless.status_of(p.node) is NodeStatus.WAITING_EXTERNAL
        )

    # The event, for one of them.
    await headless.client.deliver_external_result(
        node_id=mine.node_id,
        execution_contract_version=mine.contract.version,
        result=ExternalResult(
            summary="The operator confirmed the first run.",
            delivered_outputs=LAB_OUTPUTS,
        ),
    )
    await await_state(lambda: headless.status_of(mine.node) is NodeStatus.REVIEWING)

    # The seat is intact: it holds the same scope, and the task still in the
    # lab's hands is still readable through it.
    assert headless.status_of(theirs.node) is NodeStatus.WAITING_EXTERNAL
    seen = await seat_tool("read_execution_status", context)(node_id=theirs.node_id)
    assert seen["node_status"] == NodeStatus.WAITING_EXTERNAL.value
    assert seen["job"] is not None, "the other task lost its job at the lab"
    assert seen["job"]["state"] == JobState.WAITING_EXTERNAL.value, (
        "the wait that ended took the other task's job with it: "
        f"{seen['job']['state']!r}"
    )

    # And it takes its turn on the one still waiting, which is the claim: the
    # identity outlived the wait it sat through.
    compute, experimental = RecordingWorker(), RecordingWorker()
    loop = await loop_for(headless, compute, experimental)
    await loop.step()
    assert experimental.turns == [(theirs.node_id, NodeStatus.WAITING_EXTERNAL.value)], (
        "the seat that sat through the first wait was not given the second, so a "
        "Worker is consumed by the first long wait it serves"
    )


# ── P10-W19 ─────────────────────────────────────────────────────────────────────


async def test_p10_w19_two_projects_workers_cannot_reach_each_other(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W19: a Worker's scope is one project, and another project is not in it.

    The authorization boundary is the project, and the way it is enforced is
    that a tool is handed a scope rather than a project: the model supplies a
    node id and nothing else, and the session's project comes from the process
    it was launched in. So a Worker serving project A, told project B's node id,
    is asking about a node that does not exist as far as its scope can tell.

    Asserted through every tool a Worker holds that takes a node, because a
    boundary that holds on the read a test happens to try and not on the write
    beside it is not a boundary.
    """
    mine = prepare(node_type=NodeType.COMPUTATION, required_outputs=COMPUTE_OUTPUTS)
    other = second_project(headless.database, title="Someone else's")
    with headless.database.transaction() as session:
        theirs = build_prepared(
            session,
            project_id=other.project_id,
            node_type=NodeType.COMPUTATION,
            required_outputs=COMPUTE_OUTPUTS,
        )

    context = worker_context(headless, AgentRole.COMPUTE_WORKER)
    assert context.project_id == headless.project.project_id, "the scope is not this project"

    reads = {
        "read_execution_contract": ({"node_id": theirs.node_id},),
        "read_execution_status": ({"node_id": theirs.node_id},),
        "request_action": (
            {
                "node_id": theirs.node_id,
                "requested_action": "run_measurement",
                "description": "Reaching across projects.",
            },
        ),
    }
    for tool, (kwargs,) in reads.items():
        with pytest.raises(NotFound) as refused:
            await seat_tool(tool, context)(**kwargs)
        assert theirs.node_id in str(refused.value), (
            f"{tool} refused for a reason other than the node not being reachable "
            f"from this scope: {refused.value}"
        )

    # The same node id through this project's scope is the one that works, so
    # the refusals above are about the boundary rather than a broken argument.
    answer = await seat_tool("read_execution_status", context)(node_id=mine.node_id)
    assert answer["node_status"] == NodeStatus.READY.value

    # And nothing was written for the other project on the way through.
    with headless.database.read_only() as session:
        records = RecordRepositories(session, other.project_id)
        assert records.deviations.all() == [], (
            "a Worker wrote a deviation into a project it does not serve"
        )
        assert records.messages.for_node(theirs.node_id) == []
        assert DagRepository(session, other.project_id).node(theirs.node_id).status is (
            NodeStatus.READY
        ), "a Worker moved a node in a project it does not serve"
