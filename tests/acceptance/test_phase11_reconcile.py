"""P11-01: a run that is gone stops taking its node with it.

The five cases the plan names, each against the real stack: a real Temporal
cluster, a real worker running in this process, the real `NodeRunClient` the
start path uses, the real `TemporalWorkflowProbe`, and the real
`ExecutionReconciler` writing to real PostgreSQL. **The probe is not scripted
here.** That is the difference between this suite and
`tests/integration/reconcile`, and it is the difference that matters: those
prove the reconciler's logic, and these prove RAVEL can tell a live run from a
lost one by asking Temporal itself — including that a workflow nobody ever
started really does come back as `NOT_FOUND` rather than as a transport error
read as an excuse to do nothing.

What is arranged rather than real is the *backend*, in the one case that needs
it: an activity has to be made to exhaust its retries, and the honest way to do
that is for the work to fail. Elsewhere the backend is the scripted double the
durable-execution suite uses, for the reason that suite gives — these are tests
about the durable layer, and a scripted backend is how a run is put into the
state a case is about.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.acceptance.phase10_support import (
    ScriptedSeats,
    patched_agents,
    seat_scope,
    seat_tool,
)
from tests.e2e.conftest import Headless
from tests.integration.conftest import Prepared
from tests.integration.temporal.conftest import ScriptedBackend

from ravel.domain.enums import JobState, NodeStatus, ProjectStatus
from ravel.domain.reconciliation import RunReconciliation, WorkflowLiveness
from ravel.domain.roles import AgentRole
from ravel.dsh.agents import HarnessAgent
from ravel.execution.backends import JobOutputs
from ravel.execution.loop import read_situation
from ravel.execution.reconcile import ExecutionReconciler, TemporalWorkflowProbe
from ravel.execution.supervisor import ProjectSupervisor
from ravel.execution.temporal.worker import workflow_id_for
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.reconciliation import RunReconciliationRepository
from ravel.state.repositories.records import BackendJobRepository, RecordRepositories

pytestmark = [pytest.mark.phase11, pytest.mark.timeout(900)]

ACTOR = "compute-worker"

#: How long to let Temporal exhaust an activity's retries. The policy is
#: `maximum_attempts=5` with a doubling backoff from one second, so the four
#: gaps between the attempts are 1 + 2 + 4 + 8 seconds. The wait is the real
#: mechanism, and a case that shortened it would be testing a policy no
#: deployment runs.
RETRY_EXHAUSTION = 180.0


@dataclass
class FailingCollect(ScriptedBackend):
    """A backend whose job finishes and whose answer cannot be read.

    `check_job` sees the job end normally, and then `finish_node_run` — the one
    activity that writes — dies in `collect`. Five attempts later the workflow
    is FAILED, PostgreSQL holds the job at COMPLETED and the node at RUNNING,
    and nothing in RAVEL will ever write the ending. That is L-24 exactly, and
    the interesting part is what the recovery does with the job: it is terminal
    and it is the backend's own report, so it is left alone.
    """

    def collect(self, backend_job_ref: str) -> JobOutputs:
        raise RuntimeError(f"the result directory for {backend_job_ref} is gone")


def a_probe(headless: Headless) -> TemporalWorkflowProbe:
    """The real probe, pointed at the cluster this deployment talks to."""
    return TemporalWorkflowProbe(settings=headless.settings)


def a_sweep(headless: Headless, probe: TemporalWorkflowProbe) -> ExecutionReconciler:
    """A sweep with no grace window.

    The window protects a run that started a moment ago, and every run here
    starts a moment ago — so the window is off, and the cases about *healthy*
    runs have to prove their point by arranging a run that is genuinely alive
    rather than one that is merely young.
    """
    return ExecutionReconciler(
        database=headless.database, probe=probe, grace_seconds=0.0
    )


async def start_run(headless: Headless, prepared: Prepared) -> str:
    """Start the node's run the way a Worker's tool does, and name its workflow."""
    await headless.client.start_node_run(
        project_id=prepared.project_id,
        node_id=prepared.node_id,
        actor_id=ACTOR,
        execution_contract_version=prepared.contract.version,
    )
    return workflow_id_for(prepared.node_id, prepared.contract.version)


def strand_a_node(headless: Headless, prepared: Prepared) -> str:
    """Put a node into RUNNING with no run behind it at all.

    The purest form of the loss: PostgreSQL says work is under way and no
    workflow was ever started, which is what a Worker that died between asking
    for a run and Temporal accepting it leaves behind.
    """
    with headless.database.transaction() as session:
        DagRepository(session, prepared.project_id).transition_node(
            prepared.node_id, NodeStatus.RUNNING, actor_id=ACTOR
        )
    return workflow_id_for(prepared.node_id, prepared.contract.version)


async def liveness_is(
    probe: TemporalWorkflowProbe,
    workflow_id: str,
    *wanted: WorkflowLiveness,
    timeout: float = 60.0,
) -> WorkflowLiveness:
    """Wait for the real probe to report one of these, asking it each time.

    The probe is the subject as much as the recovery is, so the wait is written
    against it rather than against a database fact: a `NOT_FOUND` that arrived
    as `UNKNOWN` would leave this loop spinning, which is the failure mode that
    matters — an unreachable frontend must not read as a lost run.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    observed = WorkflowLiveness.UNKNOWN
    while asyncio.get_running_loop().time() < deadline:
        observed = await probe.liveness(workflow_id)
        if observed in wanted:
            return observed
        await asyncio.sleep(0.5)
    raise AssertionError(
        f"workflow {workflow_id} was {observed.value}, not "
        f"{' or '.join(item.value for item in wanted)}"
    )


@dataclass(frozen=True)
class AfterTheSweep:
    """What the sweep wrote, and what PostgreSQL says afterwards."""

    written: list[RunReconciliation]
    node: NodeStatus
    job: JobState | None
    reconciliations: list[RunReconciliation]


async def sweep(headless: Headless, prepared: Prepared) -> AfterTheSweep:
    """One sweep over this project, through the real probe."""
    probe = a_probe(headless)
    try:
        written = await a_sweep(headless, probe).reconcile([prepared.project_id])
    finally:
        await probe.close()
    database = headless.database
    with database.read_only() as session:
        node = DagRepository(session, prepared.project_id).node(prepared.node_id)
        job = BackendJobRepository(session, prepared.project_id).latest_for_node(
            prepared.node_id
        )
        found = RunReconciliationRepository(
            session, prepared.project_id
        ).for_node(prepared.node_id)
    return AfterTheSweep(
        written=written,
        node=node.status,
        job=job.state if job is not None else None,
        reconciliations=found,
    )


def status_of(database: Database, project_id: str, node_id: str) -> NodeStatus:
    with database.read_only() as session:
        return DagRepository(session, project_id).node(node_id).status


async def run_until(
    predicate, *, timeout: float = 120.0, what: str = "the expected state"
) -> None:
    """Wait for a database fact, which is the only kind worth waiting on."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"{what} did not appear within {timeout}s")


# ── Criterion one: an exhausted activity, and a workflow nobody started ─────


async def test_p11_01_a_run_that_died_writing_its_result_does_not_strand_its_node(
    headless: Headless, prepare
) -> None:
    """The run reaches the end, and the ending is never written down.

    The job completes, `finish_node_run` is called to record the Execution
    Record and move the node, and it cannot read the result — five times, and
    then the workflow fails. Before this phase the node stayed RUNNING for the
    life of the database while the loop read it as work in flight and took no
    turn that could move it.

    What the recovery must *not* do is half the point. The job is COMPLETED:
    the backend reported that the work finished, and RAVEL's failure was in
    recording it. So the job is left exactly as it is — terminal, and the only
    observation anybody made — and the loss is classified as RAVEL's own
    infrastructure rather than as anything to do with the science.
    """
    prepared = prepare()
    headless.registry.register(
        prepared.node.node_type,
        FailingCollect(states=[JobState.RUNNING, JobState.COMPLETED]),
    )
    workflow_id = await start_run(headless, prepared)

    probe = a_probe(headless)
    try:
        await liveness_is(
            probe,
            workflow_id,
            WorkflowLiveness.FAILED,
            WorkflowLiveness.TIMED_OUT,
            timeout=RETRY_EXHAUSTION,
        )
    finally:
        await probe.close()

    # L-24's state, reached the way L-24 reaches it.
    assert status_of(headless.database, prepared.project_id, prepared.node_id) is (
        NodeStatus.RUNNING
    )
    with headless.database.read_only() as session:
        job = BackendJobRepository(session, prepared.project_id).latest_for_node(
            prepared.node_id
        )
    assert job is not None
    assert job.state is JobState.COMPLETED

    recovered = await sweep(headless, prepared)

    assert len(recovered.written) == 1
    record = recovered.written[0]
    assert record.observed is WorkflowLiveness.FAILED
    assert record.failure_class.value == "INFRASTRUCTURE"
    assert recovered.node is NodeStatus.WAITING_DECISION
    assert recovered.job is JobState.COMPLETED
    assert record.job_state_before is JobState.COMPLETED
    assert record.job_state_after is JobState.COMPLETED


async def test_p11_01_a_node_whose_workflow_was_never_started_is_recovered(
    headless: Headless, prepare
) -> None:
    """The frontend's own `NOT_FOUND`, which is the fact this whole thing reads.

    A workflow id nobody ever started is the one case where a wrong probe would
    be invisible: a transport error is supposed to mean "ask again", and if
    `NOT_FOUND` arrived as `UNKNOWN` the sweep would report nothing wrong
    forever. So this asserts on the probe directly before it asserts on the
    recovery.
    """
    prepared = prepare()
    workflow_id = strand_a_node(headless, prepared)

    probe = a_probe(headless)
    try:
        observed = await liveness_is(probe, workflow_id, WorkflowLiveness.NOT_FOUND)
    finally:
        await probe.close()
    assert observed is WorkflowLiveness.NOT_FOUND

    recovered = await sweep(headless, prepared)

    assert len(recovered.written) == 1
    assert recovered.written[0].failure_class.value == "WORKFLOW_LOST"
    assert recovered.written[0].job_id is None
    assert recovered.node is NodeStatus.WAITING_DECISION


# ── What it must not touch ──────────────────────────────────────────────────


async def test_p11_01_a_run_that_is_still_alive_is_left_alone(
    headless: Headless, prepare
) -> None:
    """A real workflow, really RUNNING, and a sweep that does nothing.

    This is the mistake that would end every run in a deployment: reading a
    live run as a lost one. The backend holds the job in RUNNING and repeats
    that answer, so the run stays where it is for as long as the case looks —
    and the node, the job and the record table are all asserted unchanged.

    The job is compared against what it was a moment earlier rather than
    against RUNNING, because which of the two live states it is in depends on
    whether the first poll has landed yet — and a case that raced the workflow
    would fail for a reason that has nothing to do with the sweep.
    """
    prepared = prepare()
    headless.registry.register(
        prepared.node.node_type, ScriptedBackend(states=[JobState.RUNNING])
    )
    workflow_id = await start_run(headless, prepared)

    probe = a_probe(headless)
    try:
        assert await liveness_is(probe, workflow_id, WorkflowLiveness.RUNNING) is (
            WorkflowLiveness.RUNNING
        )
    finally:
        await probe.close()
    await run_until(
        lambda: status_of(headless.database, prepared.project_id, prepared.node_id)
        is NodeStatus.RUNNING,
        what="the run to reach its node",
    )
    with headless.database.read_only() as session:
        before = BackendJobRepository(session, prepared.project_id).latest_for_node(
            prepared.node_id
        )
    assert before is not None
    assert not before.state.is_terminal, "the run was already over before the sweep"

    recovered = await sweep(headless, prepared)

    assert recovered.written == []
    assert recovered.reconciliations == []
    assert recovered.node is NodeStatus.RUNNING
    assert recovered.job is before.state


async def test_p11_01_a_terminated_run_is_recovered_as_a_cancellation(
    headless: Headless, prepare
) -> None:
    """Somebody ended the run, and nobody is going to end its node.

    Terminating the workflow is what an operator stopping a stuck run does. It
    leaves the same stranded node an infrastructure failure does, and the
    difference is the one the classification exists for: this is not RAVEL's
    machinery failing, it is a person's decision — so the job is CANCELLED
    rather than FAILED, and a job that did not fail carries no failure class.
    """
    prepared = prepare()
    headless.registry.register(
        prepared.node.node_type, ScriptedBackend(states=[JobState.RUNNING])
    )
    workflow_id = await start_run(headless, prepared)
    await run_until(
        lambda: status_of(headless.database, prepared.project_id, prepared.node_id)
        is NodeStatus.RUNNING,
        what="the run to reach its node",
    )

    await headless.client.client.get_workflow_handle(workflow_id).terminate()
    probe = a_probe(headless)
    try:
        assert await liveness_is(probe, workflow_id, WorkflowLiveness.TERMINATED) is (
            WorkflowLiveness.TERMINATED
        )
    finally:
        await probe.close()

    recovered = await sweep(headless, prepared)

    assert len(recovered.written) == 1
    assert recovered.written[0].failure_class.value == "CANCELLED"
    assert recovered.node is NodeStatus.WAITING_DECISION
    assert recovered.job is JobState.CANCELLED
    with headless.database.read_only() as session:
        job = BackendJobRepository(session, prepared.project_id).latest_for_node(
            prepared.node_id
        )
    assert job is not None
    assert job.failure_class is None


# ── Criteria three and five: idempotency, and no verdict ────────────────────


async def test_p11_01_sweeping_twice_finds_the_loss_once(
    headless: Headless, prepare
) -> None:
    """Criterion three, against the cluster rather than a scripted answer.

    The second sweep is the same sweep: the same live nodes, the same real
    `NOT_FOUND`. It must write no second record and move nothing — so the
    assertions are not only about the record count but about the job and the
    node, because a sweep that re-ended a job already ended would fail a
    transition rather than a comparison.
    """
    prepared = prepare()
    strand_a_node(headless, prepared)

    first = await sweep(headless, prepared)
    second = await sweep(headless, prepared)

    assert len(first.written) == 1
    assert second.written == []
    assert len(second.reconciliations) == 1
    assert second.node is NodeStatus.WAITING_DECISION
    assert second.reconciliations[0].reconciliation_id == (
        first.reconciliations[0].reconciliation_id
    )


async def test_p11_01_a_lost_run_leaves_a_question_and_no_scientific_verdict(
    headless: Headless, prepare
) -> None:
    """Criterion five: the loss is not allowed to become a result.

    A run RAVEL lost delivered nothing. If the recovery wrote an Execution
    Record it would be attesting to work nobody saw; if it moved the node to
    REVIEWING it would be asking for a verdict on an empty result; and if it
    failed the node it would be calling a broken queue a failed experiment.
    What it does instead is leave the node non-terminal and waiting on Master,
    which is why the project cannot conclude around it.
    """
    prepared = prepare()
    strand_a_node(headless, prepared)

    recovered = await sweep(headless, prepared)
    assert len(recovered.written) == 1

    with headless.database.read_only() as session:
        records = RecordRepositories(session, prepared.project_id)
        assert records.executions.for_node(prepared.node_id) == []
        assert records.decisions.all() == []
        assert records.deviations.all() == []
        node = DagRepository(session, prepared.project_id).node(prepared.node_id)
        assert node.status is NodeStatus.WAITING_DECISION
        assert not node.is_terminal
        project = ProjectRegistry(session).get(prepared.project_id)
    assert project.status is not ProjectStatus.COMPLETED

    with headless.database.read_only() as session:
        stream = list(
            session.execute(
                text(
                    "SELECT event_type FROM project_events "
                    "WHERE project_id = :project ORDER BY seq"
                ),
                {"project": prepared.project_id},
            ).scalars()
        )
    assert "NODE_FAILED" not in stream
    assert "EXECUTION_RECORDED" not in stream


async def test_p11_01_master_is_told_what_happened_to_the_run(
    headless: Headless, prepare
) -> None:
    """A question Master cannot see is a question Master cannot answer.

    The recovery parks the node where Master is asked, and without this the
    only thing Master would find is a node that stopped for no reason it can
    read — the `stopped` block reports a reason from a review verdict, and a
    lost run has none. So the reason travels with the record, and every field
    of it is a report from somewhere else: Temporal's word for the run, the
    job's state, and the class RAVEL read off the two. Nothing in it is a claim
    about the science, which is the property that keeps a lost queue from
    reading as a failed experiment.
    """
    prepared = prepare()
    strand_a_node(headless, prepared)
    recovered = await sweep(headless, prepared)
    assert len(recovered.written) == 1

    master = seat_scope(
        headless.database, prepared.project_id, AgentRole.MASTER
    )
    state = await seat_tool("read_project_state", master)()
    stopped = state["stopped"]

    assert [entry["node"] for entry in stopped] == [prepared.node.display_id]
    entry = stopped[0]
    assert entry["status"] == NodeStatus.WAITING_DECISION.value
    assert entry["verdict"] is None, (
        "a lost run has no verdict; reporting one would invent a seat's judgement"
    )
    assert entry["run_reconciliation"] == {
        "reconciliation_id": recovered.written[0].reconciliation_id,
        "failure_class": "WORKFLOW_LOST",
        "observed": "NOT_FOUND",
        "workflow_id": recovered.written[0].workflow_id,
        "execution_contract_version": prepared.contract.version,
        "job_id": None,
        "job_state_before": None,
        "job_state_after": None,
        "detail": recovered.written[0].detail,
        "detected_by": "execution-reconciler",
        "created_at": recovered.written[0].created_at.isoformat(),
    }

    # And the turn that hands Master the node points at where the reason is.
    situation = read_situation(headless.database, prepared.project_id)
    prompt = HarnessAgent(
        pool=None,  # type: ignore[arg-type]
        project_id=prepared.project_id,
        role=AgentRole.MASTER,
    )._master_prompt(situation)
    assert "run_reconciliation" in prompt, (
        f"Master is handed a stopped node without being told why: {prompt}"
    )


# ── Criterion two: a supervisor restart ─────────────────────────────────────


@dataclass
class QuietSeat:
    """A seat that takes no turn.

    The subject here is the supervisor's *tick*, and a scripted Master that
    answered the loss would be answering the very question the case exists to
    watch being asked. So Master and Review are quiet, and what is left is the
    machinery: the pool, one loop per project, and the recovery that runs before
    either.
    """

    async def act(self, situation: object) -> None:
        return None

    def close(self) -> None:
        return None


async def test_p11_01_a_supervisor_recovers_a_run_that_died_before_it_started(
    headless: Headless, prepare, seats: ScriptedSeats, monkeypatch
) -> None:
    """Criterion two: nothing here was held in memory, so a restart recovers.

    The node has been stranded for an hour of `started_at` — past the grace
    window a deployment runs with — and no sweep has ever seen it. A supervisor
    starts over the project, and its first tick is the tick that recovers it,
    because `_reconcile` runs before the loops: a project holding a stranded
    node is one the loop cannot move.

    A freshly constructed supervisor is what a restart produces, which is the
    whole reason this works. It reads PostgreSQL and Temporal and holds nothing
    else, so the supervisor that finds the loss need not be the one that
    survived it.
    """
    prepared = prepare()
    strand_a_node(headless, prepared)
    with headless.database.transaction() as session:
        session.execute(
            text(
                "UPDATE dag_nodes SET started_at = started_at - :age WHERE node_id = :node"
            ),
            {"age": timedelta(hours=1), "node": prepared.node_id},
        )
    seats.override(prepared.project_id, AgentRole.MASTER, QuietSeat())
    seats.override(prepared.project_id, AgentRole.REVIEW, QuietSeat())

    with patched_agents(monkeypatch, seats):
        supervisor = ProjectSupervisor(
            database=headless.database,
            settings=headless.settings,
            poll_seconds=0.05,
            loop_poll_seconds=0.02,
        )
        task = asyncio.create_task(supervisor.run())
        try:
            await run_until(
                lambda: status_of(
                    headless.database, prepared.project_id, prepared.node_id
                )
                is NodeStatus.WAITING_DECISION,
                timeout=120.0,
                what="the supervisor's sweep to recover the stranded node",
            )
        finally:
            supervisor.stop()
            await asyncio.wait_for(task, timeout=60.0)

    recovered = await sweep(headless, prepared)
    assert recovered.written == []
    assert len(recovered.reconciliations) == 1
    assert recovered.reconciliations[0].failure_class.value == "WORKFLOW_LOST"
    assert recovered.reconciliations[0].detected_by == "execution-reconciler"


async def test_p11_01_the_supervisor_does_not_start_a_run(
    headless: Headless, prepare, seats: ScriptedSeats, monkeypatch
) -> None:
    """The control plane recovers a node and still does not run anything.

    Recovering a node means putting it where Master is asked, and the one thing
    that must not follow is RAVEL starting the work again on its own account.
    Temporal's id refuses a duplicate start, but a rescue that *tried* would be
    a rescue deciding that work happens again — which is a decision, and this
    process holds no decision. Nothing is started, and the node waits.
    """
    prepared = prepare()
    workflow_id = strand_a_node(headless, prepared)
    with headless.database.transaction() as session:
        session.execute(
            text(
                "UPDATE dag_nodes SET started_at = started_at - :age WHERE node_id = :node"
            ),
            {"age": timedelta(hours=1), "node": prepared.node_id},
        )
    seats.override(prepared.project_id, AgentRole.MASTER, QuietSeat())
    seats.override(prepared.project_id, AgentRole.REVIEW, QuietSeat())

    with patched_agents(monkeypatch, seats):
        supervisor = ProjectSupervisor(
            database=headless.database,
            settings=headless.settings,
            poll_seconds=0.05,
            loop_poll_seconds=0.02,
        )
        task = asyncio.create_task(supervisor.run())
        try:
            await run_until(
                lambda: bool(
                    reconciliations(headless.database, prepared.project_id, prepared.node_id)
                ),
                timeout=120.0,
                what="the supervisor's sweep to write its reconciliation",
            )
            await asyncio.sleep(1.0)
        finally:
            supervisor.stop()
            await asyncio.wait_for(task, timeout=60.0)

    probe = a_probe(headless)
    try:
        assert await liveness_is(probe, workflow_id, WorkflowLiveness.NOT_FOUND) is (
            WorkflowLiveness.NOT_FOUND
        )
    finally:
        await probe.close()
    with headless.database.read_only() as session:
        assert RecordRepositories(session, prepared.project_id).executions.for_node(
            prepared.node_id
        ) == []
    assert status_of(headless.database, prepared.project_id, prepared.node_id) is (
        NodeStatus.WAITING_DECISION
    )


def reconciliations(
    database: Database, project_id: str, node_id: str
) -> list[RunReconciliation]:
    """The node's reconciliations, for a polling predicate to read."""
    with database.read_only() as session:
        return RunReconciliationRepository(session, project_id).for_node(node_id)
