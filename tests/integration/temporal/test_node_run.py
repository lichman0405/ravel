"""Node runs against a real Temporal, killed and restarted.

The gate for this phase is one sentence — *kill the worker mid-activity,
restart, and observe the project reach the same state a clean run reaches* —
and everything here is arranged around being able to say it honestly.

Three things make that possible, and each is doing real work:

- **The worker runs in this process**, so a test can kill it at a moment it
  chooses rather than at a moment a subprocess happens to die.
- **Recovery is bounded by `job_activity_timeout_seconds`**, shortened here to
  seconds. Temporal cannot distinguish a dead worker from a slow one, so a
  killed activity is not retried until its `start_to_close` expires; the test
  waits that long on purpose, because that wait is what a deployment would
  serve.
- **The assertions are made against PostgreSQL, read after the workflows are
  gone.** What survived is what is in the database. A test that read the
  workflow's return value would be asserting that Temporal remembered, which
  was never in doubt.

The backend is scripted, so "the same state" is a comparison of two runs of the
same script rather than of two runs of a probabilistic one.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text

# Imported by their full paths rather than relatively: these test directories
# are namespace packages, so `from .conftest import ...` has no parent package
# to resolve against. `tests/live_research/conftest.py` reaches the integration
# fixtures the same way.
from tests.integration.temporal.conftest import (
    FIRST_CONTRACT_VERSION,
    REQUIRED_OUTPUT,
    RunningWorker,
    await_state,
)

from ravel.domain.enums import (
    CompletenessVerdict,
    FailureClass,
    JobState,
    NodeStatus,
    TerminationStatus,
)
from ravel.execution.temporal.client import NodeRunClient
from ravel.execution.temporal.contracts import ExternalResult, RunOutcome
from ravel.state.database import Database

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

ACTOR = "compute-worker"


@pytest.fixture
async def client(execution_settings, temporal_unreachable) -> NodeRunClient:
    return await NodeRunClient.connect(execution_settings)


async def _await(predicate, *, timeout: float = 30.0) -> None:
    """The shared wait, under the name this module's tests were written with."""
    await await_state(predicate, timeout=timeout)


def _run(database: Database, node_id: str):
    """The node's authoritative state: its status and its execution record."""
    with database.read_only() as session:
        status = session.execute(
            text("SELECT status FROM dag_nodes WHERE node_id = :n"), {"n": node_id}
        ).scalar_one()
        records = session.execute(
            text(
                "SELECT termination_status FROM execution_records WHERE node_id = :n "
                "ORDER BY created_at"
            ),
            {"n": node_id},
        ).scalars()
    return status, list(records)


def _node_status(database: Database, node_id: str) -> str:
    with database.read_only() as session:
        return session.execute(
            text("SELECT status FROM dag_nodes WHERE node_id = :n"), {"n": node_id}
        ).scalar_one()


def _job_states(database: Database, node_id: str) -> list[str]:
    with database.read_only() as session:
        return list(
            session.execute(
                text(
                    "SELECT state FROM backend_jobs WHERE node_id = :n ORDER BY attempt"
                ),
                {"n": node_id},
            ).scalars()
        )


# ── The run itself ──────────────────────────────────────────────────────────


async def test_a_run_that_succeeds_hands_its_node_to_review(
    database: Database, project, runnable_node, backend, registry, client
) -> None:
    node = runnable_node()
    backend.states = [JobState.RUNNING, JobState.COMPLETED]

    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.COMPLETED
    assert outcome.completeness is CompletenessVerdict.COMPLETE
    assert outcome.delivered_outputs == (REQUIRED_OUTPUT,)
    assert _run(database, node.node_id) == ("REVIEWING", ["COMPLETED"])


async def test_the_node_follows_its_job_into_an_external_wait(
    database: Database, project, runnable_node, backend, registry, client
) -> None:
    """A10, as the node sees it.

    The job is handed to a lab, the lab has not answered, and the node says so.
    WAITING_EXTERNAL is a report rather than a decision — the run is telling the
    DAG what it is doing — which is why no Decision Record is involved.
    """
    node = runnable_node()
    backend.states = [JobState.WAITING_EXTERNAL]

    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        await _await(
            lambda: _node_status(database, node.node_id) == NodeStatus.WAITING_EXTERNAL.value
        )

        await client.deliver_external_result(
            node_id=node.node_id,
            execution_contract_version=FIRST_CONTRACT_VERSION,
            result=ExternalResult(
                summary="The lab reported the conductivity series.",
                delivered_outputs=(REQUIRED_OUTPUT,),
            ),
        )
        outcome = await client.result(
            node_id=node.node_id,
            execution_contract_version=FIRST_CONTRACT_VERSION,
            timeout=timedelta(seconds=30),
        )
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.COMPLETED
    assert backend.deliveries, "the answer never reached the backend"
    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value


async def test_a_retryable_failure_runs_a_second_attempt_when_the_contract_allows(
    database: Database, project, runnable_node, backend, registry, client
) -> None:
    """Retrying is a new attempt, a new job, and two lines in the record."""
    node = runnable_node(allowed_retries=1)
    # Attempt one fails infrastructurally; attempt two succeeds. Both scripts
    # are set before the run starts, because the run retries as fast as the poll
    # interval allows and there is no moment in between to write one down.
    backend.states = [JobState.RUNNING, JobState.FAILED]
    backend.scripts = {2: [JobState.COMPLETED]}
    backend.failure_class = FailureClass.INFRA_RETRYABLE

    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.COMPLETED
    assert [attempt.attempt for attempt in outcome.attempts] == [1, 2]
    assert _job_states(database, node.node_id) == ["FAILED", "COMPLETED"]


async def test_a_retryable_failure_is_not_retried_without_permission(
    database: Database, project, runnable_node, backend, registry, client
) -> None:
    """The contract's `allowed_retries` is a grant, and zero is a real answer."""
    node = runnable_node(allowed_retries=0)
    backend.states = [JobState.RUNNING, JobState.FAILED]
    backend.failure_class = FailureClass.INFRA_RETRYABLE

    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.FAILED
    assert len(outcome.attempts) == 1
    assert "the contract allows 0 retries" in outcome.retry_reason
    # The node still goes to Review. A Worker reports; Review decides what the
    # failure means, and moving the node to FAILED here would be the Worker
    # judging its own work.
    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value


async def test_a_scientific_failure_is_never_retried(
    database: Database, project, runnable_node, backend, registry, client
) -> None:
    node = runnable_node(allowed_retries=3)
    backend.states = [JobState.RUNNING, JobState.FAILED]
    backend.failure_class = FailureClass.NON_RETRYABLE

    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.FAILED
    assert len(outcome.attempts) == 1
    assert "non-retryable" in outcome.retry_reason


def _clean_outcome(outcome: RunOutcome) -> tuple:
    """The part of an outcome two runs must agree on.

    Identifiers and timestamps are generated per run, so comparing them would
    only prove the runs were different. What has to match is what the project
    now says about the node.
    """
    return (
        outcome.termination_status,
        outcome.completeness,
        outcome.delivered_outputs,
        outcome.missing_outputs,
        tuple(attempt.attempt for attempt in outcome.attempts),
    )


# ── The gate: a worker that dies mid-run ────────────────────────────────────


async def test_killing_the_worker_mid_run_reaches_the_same_state_as_a_clean_run(
    database: Database, project, runnable_node, backend, registry, client
) -> None:
    """A16, and the phase's gate.

    The comparison is deliberate: two runs of the same script, one of which has
    its worker killed while an activity is in flight, must leave the project
    saying the same thing. "It recovered" is not testable; "it recovered to the
    same place" is.
    """
    clean_node = runnable_node()
    backend.states = [JobState.RUNNING, JobState.COMPLETED]
    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=clean_node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        clean = await handle.result()
    finally:
        await worker.stop_gracefully()

    # The same script, run again, with the worker killed the moment the job is
    # under way — after `start_job` committed, while `check_job` is polling.
    killed_node = runnable_node()
    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=killed_node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        await _await(lambda: _job_states(database, killed_node.node_id) != [])
        await worker.kill()

        replacement = await RunningWorker.start(client.settings, registry, database)
        try:
            recovered = await handle.result()
        finally:
            await replacement.stop_gracefully()
    finally:
        await worker.kill()

    assert _clean_outcome(recovered) == _clean_outcome(clean)
    assert _run(database, killed_node.node_id) == _run(database, clean_node.node_id)


async def test_a_worker_killed_while_a_run_waits_loses_nothing_authoritative(
    database: Database, project, runnable_node, backend, registry, client
) -> None:
    """A16 for the wait that lasts hours rather than seconds.

    The run is blocked in a durable timer with no worker alive anywhere. The
    work is not lost because the workflow is in Temporal's history and the job
    is in PostgreSQL, and the signal that ends the wait is delivered to the
    restarted worker rather than to the dead one.
    """
    node = runnable_node()
    backend.states = [JobState.WAITING_EXTERNAL]

    worker = await RunningWorker.start(client.settings, registry, database)
    try:
        await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        await _await(
            lambda: _node_status(database, node.node_id) == NodeStatus.WAITING_EXTERNAL.value
        )
        await worker.kill()
    finally:
        await worker.kill()

    # Nothing is running. The waiting job is still a row in PostgreSQL, and it
    # is what the replacement worker will find rather than start again.
    assert _job_states(database, node.node_id) == ["WAITING_EXTERNAL"]
    submitted_before = backend.submit_entries

    replacement = await RunningWorker.start(client.settings, registry, database)
    try:
        await client.deliver_external_result(
            node_id=node.node_id,
            execution_contract_version=FIRST_CONTRACT_VERSION,
            result=ExternalResult(
                summary="The lab reported the conductivity series at last.",
                delivered_outputs=(REQUIRED_OUTPUT,),
            ),
        )
        outcome = await client.result(
            node_id=node.node_id,
            execution_contract_version=FIRST_CONTRACT_VERSION,
            timeout=timedelta(seconds=30),
        )
    finally:
        await replacement.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.COMPLETED
    assert backend.submit_entries == submitted_before, (
        "the recovery submitted the work again instead of finding the job it "
        "already had"
    )
    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value
