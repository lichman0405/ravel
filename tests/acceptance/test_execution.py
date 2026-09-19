"""A06, A07, A13, A16, A17: what happens to work once it leaves Master.

Five items about the same run of the machine. A06 is the ordinary case — work is
handed to a backend and comes back complete. A07 and A13 are the two ways it does
not: a failure, and a delivery short of what was owed. A16 asks whether any of it
survives the worker dying. A17 asks whether two branches can run at once and be
joined by a rule rather than by luck.

The backends are V0's mocks and are labelled as such everywhere they appear —
`SIMULATED_KIND` on the artifact row, the warning inside the bytes. What is not
simulated is everything around them: PostgreSQL, Temporal, a real worker in this
process, MinIO, and the frozen contracts a node runs under. `acceptance/
MOCK_SCENARIOS.yaml` decides what each scenario does, and these tests read the
catalogue rather than restating it, so a change to it shows up here instead of
passing quietly.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.e2e.conftest import Headless, Task
from tests.integration.conftest import Prepared
from tests.integration.temporal.conftest import RunningWorker, await_state

from ravel.backends import catalogue
from ravel.domain.artifacts import SIMULATED_KIND
from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    CompletenessVerdict,
    FailureClass,
    FailurePolicy,
    JobState,
    JoinPolicy,
    NodeStatus,
    NodeType,
    ProjectOutcome,
    ProjectStatus,
    ReviewOutcome,
    TerminationStatus,
)
from ravel.domain.roles import AgentRole
from ravel.execution.temporal.contracts import ExternalResult
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.records import BackendJobRepository, RecordRepositories
from ravel.state.repositories.research import ArtifactRepository

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

#: How long a node run may take before a test calls it wedged. Generous,
#: because what it waits for is a real worker polling a real Temporal.
RUN_TIMEOUT = timedelta(seconds=180)

#: The lab scenario that waits for the world rather than for a duration, and
#: what its contract requires. Both name the same scenario the catalogue does.
LONG_WAIT = "LAB_LONG_WAIT"
LAB_OUTPUTS = ("experiment_log", "raw_data")

ACTOR = "experimental-worker"

#: What the scripted Master and Reviewer are asked for. Phrases rather than
#: identifiers, because identifiers are RAVEL's to assign.
MEASURE = "Measure conductivity across the dopant series."
MEASURE_AGAIN = "Measure the conductivity of a second batch."
ANALYSE = "Fit the conductivity model to the measurements."


def node_status(database: Database, node_id: str) -> str:
    """A node's status, read from PostgreSQL rather than from a return value."""
    with database.read_only() as session:
        return str(
            session.execute(
                text("SELECT status FROM dag_nodes WHERE node_id = :node"),
                {"node": node_id},
            ).scalar_one()
        )


def status_changes(database: Database, project_id: str) -> list[tuple[str, str]]:
    """Every node status change this project announced, in the order it did.

    Read from the event stream, because A17's claim is about *when* each node
    moved relative to the others and a node's current status cannot say that.
    """
    with database.read_only() as session:
        rows = session.execute(
            text(
                "SELECT payload ->> 'node_id' AS node_id, payload ->> 'status' AS status "
                "FROM project_events WHERE project_id = :project "
                "AND payload ? 'node_id' AND payload ? 'from' ORDER BY seq"
            ),
            {"project": project_id},
        ).all()
    return [(str(node_id), str(status)) for node_id, status in rows]


def build(
    headless: Headless,
    objective: str,
    *,
    node_type: NodeType = NodeType.COMPUTATION,
    depends_on: tuple[DagNode, ...] = (),
    join_policy: JoinPolicy | None = None,
    failure_policy: FailurePolicy | None = None,
) -> DagNode:
    """One node, built the way the domain builds one."""
    return DagNode.create(
        project_id=headless.project.project_id,
        node_type=node_type,
        objective=objective,
        created_by=AgentRole.MASTER.value,
        dependencies=tuple(node.node_id for node in depends_on),
        join_policy=join_policy,
        failure_policy=failure_policy,
    )


def task(headless: Headless, objective: str, **terms: object) -> Task:
    """A piece of work for the scripted Master to plan."""
    return Task(build=lambda: build(headless, objective), **terms)  # type: ignore[arg-type]


# ── A06 ─────────────────────────────────────────────────────────────────────


async def test_a06_a_compute_run_delivers_a_complete_record_and_its_artifacts(
    headless: Headless,
) -> None:
    """A06: "Mock Compute Worker executes a successful task and submits complete
    ExecutionRecord/artifacts."

    *Complete* is asserted twice and differently, because it means two things.
    The record is complete: a termination status, every attempt that was made,
    the contract version the work ran under, and a completeness check naming what
    was required and what arrived. The delivery is complete: one artifact per
    required output, and the references on the record are the rows in the
    database — a record claiming four outputs while three artifacts exist is
    exactly the discrepancy a Worker must not be able to produce.

    The artifact is checked for its simulated marking here rather than only in
    the extra-gates file, because this is the test a reader would otherwise take
    as evidence that RAVEL measured something.
    """
    headless.compute("COMPUTE_SUCCESS")
    master = headless.master((task(headless, MEASURE),))

    run = await headless.drive(master, headless.review())

    assert run.finished, f"the loop halted at {run.status.value}"
    assert run.status is ProjectStatus.COMPLETED
    node = master.planned[0]
    assert node_status(headless.database, node.node_id) == NodeStatus.PASSED.value

    with headless.database.read_only() as session:
        execution = RecordRepositories(session, master.project_id).latest_execution(
            node.node_id
        )
        jobs = BackendJobRepository(session, master.project_id).for_node(node.node_id)
        artifacts = ArtifactRepository(session, master.project_id).all()

    assert execution is not None, "a node passed with no Execution Record behind it"
    assert execution.termination_status is TerminationStatus.COMPLETED
    assert execution.backend.startswith("mock"), (
        f"the record names backend {execution.backend!r}; V0's only Compute "
        "Worker is a mock, and a record that did not say so would read as a "
        "real measurement"
    )
    assert execution.execution_contract_version == 1
    assert execution.attempt_count == 1, "a successful run is not retried"
    assert execution.attempts[0].backend_job_ref, "an attempt that names no backend job"

    completeness = execution.completeness
    assert completeness.verdict is CompletenessVerdict.COMPLETE
    assert set(completeness.delivered_outputs) == set(completeness.required_outputs)
    assert completeness.missing_outputs == ()

    assert len(execution.output_refs) == len(completeness.required_outputs), (
        "the record has to reference every output it says it delivered"
    )
    assert set(execution.output_refs) == {artifact.artifact_id for artifact in artifacts}
    assert {artifact.name for artifact in artifacts} == set(completeness.required_outputs)
    for artifact in artifacts:
        assert artifact.kind == SIMULATED_KIND, (
            f"{artifact.name!r} came out of a mock without being marked simulated"
        )

    assert [(job.attempt, job.state) for job in jobs] == [(1, JobState.COMPLETED)]


# ── A07 ─────────────────────────────────────────────────────────────────────


async def test_a07_a_retryable_failure_is_retried_inside_the_frozen_contract(
    headless: Headless,
) -> None:
    """A07's retry half: "Worker follows allowed retry/escalation; no
    unauthorized scientific parameter change."

    The second clause is the one worth testing and the harder one to test. It is
    not enough that the retry happened; what has to hold is that it happened
    under *the same terms* — the same contract version, the same permitted
    actions. A Worker that answered an infrastructure failure by widening its
    own contract would be doing science on its own authority, and the record
    would still look like an ordinary success.

    So the contract is read back and asserted to be the only version that
    exists, and the two job rows are asserted to name one contract reference.
    The retry is a second attempt, not a second revision.
    """
    scenario = catalogue().compute_scenario("COMPUTE_RETRYABLE_INFRA_FAILURE")
    assert scenario.requires_contract_retry_permission, "the catalogue changed meaning"

    headless.compute("COMPUTE_RETRYABLE_INFRA_FAILURE")
    master = headless.master((task(headless, MEASURE, allowed_retries=1),))

    run = await headless.drive(master, headless.review())

    assert run.finished, f"the loop halted at {run.status.value}"
    node = master.planned[0]
    assert node_status(headless.database, node.node_id) == NodeStatus.PASSED.value

    with headless.database.read_only() as session:
        execution = RecordRepositories(session, master.project_id).latest_execution(
            node.node_id
        )
        jobs = BackendJobRepository(session, master.project_id).for_node(node.node_id)
        contract = ExecutionContractRepository(session, master.project_id).for_node(
            node.node_id
        )

    assert execution is not None
    assert execution.attempt_count == 2, (
        "the scenario fails the first attempt and completes the second; a run "
        f"with {execution.attempt_count} attempts played a different scenario"
    )

    assert [(job.attempt, job.state) for job in jobs] == [
        (1, JobState.FAILED),
        (2, JobState.COMPLETED),
    ]
    failed, retried = jobs
    assert failed.failure_class is FailureClass.INFRA_RETRYABLE, (
        "an INFRA_RETRYABLE failure is the only kind a Worker may retry without "
        "asking, and the class is what says so"
    )
    assert retried.failure_class is None

    assert {job.execution_contract_ref for job in jobs} == {contract.contract_id}, (
        "the retry ran under a different contract from the attempt it retried"
    )
    assert {job.execution_contract_version for job in jobs} == {contract.version}
    assert contract.version == execution.execution_contract_version, (
        "a retry wrote a new contract version, which is a revision and not a retry"
    )


async def test_a07_a_scientific_failure_is_not_retried_and_ends_as_failed(
    headless: Headless,
) -> None:
    """A07's escalation half, and the line between the two failure classes.

    A scientific failure is the work being wrong rather than the infrastructure
    being unavailable, and retrying it would be repeating an experiment that has
    already answered. The contract here allows three retries and the Worker takes
    none of them, which is what makes this a statement about the failure class
    rather than about the allowance.
    """
    headless.compute("COMPUTE_SCIENTIFIC_FAILURE")
    master = headless.master(
        (task(headless, MEASURE, allowed_retries=3),), outcome=ProjectOutcome.FAILED
    )

    run = await headless.drive(master, headless.review())

    assert run.finished, f"the loop halted at {run.status.value}"
    node = master.planned[0]
    assert node_status(headless.database, node.node_id) == NodeStatus.FAILED.value

    with headless.database.read_only() as session:
        execution = RecordRepositories(session, master.project_id).latest_execution(
            node.node_id
        )
        jobs = BackendJobRepository(session, master.project_id).for_node(node.node_id)

    assert execution is not None
    assert execution.termination_status is TerminationStatus.FAILED
    assert execution.attempt_count == 1, (
        "a NON_RETRYABLE failure was retried; the contract permitted three, and "
        "the permission is not the reason to use one"
    )
    assert [(job.attempt, job.state, job.failure_class) for job in jobs] == [
        (1, JobState.FAILED, FailureClass.NON_RETRYABLE)
    ]


# ── A13 ─────────────────────────────────────────────────────────────────────


async def test_a13_a_short_delivery_is_recorded_as_incomplete_and_not_as_a_result(
    headless: Headless,
) -> None:
    """A13: "Worker detects missing required artifact, requests it or produces
    INCOMPLETE_DELIVERY; Review is not given a false complete result."

    The scenario completes and delivers less than the contract required, which is
    the case that matters: nothing failed, so a system that only checked for
    failure would pass it. What stops that is the completeness check, asserted
    where a reviewer would read it — the Execution Record names what was required
    and what is missing, so the gap is visible to whoever judges the work rather
    than only to whoever ran it.

    The verdict follows the record rather than the run. The scripted Reviewer is
    told to fail this node, and the assertion is that it *can*: everything needed
    to reach that conclusion is in PostgreSQL, and the run's own termination
    status would have said COMPLETED.
    """
    headless.compute("COMPUTE_MISSING_OUTPUT")
    master = headless.master((task(headless, MEASURE),), outcome=ProjectOutcome.FAILED)
    review = headless.review({MEASURE: ReviewOutcome.FAIL})

    run = await headless.drive(master, review)

    assert run.finished, f"the loop halted at {run.status.value}"
    node = master.planned[0]

    with headless.database.read_only() as session:
        execution = RecordRepositories(session, master.project_id).latest_execution(
            node.node_id
        )
        artifacts = ArtifactRepository(session, master.project_id).all()

    assert execution is not None
    assert execution.termination_status is TerminationStatus.COMPLETED, (
        "the run itself completed; what is wrong is what it handed over, and a "
        "test where the job failed would be testing A07 instead"
    )
    completeness = execution.completeness
    assert completeness.verdict is CompletenessVerdict.INCOMPLETE_DELIVERY
    assert completeness.missing_outputs, (
        "an INCOMPLETE_DELIVERY verdict has to name what is missing, or nobody "
        "can ask for it"
    )
    assert set(completeness.missing_outputs) <= set(completeness.required_outputs)

    delivered = {artifact.name for artifact in artifacts}
    assert delivered == set(completeness.delivered_outputs)
    assert delivered < set(completeness.required_outputs), (
        "the scenario delivered everything the contract required, so there is "
        "nothing for the completeness check to have found"
    )
    assert not set(completeness.missing_outputs) & delivered

    # And what a short delivery comes to once Review has seen it: a FAIL, so
    # nothing downstream is entitled to treat this node's output as a result.
    assert node_status(headless.database, node.node_id) == NodeStatus.FAILED.value


# ── A16 ─────────────────────────────────────────────────────────────────────


async def test_a16_a_waiting_task_survives_the_worker_being_killed(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """A16: "Restart runtime/Temporal workers while a task waits. Waiting task
    resumes without lost authoritative state."

    The worker is *killed* rather than stopped — `RunningWorker.kill` cancels the
    task, which is what a machine dying does, and it abandons whatever activity
    was in flight without unwinding it. A graceful stop would drain first, and a
    test that drained would not be testing recovery.

    What has to survive is not the process but the state: the node is in
    WAITING_EXTERNAL in PostgreSQL, the backend job is in the same state, and the
    run is a durable wait inside the workflow. A second worker, started fresh
    against the same database, picks the run back up. Then the signal arrives
    from outside, exactly as A10 says it does, and the run finishes under the
    terms it started with.

    Driven at the node-run level rather than through the loop, because the loop
    runs to completion in one call and this test has to act in the middle.
    """
    headless.lab(LONG_WAIT)
    prepared = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)

    await headless.client.start_node_run(
        project_id=headless.project.project_id,
        node_id=prepared.node_id,
        actor_id=ACTOR,
        execution_contract_version=prepared.contract.version,
    )
    def waiting() -> bool:
        return (
            node_status(headless.database, prepared.node_id)
            == NodeStatus.WAITING_EXTERNAL.value
        )

    await await_state(waiting)

    with headless.database.read_only() as session:
        jobs = BackendJobRepository(session, headless.project.project_id).for_node(
            prepared.node_id
        )
    assert [(job.state, job.attempt) for job in jobs] == [(JobState.WAITING_EXTERNAL, 1)]

    # The machine dies, and a new one starts in its place.
    await headless.worker.kill()
    headless.worker = await RunningWorker.start(
        headless.settings, headless.registry, headless.database
    )

    # Nothing moved while there was no worker, because nothing was in memory:
    # the state a restart resumes from is the state PostgreSQL holds.
    assert node_status(headless.database, prepared.node_id) == NodeStatus.WAITING_EXTERNAL.value
    with headless.database.read_only() as session:
        after_restart = BackendJobRepository(
            session, headless.project.project_id
        ).for_node(prepared.node_id)
    assert len(after_restart) == 1, "the restart submitted the work a second time"
    assert after_restart[0].job_id == jobs[0].job_id

    await headless.client.deliver_external_result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
        result=ExternalResult(
            summary="The operator confirmed the run.",
            delivered_outputs=LAB_OUTPUTS,
        ),
    )
    outcome = await headless.client.result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
        timeout=RUN_TIMEOUT,
    )

    assert outcome.termination_status is TerminationStatus.COMPLETED
    assert outcome.completeness is CompletenessVerdict.COMPLETE
    assert node_status(headless.database, prepared.node_id) == NodeStatus.REVIEWING.value

    with headless.database.read_only() as session:
        execution = RecordRepositories(session, headless.project.project_id).latest_execution(
            prepared.node_id
        )
    assert execution is not None, "the run ended without a record, after a restart"
    assert execution.execution_contract_version == prepared.contract.version


# ── A17 ─────────────────────────────────────────────────────────────────────


async def test_a17_two_branches_run_and_an_all_join_waits_for_both(
    headless: Headless,
) -> None:
    """A17: "Run at least two parallel branches and demonstrate ALL/ANY or
    threshold join behavior."

    The two branches are independent, so nothing orders them; the join depends on
    both, so it cannot start until both have passed. That they really ran
    alongside each other is asserted from the event stream rather than hoped for,
    and the shape of the assertion is the definition: each branch was claimed
    before the *other* one finished. A version of this plan that ran the branches
    in sequence would fail it, which is what makes it a test of parallelism
    rather than of two nodes existing.

    The join's own record is the second half. When it runs, it runs once — a
    fan-in that fired twice would be two joins wearing one node's identifier.
    """
    headless.compute("COMPUTE_SUCCESS")
    project_id = headless.project.project_id
    first = build(headless, MEASURE)
    second = build(headless, MEASURE_AGAIN)
    join = build(
        headless,
        ANALYSE,
        depends_on=(first, second),
        join_policy=JoinPolicy.ALL,
        failure_policy=FailurePolicy.BLOCK,
    )
    master = headless.master(
        (Task(build=lambda: first), Task(build=lambda: second), Task(build=lambda: join))
    )

    run = await headless.drive(master, headless.review())

    assert run.finished, f"the loop halted at {run.status.value}"
    for node in (first, second, join):
        assert node_status(headless.database, node.node_id) == NodeStatus.PASSED.value, (
            f"{node.display_id} did not pass, so the join above it proves nothing"
        )

    order = status_changes(headless.database, project_id)
    claimed: dict[str, int] = {}
    finished: dict[str, int] = {}
    for index, (node_id, status) in enumerate(order):
        if status == "RUNNING":
            claimed.setdefault(node_id, index)
        elif status == "PASSED":
            finished.setdefault(node_id, index)

    assert claimed[first.node_id] < finished[second.node_id], (
        "the second branch finished before the first was claimed, so the two ran "
        "one after the other"
    )
    assert claimed[second.node_id] < finished[first.node_id]

    assert claimed[join.node_id] > finished[first.node_id]
    assert claimed[join.node_id] > finished[second.node_id], (
        "an ALL join started before both of its dependencies had passed"
    )

    with headless.database.read_only() as session:
        executions = RecordRepositories(session, project_id).executions.for_node(join.node_id)
    assert len(executions) == 1, (
        f"the join ran {len(executions)} times; a fan-in that fires twice is not a join"
    )
    assert executions[0].termination_status is TerminationStatus.COMPLETED


async def test_a17_an_all_join_does_not_fire_against_a_failed_branch(
    headless: Headless,
) -> None:
    """The other half of ALL, and the reason to state the policy at all.

    With both branches failed and the join's failure policy set to BLOCK, the
    join is never promoted — it is not failed, it is work that can no longer
    happen, and the difference matters because a failed node is a result to
    review while an unreachable one is a plan that stopped making sense.

    The control is the test above: the same shape with both branches passing does
    promote the join. Without it, this would be satisfied by a join that never
    fires at all.

    Master's answer here is a replan, and that is the system's own answer to a
    fan-in that cannot fire: a BLOCKED node is not an ending, so
    `MasterService.conclude` refuses while one exists, and the project cannot
    end until Master retires it. `replan_after_failure` does exactly that, which
    is why the join ends CANCELLED rather than sitting blocked forever.
    """
    headless.compute("COMPUTE_SCIENTIFIC_FAILURE")
    headless.lab("LAB_SUCCESS")
    # One branch answers and one does not, which is the situation that tells ALL
    # apart from CONTINUE: the fan-in is short of exactly one result.
    first = build(headless, MEASURE, node_type=NodeType.EXPERIMENT)
    second = build(headless, MEASURE_AGAIN)
    join = build(
        headless,
        ANALYSE,
        depends_on=(first, second),
        join_policy=JoinPolicy.ALL,
        failure_policy=FailurePolicy.BLOCK,
    )
    replacement = build(
        headless, "Measure the series on the bench.", node_type=NodeType.EXPERIMENT
    )

    master = headless.master(
        (Task(build=lambda: first), Task(build=lambda: second), Task(build=lambda: join)),
        replan=lambda _failed: (Task(build=lambda: replacement),),
    )

    run = await headless.drive(master, headless.review())

    assert run.finished, f"the loop halted at {run.status.value}"
    assert node_status(headless.database, first.node_id) == NodeStatus.PASSED.value
    assert node_status(headless.database, second.node_id) == NodeStatus.FAILED.value

    with headless.database.read_only() as session:
        joined = RecordRepositories(session, master.project_id).executions.for_node(join.node_id)
        jobs = BackendJobRepository(session, master.project_id).for_node(join.node_id)

    assert joined == [], "an ALL join fired against branches that failed"
    assert jobs == [], "an ALL join was handed to a backend against failed branches"
    assert "RUNNING" not in [status for node_id, status in status_changes(
        headless.database, master.project_id
    ) if node_id == join.node_id], "the join was promoted despite the failure policy"

    assert node_status(headless.database, join.node_id) == NodeStatus.CANCELLED.value, (
        "a join that can no longer fire is retired by the replan; leaving it "
        "BLOCKED would leave the project unable to conclude at all"
    )
