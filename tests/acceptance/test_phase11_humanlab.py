"""P11-06: the bench channel, from Master's contract to the record Master reads.

The directive names this chain in full, and this module is the one place it is
walked as one thing:

    Master → Experiment Contract → LabPreparation → Experimental Worker →
    HumanLabBackend → LAB_USER → result / deviation → Review → Master

Three suites already cover its parts. `tests/unit/preparation/test_lab.py` says
what the package holds, `tests/integration/backends/test_lab_backend.py` says
what the backend does through its own port, and
`tests/integration/gateway/test_lab_handover.py` says what the person's upload
door refuses. What none of them says is what happens when a *run* is on the
other side: a real workflow, a real durable wait, a real signal, and a seat that
reads the ending afterwards. A backend can hold every one of its promises and a
workflow can still lose the answer, because between them sit the two things
this phase was built on — Temporal's durability and PostgreSQL's authority —
and only a run exercises both.

**Nothing here is a mock.** The node is prepared by the real materializer
through the real activities, the package a person is handed is the one RAVEL
wrote, the upload goes through the same function the Gateway's route calls, and
the delivery is the real signal to a real workflow. What is scripted is the
*seats*: no model is asked anything, because what this item is about is the
channel rather than what a Master would decide.

The three claims are the item's own, and the second is the one the directive
states in words: **不能上传一个任意文件就自动完成。必须根据 required_outputs
检查。** A file that answers nothing is refused at the door (the gateway suite's
subject), and a delivery that does not cover what was owed does not finish the
run — which is this suite's, because "the run is still waiting" is a fact about
a workflow rather than about a backend.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.acceptance.phase10_support import seat_scope, seat_tool
from tests.e2e.conftest import Headless
from tests.integration.conftest import Prepared
from tests.integration.temporal.conftest import await_state
from tests.support.lab import (
    LOG_BYTES,
    OUTPUTS,
    RAW_BYTES,
    UPLOADER,
    prepare_for_the_bench,
    raise_a_deviation,
    start_and_wait,
    upload,
)

from ravel.backends.lab import HumanLabBackend
from ravel.domain.enums import CompletenessVerdict, JobState, NodeStatus
from ravel.domain.execution import BackendJob, ExecutionRecord
from ravel.domain.lab import LabHandover
from ravel.domain.roles import AgentRole
from ravel.execution.temporal.contracts import ExternalResult
from ravel.state.database import Database
from ravel.state.repositories.lab import LabHandoverRepository
from ravel.state.repositories.records import RecordRepositories
from ravel.state.store import S3ArtifactStore

pytestmark = [pytest.mark.phase11, pytest.mark.acceptance]

#: The role whose contract this run executes. A string because that is what
#: `RunInput.actor_id` carries everywhere else.
ACTOR = "experimental-worker"

#: The two files a bench delivers, keyed by the output each answers.
DELIVERED = {OUTPUTS[0]: LOG_BYTES, OUTPUTS[1]: RAW_BYTES}


def handover_of(database: Database, prepared: Prepared) -> LabHandover:
    """The durable record of the handover, as it stands now."""
    with database.read_only() as session:
        found = LabHandoverRepository(session, prepared.project_id).for_attempt(
            prepared.node_id, 1, prepared.contract.version
        )
    assert found is not None, "the run handed work to a bench and recorded nothing"
    return found


def jobs_of(database: Database, prepared: Prepared) -> list[BackendJob]:
    """The backend job rows for this node, oldest attempt first."""
    with database.read_only() as session:
        return list(
            RecordRepositories(session, prepared.project_id).jobs.for_node(
                prepared.node_id
            )
        )


def executions_of(database: Database, prepared: Prepared) -> list[ExecutionRecord]:
    """Every execution recorded for this node, oldest first."""
    with database.read_only() as session:
        return list(
            RecordRepositories(session, prepared.project_id).executions.for_node(
                prepared.node_id
            )
        )


async def deliver(
    headless: Headless,
    prepared: Prepared,
    *,
    summary: str,
    outputs: tuple[str, ...],
    detail: str = "",
    deviation_id: str | None = None,
) -> None:
    """Tell the waiting run that something happened at the bench.

    `deviation_id` is how a report rather than a delivery travels: the bench
    raises the record through the Gateway and names it in the payload, so the
    Worker stops the run *against that record* instead of raising a second one
    for the same sentence. Both shapes below are the same signal, which is the
    point — a run cannot tell what a delivery means until it reads it.
    """
    await headless.client.deliver_external_result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
        result=ExternalResult(
            summary=summary,
            detail=detail,
            delivered_outputs=outputs,
            payload={"deviation_id": deviation_id} if deviation_id else {},
        ),
    )


# ── The chain ─────────────────────────────────────────────────────────────────


async def test_p11_06_masters_contract_reaches_a_bench_and_comes_back_as_a_record(
    headless: Headless,
    prepare: Callable[..., Prepared],
    database: Database,
    artifact_store: S3ArtifactStore,
) -> None:
    """The whole chain, once, with every step the production one.

    What the case asserts at each end is what makes it the *chain* rather than
    a list of parts. Before the person answers: the node and its job are both
    waiting, nothing has been executed, and the handover names the package
    RAVEL built and what the bench owes. After they answer: the run ends as
    completed, the artifacts are the bytes they uploaded attributed to them,
    the Execution Record names the bench backend and says the delivery was
    complete — and Master's own read of the project shows the work has arrived
    and is waiting on a verdict.
    """
    prepared = prepare_for_the_bench(headless, prepare)
    await start_and_wait(headless, prepared)

    handover = handover_of(database, prepared)
    assert handover.required_outputs == OUTPUTS, (
        "the handover does not owe what the contract required, so nothing "
        "downstream can be comparing against the right names"
    )
    assert handover.preparation_id, "the bench was given a package RAVEL did not record"
    assert handover.state is JobState.WAITING_EXTERNAL
    assert [job.state for job in jobs_of(database, prepared)] == [
        JobState.WAITING_EXTERNAL
    ], "RAVEL reported a bench as running, which it cannot see"
    assert executions_of(database, prepared) == [], (
        "a run that has produced nothing already has an Execution Record"
    )

    arrived = []
    for output, body in DELIVERED.items():
        artifact, version = upload(database, artifact_store, handover, output, body)
        arrived.append((artifact, version))
    assert [artifact.created_by for artifact, _version in arrived] == [UPLOADER] * 2, (
        "the record does not say who uploaded what the bench produced"
    )

    await deliver(
        headless, prepared, summary="The bench ran both samples.", outputs=OUTPUTS
    )
    outcome = await headless.client.result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
    )

    assert outcome.completeness is CompletenessVerdict.COMPLETE
    assert headless.status_of(prepared.node) is NodeStatus.REVIEWING, (
        "the delivery arrived and the node did not go to the seat that judges it"
    )

    (execution,) = executions_of(database, prepared)
    assert execution.backend == HumanLabBackend.name
    assert execution.delivery_is_complete
    assert set(execution.output_refs) == {
        artifact.artifact_id for artifact, _version in arrived
    }, "the Execution Record does not reference what the bench delivered"

    # And Master's read of the project — the tool the seat actually calls —
    # shows the work sitting where the chain left it.
    state = await read_master_state(headless, prepared)
    assert state["dag"]["at_a_glance"]["REVIEWING"] == 1
    # `.get`, because a status nobody is in is left out of the tally rather
    # than reported as zero — which is the stronger statement of the two: the
    # bench's wait is over, and the projection has stopped counting it at all.
    assert state["dag"]["at_a_glance"].get("WAITING_EXTERNAL", 0) == 0


async def read_master_state(headless: Headless, prepared: Prepared) -> dict:
    """What a Master session reads, through the role's own tool.

    The seat's own context and the registered handler, so what this returns is
    what a Master would be handed — a projection assembled by the tool rather
    than a query this case wrote to look like one.
    """
    context = seat_scope(headless.database, prepared.project_id, AgentRole.MASTER)
    return await seat_tool("read_project_state", context)()


# ── What finishes a run ───────────────────────────────────────────────────────


async def test_p11_06_a_delivery_that_does_not_cover_what_is_owed_does_not_finish_it(
    headless: Headless,
    prepare: Callable[..., Prepared],
    database: Database,
    artifact_store: S3ArtifactStore,
) -> None:
    """A partial delivery leaves the run waiting, and the record says what for.

    The gate is `required_outputs` and it is checked against what is *recorded*:
    the delivery below claims both outputs arrived, and one of them did not. A
    backend that believed the claim would finish this run, and the file the
    bench has not sent yet would never be asked for again — which is the failure
    the directive names, one step further along than the upload door's own
    refusal.

    The wait is asserted after the delivery has been *processed* rather than
    after a sleep: a partial delivery writes the missing names to the handover,
    so waiting for that note is waiting for the fact rather than for a clock.
    """
    prepared = prepare_for_the_bench(headless, prepare)
    await start_and_wait(headless, prepared)
    handover = handover_of(database, prepared)

    upload(database, artifact_store, handover, OUTPUTS[0], LOG_BYTES)
    await deliver(
        headless,
        prepared,
        summary="The bench sent the log and will send the raw data tomorrow.",
        outputs=OUTPUTS,
    )
    await await_state(lambda: "still owed" in handover_of(database, prepared).detail)

    # Nothing finished, and nothing was given up on.
    assert headless.status_of(prepared.node) is NodeStatus.WAITING_EXTERNAL
    assert [job.state for job in jobs_of(database, prepared)] == [
        JobState.WAITING_EXTERNAL
    ]
    assert executions_of(database, prepared) == []
    waiting = handover_of(database, prepared)
    assert OUTPUTS[1] in waiting.detail, (
        "the record says something is still owed without saying which output"
    )

    # And the second delivery is what ends it, which is what makes the first
    # one a wait rather than a failure.
    upload(database, artifact_store, waiting, OUTPUTS[1], RAW_BYTES)
    await deliver(
        headless, prepared, summary="The raw data arrived.", outputs=(OUTPUTS[1],)
    )
    outcome = await headless.client.result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
    )
    assert outcome.completeness is CompletenessVerdict.COMPLETE


async def test_p11_06_a_bench_that_reports_a_deviation_stops_the_run_for_master(
    headless: Headless,
    prepare: Callable[..., Prepared],
    database: Database,
) -> None:
    """The other thing a bench can send, and it is Master's to answer.

    A deviation is not a delivery with a problem in it: it is a report that the
    work as asked for cannot be done, and the run stops rather than trying again
    — a second attempt would put the same request to a bench that has already
    said no. Where it stops is `WAITING_DECISION`, because whether the contract
    permits what was asked is a question about the contract, and the seat that
    may change one is Master.
    """
    prepared = prepare_for_the_bench(headless, prepare)
    await start_and_wait(headless, prepared)

    # The report is recorded first, the way the Gateway records it: the route a
    # bench reports through writes the deviation and the delivery only *names*
    # it. Naming one that does not exist is a delivery, not a report — which is
    # what the backend suite pins, and why the identifier is not invented here.
    deviation = raise_a_deviation(database, prepared)
    await deliver(
        headless,
        prepared,
        summary="The furnace would not hold 900C.",
        outputs=(),
        detail="Asked to run at 900C; the instrument stops at 850C.",
        deviation_id=deviation.deviation_id,
    )
    outcome = await headless.client.result(
        node_id=prepared.node_id,
        execution_contract_version=prepared.contract.version,
    )

    assert outcome.deviation_id, "the report reached the run and left no trace"
    assert headless.status_of(prepared.node) is NodeStatus.WAITING_DECISION, (
        "the bench reported a deviation and the node was not parked for Master"
    )
    (execution,) = executions_of(database, prepared)
    assert execution.deviations == (outcome.deviation_id,), (
        "the Execution Record does not reference the deviation that ended it"
    )
    assert not execution.delivery_is_complete, (
        "a run stopped by a deviation reported itself as a complete delivery"
    )
