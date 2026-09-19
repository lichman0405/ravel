"""A mock scenario that ends at a node status, run for real.

Three of the ten scenarios end somewhere a backend's own port cannot show: the
*catalogue* says what the Worker must end up doing, and what it says is a
`NodeStatus` or a `WorkerMessageKind`. Those are facts about the project, so
they can only be observed by running a node.

These tests therefore start a real Temporal worker in this process, exactly as
`tests/integration/temporal/test_node_run.py` does, and hand the node to the
real `MockLabBackend`. What is being tested is the whole chain that Phase 6
added: the mock reports a deviation on a poll, `check_job` finds it, the
contract the job ran under is asked whether it permits the thing, the work is
stopped, a `WorkerMessage` is recorded, and the node is parked where only Master
can move it.

**The expectations are read from the catalogue** — `expected_worker_state`,
`expected_action` — and never written out here. A test that hard-coded
`WAITING_DECISION` would keep passing if the acceptance file changed its mind
about what A11 requires, which is the drift this whole arrangement exists to
prevent.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.integration.temporal.conftest import (
    RunningWorker,
    await_state,
)

from ravel.backends import catalogue
from ravel.domain.enums import (
    CompletenessVerdict,
    NodeStatus,
    NodeType,
    TerminationStatus,
    WorkerMessageKind,
)
from ravel.execution.temporal.contracts import ExternalResult
from ravel.state.database import Database

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

ACTOR = "experimental-worker"

#: A run that is waiting on a lab answers when the lab does, so the wait here is
#: for the workflow to finish rather than for anything to time out; the number
#: only has to be larger than the mock's own.
RESULT_TIMEOUT = timedelta(seconds=60)

#: The scenarios whose report the contract does not permit — which is what makes
#: them deviations rather than ordinary work. Read from the catalogue so that a
#: scenario that started permitting its own substitution would be noticed.
DEVIATION_SCENARIOS = tuple(
    sorted(
        scenario_id
        for scenario_id, scenario in catalogue().experiment.items()
        if scenario.report is not None and not scenario.substitution_allowed
    )
)


def _node_status(database: Database, node_id: str) -> str:
    with database.read_only() as session:
        return session.execute(
            text("SELECT status FROM dag_nodes WHERE node_id = :n"), {"n": node_id}
        ).scalar_one()


def _messages(database: Database, node_id: str) -> list[tuple[str, str]]:
    with database.read_only() as session:
        rows = session.execute(
            text(
                "SELECT kind, body FROM worker_messages WHERE node_id = :n "
                "ORDER BY sent_at"
            ),
            {"n": node_id},
        ).all()
    return [(kind, body) for kind, body in rows]


def _deviations(database: Database, node_id: str) -> list[tuple[str, bool, str]]:
    with database.read_only() as session:
        rows = session.execute(
            text(
                "SELECT requested_action, permitted, raised_by FROM deviation_records "
                "WHERE node_id = :n ORDER BY raised_at"
            ),
            {"n": node_id},
        ).all()
    return [(action, bool(permitted), raised) for action, permitted, raised in rows]


def _job_states(database: Database, node_id: str) -> list[str]:
    with database.read_only() as session:
        return list(
            session.execute(
                text("SELECT state FROM backend_jobs WHERE node_id = :n ORDER BY attempt"),
                {"n": node_id},
            ).scalars()
        )


def _expected_parking(scenario_id: str) -> NodeStatus:
    """Where the catalogue says a reporting scenario leaves the node.

    It says so in two spellings, and only one of them is a `NodeStatus`:
    `LAB_DEVIATION_PRESSURE` names the state, `LAB_OPERATOR_QUESTION_UNDEFINED`
    names the *action* (`ESCALATE`). Those are not two expectations — an
    escalation is a Worker asking a question only Master may answer, which is
    what `WAITING_DECISION` says the node is doing — so the action spelling is
    read as the state it implies rather than duplicated as a literal in a test.

    The action has to be `ESCALATE` for that reading to hold. A scenario that
    named some other action and no state would say the Worker must do something
    without saying where that leaves the node, and guessing would hide the gap.
    """
    scenario = catalogue().lab_scenario(scenario_id)
    if scenario.expected_action:
        kind = WorkerMessageKind(scenario.expected_action)
        assert kind is WorkerMessageKind.ESCALATE, (
            f"{scenario_id} expects the Worker to {kind.value} and does not say "
            "where the node ends up; only an escalation parks it for Master"
        )
    if scenario.expected_worker_state:
        return NodeStatus(scenario.expected_worker_state)
    return NodeStatus.WAITING_DECISION


# ── A11: the report the Worker may not answer ───────────────────────────────


@pytest.mark.parametrize("scenario_id", DEVIATION_SCENARIOS)
async def test_a_lab_report_the_contract_does_not_permit_parks_the_node_for_master(
    database: Database,
    project,
    prepare,
    temporal_registry,
    client,
    scenario_id: str,
) -> None:
    """A11, end to end.

    The Worker pauses and escalates, and it does not answer scientifically
    itself: the assertion that carries that is the *absence* of anything the
    Worker decided. There is no Execution Record, no delivered output, and no
    attempt at a second run — only a deviation, a message asking Master, and a
    node waiting.

    `WAITING_DECISION` rather than `REVIEWING`, because the two say different
    things: REVIEWING asks "was this work good", and this node has no work to
    judge yet. It is waiting for a person to rule on what it was allowed to do.
    """
    scenario = catalogue().lab_scenario(scenario_id)
    expected_status = _expected_parking(scenario_id)
    node = prepare(
        node_type=NodeType.EXPERIMENT,
        required_outputs=("experiment_log", "raw_data"),
    )

    worker = await RunningWorker.start(
        client.settings, temporal_registry(scenario_id), database
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id, node_id=node.node_id, actor_id=ACTOR
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.DEVIATION
    assert outcome.deviation_id is not None, (
        "the outcome does not name the deviation that ended the run, so nothing "
        "reading only the outcome can find what stopped it"
    )

    raised = _deviations(database, node.node_id)
    assert len(raised) == 1
    action, permitted, by = raised[0]
    assert not permitted, "a deviation is by definition something not permitted"
    assert by.startswith("backend:"), (
        "the record has to say who raised it, and it was the lab that reported it"
    )
    assert scenario.report is not None
    assert action == scenario.report.requested_action, (
        "the record has to name the thing the lab was asking to do, in the lab's "
        "own terms; a paraphrase would not be matchable against a contract"
    )
    if scenario.report.parameter:
        assert scenario.report.parameter in action

    messages = _messages(database, node.node_id)
    assert len(messages) == 1, f"the Worker said {len(messages)} things; expected one"
    kind, body = messages[0]
    assert kind == WorkerMessageKind.ESCALATE.value
    assert "Master" in body, (
        "an escalation that does not say who must answer it is not an escalation"
    )

    assert _node_status(database, node.node_id) == expected_status.value
    assert _job_states(database, node.node_id) == ["CANCELLED"], (
        "the lab is still holding the question; leaving the work running would "
        "let it act on an instruction nobody permitted"
    )


async def test_a_deviation_does_not_consume_a_retry(
    database: Database,
    project,
    prepare,
    temporal_registry,
    client,
) -> None:
    """No second attempt, even when the contract would have allowed one.

    A retry would put the same request to a backend that has already said it is
    outside the contract. The run stops because RAVEL has been told it does not
    have the permission it asked for, and asking again is not a way to get it.
    """
    scenario_id = "LAB_DEVIATION_PRESSURE"
    node = prepare(
        node_type=NodeType.EXPERIMENT,
        allowed_retries=5,
        required_outputs=("experiment_log", "raw_data"),
    )

    worker = await RunningWorker.start(
        client.settings, temporal_registry(scenario_id), database
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id, node_id=node.node_id, actor_id=ACTOR
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert len(outcome.attempts) == 1, (
        "a deviation is not a failure to retry; it is a question only Master can "
        "answer"
    )
    assert "deviation" in outcome.retry_reason
    assert _job_states(database, node.node_id) == ["CANCELLED"]


# ── A13: the delivery that does not complete ────────────────────────────────


async def test_a_partial_delivery_sends_the_node_to_review_with_a_message(
    database: Database,
    project,
    prepare,
    temporal_registry,
    client,
) -> None:
    """A13, end to end: the gap is recorded, not smoothed over.

    The node goes to Review — the work did run and did produce something, so
    Review is the right reader for it — and it goes with a WorkerMessage asking
    for what is missing. What must *not* happen is Review being handed a
    complete-looking result, which is what `INCOMPLETE_DELIVERY` is for.
    """
    scenario = catalogue().lab_scenario("LAB_MISSING_RAW_DATA")
    assert scenario.expected_action, "the catalogue does not say what is expected"
    node = prepare(
        node_type=NodeType.EXPERIMENT,
        required_outputs=("experiment_log", "raw_data"),
    )

    worker = await RunningWorker.start(
        client.settings, temporal_registry("LAB_MISSING_RAW_DATA"), database
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id, node_id=node.node_id, actor_id=ACTOR
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.COMPLETED
    assert outcome.completeness is CompletenessVerdict.INCOMPLETE_DELIVERY
    assert outcome.missing_outputs == ("raw_data",), (
        "which output is missing has to come from the contract, not from the lab"
    )
    assert outcome.delivered_outputs == tuple(scenario.delivered_outputs)

    messages = _messages(database, node.node_id)
    assert [kind for kind, _ in messages] == [scenario.expected_action]
    assert WorkerMessageKind(scenario.expected_action) is (
        WorkerMessageKind.REQUEST_MISSING_INFORMATION
    )
    assert "raw_data" in messages[0][1]

    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value
    assert _deviations(database, node.node_id) == [], (
        "a missing file is not a deviation; it is an incomplete delivery, and "
        "the two routes lead to different places"
    )

async def test_a_complete_lab_delivery_sends_the_node_to_review_in_silence(
    database: Database,
    project,
    prepare,
    temporal_registry,
    client,
) -> None:
    """The control for the test above: nothing missing means nothing said.

    Without this, "the Worker asked for the missing output" and "the Worker
    always asks for something" would look the same from the assertions.
    """
    node = prepare(
        node_type=NodeType.EXPERIMENT,
        required_outputs=("experiment_log", "raw_data"),
    )

    worker = await RunningWorker.start(
        client.settings, temporal_registry("LAB_SUCCESS"), database
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id, node_id=node.node_id, actor_id=ACTOR
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.completeness is CompletenessVerdict.COMPLETE
    assert outcome.missing_outputs == ()
    assert _messages(database, node.node_id) == []
    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value


async def test_a_lab_that_waits_resumes_from_the_signal_and_finishes(
    database: Database,
    project,
    prepare,
    temporal_registry,
    client,
) -> None:
    """A10, through the mock rather than through a scripted double.

    The lab reaches WAITING_EXTERNAL and stays there — it is not waiting out a
    duration — and the node follows it. The signal is what ends the wait, and
    what the signal carried is what the lab then delivers.
    """
    node = prepare(
        node_type=NodeType.EXPERIMENT,
        required_outputs=("experiment_log", "raw_data"),
    )

    worker = await RunningWorker.start(
        client.settings, temporal_registry("LAB_LONG_WAIT"), database
    )
    try:
        await client.start_node_run(
            project_id=project.project_id, node_id=node.node_id, actor_id=ACTOR
        )
        await await_state(
            lambda: _node_status(database, node.node_id) == NodeStatus.WAITING_EXTERNAL.value
        )
        assert _job_states(database, node.node_id) == ["WAITING_EXTERNAL"]

        await client.deliver_external_result(
            node_id=node.node_id,
            result=ExternalResult(
                summary="The operator confirmed the run.",
                delivered_outputs=("experiment_log", "raw_data"),
            ),
        )
        outcome = await client.result(node_id=node.node_id, timeout=RESULT_TIMEOUT)
    finally:
        await worker.stop_gracefully()

    assert outcome.termination_status is TerminationStatus.COMPLETED
    assert outcome.completeness is CompletenessVerdict.COMPLETE
    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value
