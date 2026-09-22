"""Recovering a node whose run is gone, against a real database.

The reconciler's job is narrow, and every test here is about its edges rather
than its middle: what it does to a node whose workflow Temporal says has ended,
and — more of them — what it refuses to do to a node it cannot prove anything
about. The probe is scripted, because a Temporal cluster cannot be asked to
lose a workflow on demand; everything under it is real, including the
transitions, the append-only guards, and the event stream the recovery writes
into.

The state each test starts from is built the way production builds it —
`prepare` is the same fixture the backend and review suites use, and the run is
started through `DagRepository.transition_node` and `BackendJobRepository.start`
— so a test cannot arrange a node no run could have produced.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.integration.conftest import Prepared

from ravel.domain.enums import FailureClass, JobState, NodeStatus, NodeType
from ravel.domain.execution import BackendJob
from ravel.domain.reconciliation import (
    RunFailureClass,
    RunReconciliation,
    WorkflowLiveness,
)
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import WORKER_RUN_NODE_TYPES
from ravel.execution.reconcile import ExecutionReconciler, WorkflowProbe
from ravel.execution.temporal.worker import workflow_id_for
from ravel.state.database import Database
from ravel.state.repositories.base import ProjectScopeError
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.reconciliation import RunReconciliationRepository
from ravel.state.repositories.records import BackendJobRepository, RecordRepositories

pytestmark = pytest.mark.integration


@dataclass
class ScriptedProbe:
    """Temporal, answering from a field.

    Records what it was asked, because half of what these tests assert is that
    the reconciler did *not* ask — a node that is running, or one that started
    a moment ago, must not reach the probe at all.
    """

    answer: WorkflowLiveness = WorkflowLiveness.NOT_FOUND
    asked: list[str] = field(default_factory=list)

    async def liveness(self, workflow_id: str) -> WorkflowLiveness:
        self.asked.append(workflow_id)
        return self.answer


@dataclass
class RacingProbe:
    """A probe that gives somebody else the time to answer first.

    The gap between asking Temporal and writing the recovery is a network call
    wide, and a competing writer that runs *before* the sweep starts is a
    different case entirely — it is filtered out by the read that finds
    candidates. Standing in the gap is the only way to test what the write
    transaction re-reads, and doing it from inside `liveness` puts the write
    where it happens in a deployment.
    """

    answer: WorkflowLiveness
    during: Callable[[], None]

    async def liveness(self, workflow_id: str) -> WorkflowLiveness:
        self.during()
        return self.answer


@dataclass(frozen=True)
class Lost:
    """A node with a run under way, and the identifiers of that run."""

    prepared: Prepared
    version: int
    workflow_id: str
    job_id: str | None

    @property
    def node_id(self) -> str:
        return self.prepared.node_id

    @property
    def project_id(self) -> str:
        return self.prepared.project_id


def start_a_run(
    database: Database,
    prepared: Prepared,
    *,
    job_state: JobState | None = JobState.RUNNING,
    node_status: NodeStatus = NodeStatus.RUNNING,
) -> Lost:
    """Put a node into a run, the way the start path does.

    `job_state=None` is a run that died before `start_job` recorded anything:
    the node is RUNNING and no job row exists, which happens when the planning
    activity — the first one that can fail — never returned.
    """
    with database.transaction() as session:
        dag = DagRepository(session, prepared.project_id)
        dag.transition_node(prepared.node_id, NodeStatus.RUNNING, actor_id="compute-worker")
        if node_status is not NodeStatus.RUNNING:
            dag.transition_node(prepared.node_id, node_status, actor_id="compute-worker")
        version = ExecutionContractRepository(session, prepared.project_id).for_node(
            prepared.node_id
        ).version
        job_id: str | None = None
        if job_state is not None:
            jobs = BackendJobRepository(session, prepared.project_id)
            job = jobs.start(
                BackendJob(
                    project_id=prepared.project_id,
                    node_id=prepared.node_id,
                    attempt=1,
                    execution_contract_ref=prepared.contract.contract_id,
                    execution_contract_version=version,
                    backend="mock-compute",
                )
            )
            job_id = job.job_id
            if job_state is not JobState.SUBMITTED:
                jobs.record_state(job_id, job_state, backend_state=job_state.value)
    return Lost(
        prepared=prepared,
        version=version,
        workflow_id=workflow_id_for(prepared.node_id, version),
        job_id=job_id,
    )


def a_reconciler(database: Database, probe: WorkflowProbe) -> ExecutionReconciler:
    """The reconciler as the tests run it: scripted Temporal, and no grace.

    The grace window protects a run that started a moment ago, and a test that
    had to wait thirty seconds for every case would be testing the clock. The
    test that is *about* the window builds its own reconciler instead.
    """
    return ExecutionReconciler(database=database, probe=probe, grace_seconds=0.0)


def read_state(database: Database, lost: Lost) -> tuple[NodeStatus, JobState | None]:
    """The node's status and its job's, read back from PostgreSQL."""
    with database.read_only() as session:
        node = DagRepository(session, lost.project_id).node(lost.node_id)
        job = (
            BackendJobRepository(session, lost.project_id).get(job_id=lost.job_id)
            if lost.job_id is not None
            else None
        )
        return node.status, job.state if job is not None else None


def reconciliations_for(database: Database, lost: Lost) -> list[RunReconciliation]:
    """Every reconciliation this node has, read from PostgreSQL."""
    with database.read_only() as session:
        return RunReconciliationRepository(session, lost.project_id).for_node(lost.node_id)


def event_types(database: Database, project_id: str) -> list[str]:
    """Every event this project announced, in order."""
    with database.read_only() as session:
        return list(
            session.execute(
                text(
                    "SELECT event_type FROM project_events "
                    "WHERE project_id = :project ORDER BY seq"
                ),
                {"project": project_id},
            ).scalars()
        )


def a_reconciliation(lost: Lost, *, version: int) -> RunReconciliation:
    """A reconciliation record for one run, built the way the sweep builds one."""
    return RunReconciliation(
        project_id=lost.project_id,
        node_id=lost.node_id,
        execution_contract_version=version,
        workflow_id=workflow_id_for(lost.node_id, version),
        observed=WorkflowLiveness.FAILED,
        failure_class=RunFailureClass.INFRASTRUCTURE,
        node_status_before=NodeStatus.RUNNING,
        node_status_after=NodeStatus.WAITING_DECISION,
    )


# ── The recovery itself ─────────────────────────────────────────────────────


async def test_a_dead_run_stops_stranding_its_node(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """L-24's case, arranged and then recovered.

    The workflow exhausted the retries of its last activity and Temporal says
    FAILED. Before this phase the node stayed RUNNING forever, and the project
    could not reach an ending while it held one. What must be true afterwards:
    the node is out of the live statuses, Master has something waiting, and the
    job is not left claiming to be in flight.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.FAILED)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert [record.node_id for record in written] == [lost.node_id]
    record = written[0]
    assert record.observed is WorkflowLiveness.FAILED
    assert record.failure_class is RunFailureClass.INFRASTRUCTURE
    assert record.node_status_before is NodeStatus.RUNNING
    assert record.node_status_after is NodeStatus.WAITING_DECISION
    assert record.execution_contract_version == lost.version
    assert record.workflow_id == lost.workflow_id
    assert record.job_state_before is JobState.RUNNING
    assert record.job_state_after is JobState.FAILED
    assert record.detected_by == "execution-reconciler"

    assert read_state(database, lost) == (NodeStatus.WAITING_DECISION, JobState.FAILED)


async def test_the_node_is_not_told_the_science_failed(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """What the recovery is forbidden to write, asserted as an absence.

    Three writes would each have been a claim RAVEL is not entitled to make: an
    Execution Record (a Worker's act), a Review Record (Review's), and a
    Decision Record (Master's). The stream is checked too, because FAILED is
    reachable from RUNNING and using it would have been the easy way out — one
    transition, no Master, and a broken queue recorded as a failed experiment.

    The node already carries the pre-run clearance Review submitted to let it
    enter RUNNING, so the review assertion is that the pile does not grow: a
    node whose run vanished has nothing for Review to measure, and the record
    that would say otherwise is a verdict on an empty result.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.FAILED)
    with database.read_only() as session:
        cleared_by = RecordRepositories(session, lost.project_id).reviews.for_node(
            lost.node_id
        )

    await a_reconciler(database, probe).reconcile([lost.project_id])

    with database.read_only() as session:
        records = RecordRepositories(session, lost.project_id)
        assert records.executions.for_node(lost.node_id) == []
        assert records.reviews.for_node(lost.node_id) == cleared_by
        assert records.decisions.all() == []

    stream = event_types(database, lost.project_id)
    assert "NODE_FAILED" not in stream
    assert stream[-1] == "NODE_WAITING"


async def test_the_job_is_failed_for_infrastructure_and_not_for_science(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The job's own class, which is what a retry policy would read.

    `INFRA_RETRYABLE` is the vocabulary's word for exactly this — "a scheduler
    lost the job" — and it is the class that says nothing was learned. A
    `NON_RETRYABLE` here would tell the next reader that running it again would
    fail the same way, which is a statement about the work rather than about
    the run RAVEL lost.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.NOT_FOUND)

    await a_reconciler(database, probe).reconcile([lost.project_id])

    with database.read_only() as session:
        job = BackendJobRepository(session, lost.project_id).get(job_id=lost.job_id)
    assert job.state is JobState.FAILED
    assert job.failure_class is FailureClass.INFRA_RETRYABLE
    assert "gone" in job.detail


async def test_the_workflow_it_asks_about_is_the_one_the_start_path_would_make(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The id is derived the same way on both sides, or the probe asks about nothing.

    A reconciler that guessed a different id would read `NOT_FOUND` for a run
    that is executing perfectly well — and would do it silently, since
    `NOT_FOUND` is exactly what a genuinely lost run looks like.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.RUNNING)

    await a_reconciler(database, probe).reconcile([lost.project_id])

    assert probe.asked == [workflow_id_for(lost.node_id, lost.version)]


async def test_a_lost_run_with_no_job_is_still_recovered(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """A run that died before its first job was recorded.

    `start_job` is the second activity, and a workflow can fail before it ever
    runs. The node is RUNNING, the job table has nothing for it, and the
    recovery has to work without one — the record says `None` for the job
    rather than pointing a reader at a row that does not exist.
    """
    lost = start_a_run(database, prepare(), job_state=None)
    probe = ScriptedProbe(answer=WorkflowLiveness.NOT_FOUND)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert len(written) == 1
    assert written[0].job_id is None
    assert written[0].job_state_before is None
    assert written[0].job_state_after is None
    assert written[0].failure_class is RunFailureClass.WORKFLOW_LOST
    assert read_state(database, lost) == (NodeStatus.WAITING_DECISION, None)


async def test_a_run_that_was_stopped_is_a_cancellation_and_not_a_failure(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """Somebody ended the run, and the job is cancelled rather than failed.

    A job that did not fail carries no failure class, by constraint — only a
    failure has a reason — so this is also the case that would break if the
    reconciler wrote `INFRA_RETRYABLE` onto every job it ended.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.TERMINATED)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert written[0].failure_class is RunFailureClass.CANCELLED
    with database.read_only() as session:
        job = BackendJobRepository(session, lost.project_id).get(job_id=lost.job_id)
    assert job.state is JobState.CANCELLED
    assert job.failure_class is None


async def test_a_run_that_finished_unconsumed_is_recovered_too(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The workflow reached its ending and nobody wrote it down.

    Temporal says COMPLETED and PostgreSQL says the node is RUNNING, and the
    pair cannot both be true: whichever activity wrote the ending wrote the
    Execution Record and moved the node in the same transaction, so a node
    still in a live status means the process died between the workflow's last
    step and the write. A supervisor that crashed in that window leaves the run
    finished and the node live forever, which is L-24 with the evidence
    pointing the other way.

    Two things this case is here to pin. The classification is RAVEL's own
    failure, because nothing was learned about the science — and a COMPLETED
    workflow is not that finding either, since the record of what it produced
    is exactly what is missing. And no Execution Record is written, which is
    the tempting mistake here: the work *did* finish, so a reconciler that
    wrote one would be attesting to a result nobody ever read, on a Worker's
    behalf, from a backend that was never asked to collect anything.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.COMPLETED)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert len(written) == 1
    assert written[0].observed is WorkflowLiveness.COMPLETED
    assert written[0].failure_class is RunFailureClass.INFRASTRUCTURE
    assert read_state(database, lost) == (NodeStatus.WAITING_DECISION, JobState.FAILED)
    with database.read_only() as session:
        records = RecordRepositories(session, lost.project_id)
        assert records.executions.for_node(lost.node_id) == []


async def test_a_waiter_is_brought_back_before_master_is_asked(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The DAG's own rule: a wait ends by resuming.

    `WAITING_EXTERNAL` has no edge to `WAITING_DECISION`, and inventing one
    would skip the step where the run decided what it now had. So the recovery
    is two transitions, and the record says where the node was rather than only
    where it went.
    """
    lost = start_a_run(
        database,
        prepare(),
        job_state=JobState.WAITING_EXTERNAL,
        node_status=NodeStatus.WAITING_EXTERNAL,
    )
    probe = ScriptedProbe(answer=WorkflowLiveness.TERMINATED)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert written[0].node_status_before is NodeStatus.WAITING_EXTERNAL
    assert written[0].node_status_after is NodeStatus.WAITING_DECISION
    assert read_state(database, lost) == (NodeStatus.WAITING_DECISION, JobState.CANCELLED)
    assert event_types(database, lost.project_id)[-2:] == ["NODE_STARTED", "NODE_WAITING"]


# ── What it refuses to touch ────────────────────────────────────────────────


async def test_a_run_that_is_still_going_is_left_alone(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The commonest case, and the one that must cost nothing.

    A probe that answered RUNNING for a live run and got a recovery anyway
    would end every run in the deployment on its first tick.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.RUNNING)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert written == []
    assert read_state(database, lost) == (NodeStatus.RUNNING, JobState.RUNNING)
    assert reconciliations_for(database, lost) == []


async def test_a_frontend_that_cannot_be_reached_is_not_evidence(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """`UNKNOWN` means the probe established nothing, so nothing is recovered.

    This is the difference between a probe that was told the run is gone and
    one that could not ask. Reading the second as the first would end healthy
    runs during an outage — and end them in the way that is hardest to notice,
    because the node would have moved for a reason nobody could reproduce.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.UNKNOWN)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert probe.asked == [lost.workflow_id]
    assert written == []
    assert read_state(database, lost) == (NodeStatus.RUNNING, JobState.RUNNING)


async def test_a_node_that_has_left_the_live_statuses_is_not_asked_about(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """Review has it, so no run is holding it and there is nothing to recover.

    The candidate set is read from the node's status, which is why a node
    waiting on Review never reaches the probe — no workflow's loss could strand
    a node whose next move belongs to a seat.
    """
    lost = start_a_run(database, prepare(), node_status=NodeStatus.REVIEWING)
    probe = ScriptedProbe(answer=WorkflowLiveness.NOT_FOUND)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert probe.asked == []
    assert written == []


async def test_a_run_that_started_moments_ago_is_not_questioned(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The grace window, and the one mistake it exists to prevent.

    Between a node entering RUNNING and its workflow being findable there is a
    window in which a probe can only be wrong. The reconciler is therefore
    built to ask again rather than to act quickly, and this is what asking
    again costs: one node, one tick.

    The window is measured from `started_at` — when the DAG says the run began
    — and not from when the row was written, which is the same moment for a
    fixture and hours apart for a node that has been running all afternoon.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.NOT_FOUND)
    reconciler = ExecutionReconciler(database=database, probe=probe)

    assert await reconciler.reconcile([lost.project_id]) == []
    assert probe.asked == []

    with database.transaction() as session:
        session.execute(
            text(
                "UPDATE dag_nodes SET started_at = started_at - :age WHERE node_id = :node"
            ),
            {"age": timedelta(hours=1), "node": lost.node_id},
        )

    assert len(await reconciler.reconcile([lost.project_id])) == 1
    assert probe.asked == [lost.workflow_id]


async def test_a_node_answered_meanwhile_is_left_alone(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """Between the probe and the write, somebody else may have answered.

    The probe is a network call and the write is a transaction, so the two are
    not one moment. A node Master cancelled in between has had its question
    answered, and recovering it would be RAVEL editing a decision that has
    already been made — which is why everything is re-read inside the write
    transaction rather than trusted from the read that found it.
    """
    lost = start_a_run(database, prepare())

    def master_cancels_it() -> None:
        with database.transaction() as session:
            DagRepository(session, lost.project_id).cancel_node(
                lost.node_id, role=AgentRole.MASTER, decision_ref="dec-cancelled"
            )

    probe = RacingProbe(answer=WorkflowLiveness.NOT_FOUND, during=master_cancels_it)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert written == []
    assert reconciliations_for(database, lost) == []
    assert read_state(database, lost)[0] is NodeStatus.CANCELLED


async def test_a_project_with_nothing_running_is_not_asked_about_at_all(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """Nothing live means no probe, which means no Temporal connection.

    Worth asserting rather than assuming: the supervisor reconciles on every
    tick of every deployment, and a sweep that opened a client in order to
    discover there was nothing to ask would put a Temporal connection in the
    control plane's steady state for no reason. `_fallback` is where that
    client would live, and a reconciler that never needed one has none.
    """
    prepare()
    probe = ScriptedProbe(answer=WorkflowLiveness.NOT_FOUND)
    reconciler = a_reconciler(database, probe)

    assert await reconciler.reconcile([]) == []
    assert await reconciler.reconcile(["no-such-project"]) == []
    assert probe.asked == []
    assert reconciler._fallback is None


# ── Idempotency ─────────────────────────────────────────────────────────────


async def test_scanning_twice_finds_the_loss_once(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The property a timer-driven sweep lives or dies by.

    The second scan is the same scan: the same read, the same probe answer. It
    must not write a second row, must not end the job again, must not move the
    node again, and must not put a second set of events in the stream. The
    node's own status is the first guard — it is no longer live, so it is not a
    candidate — and the constraint is the second, for two sweeps at once.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.FAILED)
    reconciler = a_reconciler(database, probe)

    first = await reconciler.reconcile([lost.project_id])
    stream_after_first = event_types(database, lost.project_id)
    second = await reconciler.reconcile([lost.project_id])

    assert len(first) == 1
    assert second == []
    assert len(reconciliations_for(database, lost)) == 1
    assert event_types(database, lost.project_id) == stream_after_first
    assert read_state(database, lost) == (NodeStatus.WAITING_DECISION, JobState.FAILED)


async def test_two_sweeps_at_once_still_write_one_record(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The constraint, exercised directly rather than through the scan.

    Two supervisors is a deployment mistake rather than an impossible state —
    nothing in the database says only one may run — and both would read the
    same live node. What makes that safe is not the candidate set, which both
    sweeps see identically, but the unique key: one insert wins and the other
    returns the row the winner wrote.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.FAILED)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])
    assert len(written) == 1

    with database.transaction() as session:
        again = RunReconciliationRepository(session, lost.project_id).record(
            written[0].model_copy(update={"detail": "a second sweep"})
        )

    assert again.reconciliation_id == written[0].reconciliation_id
    assert again.detail == written[0].detail
    assert len(reconciliations_for(database, lost)) == 1


async def test_a_second_loss_under_revised_terms_is_its_own_record(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The key is the run, not the node.

    Master answering a lost run by revising the contract opens a second run
    under a new version. If that one is lost too, collapsing it into the first
    record would hide the second loss behind the first recovery — and a
    revision is exactly the act that says the work is still meant to happen.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.FAILED)
    await a_reconciler(database, probe).reconcile([lost.project_id])

    with database.transaction() as session:
        second = RunReconciliationRepository(session, lost.project_id).record(
            a_reconciliation(lost, version=lost.version + 1)
        )

    assert second.execution_contract_version == lost.version + 1
    assert len(reconciliations_for(database, lost)) == 2


# ── Scope, storage, and node type ───────────────────────────────────────────


async def test_a_reconciliation_belongs_to_its_project(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """The reconciler writes about one project and cannot write about another.

    The repository's scope is what refuses the row, not the record's
    `project_id`, for the reason every write in this project is built that way:
    a supplied identifier is a claim, and the session already knows which
    project it is serving.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.FAILED)
    await a_reconciler(database, probe).reconcile([lost.project_id])
    elsewhere = a_reconciliation(lost, version=lost.version + 1)

    with pytest.raises(ProjectScopeError), database.transaction() as session:
        RunReconciliationRepository(session, "someone-elses-project").record(elsewhere)


async def test_the_reconciliation_is_append_only(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """A record of what RAVEL saw cannot be edited after it saw it.

    The guard triggers are installed from the metadata, so the table was made
    read-only by not appearing in `guards.UPDATABLE_TABLES` — and this is the
    assertion that the omission did what it was for, in the same shape the
    guard suite states it for every other record.
    """
    lost = start_a_run(database, prepare())
    probe = ScriptedProbe(answer=WorkflowLiveness.FAILED)
    await a_reconciler(database, probe).reconcile([lost.project_id])

    for statement in (
        "UPDATE run_reconciliations SET failure_class = 'SCIENTIFIC'",
        "DELETE FROM run_reconciliations",
    ):
        with pytest.raises(Exception, match="append-only"), database.transaction() as session:
            session.execute(text(statement))


@pytest.mark.parametrize("node_type", sorted(WORKER_RUN_NODE_TYPES))
async def test_recovery_does_not_depend_on_which_worker_run_it_is(
    database: Database, prepare: Callable[..., Prepared], node_type: NodeType
) -> None:
    """Both node types a Worker runs are recovered by exactly the same rule.

    COMPUTATION and EXPERIMENT differ in their criteria, their backend and the
    work behind them, and none of that is what this layer reads. What they
    share is the only thing it does read: each is executed as a durable run, so
    each has a workflow that can be lost.
    """
    lost = start_a_run(database, prepare(node_type=node_type))
    probe = ScriptedProbe(answer=WorkflowLiveness.NOT_FOUND)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert len(written) == 1
    assert written[0].failure_class is RunFailureClass.WORKFLOW_LOST
    assert read_state(database, lost)[0] is NodeStatus.WAITING_DECISION


async def test_a_node_no_worker_runs_is_never_asked_about(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """A RESEARCH node in RUNNING has no workflow, so NOT_FOUND says nothing.

    The live five-agent certification is where this came from: three research
    nodes were parked in `WAITING_DECISION` in the middle of their searches,
    each with a reconciliation saying its run had been lost. No run existed to
    lose. A Research Agent does that work inside its own turn — it never calls
    `start_execution`, and no activity of a run ever moved the node — so the
    probe was asked about a workflow id that nothing had started, and answered
    correctly that it was not there.

    This case had it backwards until it met a real project: an earlier version
    of it asserted that a RESEARCH node *was* recovered, on the reasoning that
    the reconciler reads only the durable layer and not what kind of work the
    node asked for. The reasoning was right and the conclusion was wrong. Which
    nodes have a run is exactly what the durable layer knows, and a node type
    performed by an agent has none.

    Asserted twice, because those are two different claims: nothing was
    written, and the probe was never asked. The second is the one that would
    have caught this before the live run — the reconciler had no business
    forming the question.
    """
    lost = start_a_run(
        database,
        prepare(node_type=NodeType.RESEARCH, with_acceptance=False),
        job_state=None,
    )
    probe = ScriptedProbe(answer=WorkflowLiveness.NOT_FOUND)

    written = await a_reconciler(database, probe).reconcile([lost.project_id])

    assert written == []
    assert probe.asked == []
    assert read_state(database, lost) == (NodeStatus.RUNNING, None)
