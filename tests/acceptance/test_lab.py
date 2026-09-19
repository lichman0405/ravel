"""A10, A11, A12: what a bench does when the world is slower or wider than the plan.

Three items about the one agent that works in a room rather than in a process.
A10 is a task that waits for something outside RAVEL and is woken by it. A11 is a
bench that is asked for something the contract does not permit — the case the
whole Execution/Decision split exists for. A12 is Master's answer to it.

The expectations come from `acceptance/MOCK_SCENARIOS.yaml` rather than from
this file: which scenarios wait, which report, what state each leaves the Worker
in. A test that hard-coded `WAITING_DECISION` would keep passing if the
acceptance file changed its mind, which is the drift this arrangement exists to
prevent.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta

import pytest
from sqlalchemy import text
from tests.e2e.conftest import Headless, Task
from tests.integration.conftest import Prepared
from tests.integration.temporal.conftest import await_state

from ravel.backends import catalogue
from ravel.domain.contracts import ExecutionContract
from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    CompletenessVerdict,
    DecisionType,
    JobState,
    NodeStatus,
    NodeType,
    ProjectOutcome,
    ProjectStatus,
    TerminationStatus,
    WorkerMessageKind,
)
from ravel.domain.roles import AgentRole
from ravel.execution.temporal.contracts import ExternalResult
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.records import RecordRepositories

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

#: How long a lab run may take before a test calls it wedged.
RUN_TIMEOUT = timedelta(seconds=180)

#: The two scenarios these items are about, named as the catalogue names them.
LONG_WAIT = "LAB_LONG_WAIT"
PRESSURE = "LAB_DEVIATION_PRESSURE"

#: What a lab task's contract requires. Two outputs, so a lab that sends one is
#: visibly short of what it owed.
LAB_OUTPUTS = ("experiment_log", "raw_data")

ACTOR = "experimental-worker"

PRESSURISE = "Run the pressure series on the doped samples."


def node_status(database: Database, node_id: str) -> str:
    """A node's status, read from PostgreSQL rather than from a return value."""
    with database.read_only() as session:
        return str(
            session.execute(
                text("SELECT status FROM dag_nodes WHERE node_id = :node"),
                {"node": node_id},
            ).scalar_one()
        )


# ── A10 ─────────────────────────────────────────────────────────────────────


async def test_a10_a_waiting_lab_task_is_woken_by_the_signal(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """A10: "Mock Experimental Worker enters WAITING_EXTERNAL and later resumes
    from Temporal signal/event."

    Both halves are asserted where they can be seen. The *wait* is a fact about
    what has not happened: the node is in WAITING_EXTERNAL, the job is in
    WAITING_EXTERNAL, and there is no Execution Record — the run has produced
    nothing, because the lab has not answered and the scenario has no duration to
    wait out. The *resume* is the signal, which nothing in RAVEL could have
    substituted for: the run is blocked in a durable wait and the signal is the
    only thing that ends it.

    The catalogue is asked first. `waits_for_signal` is where A10's scenario says
    what it is, and a scenario that acquired a duration would fail here rather
    than passing as a wait that happened to finish.
    """
    scenario = catalogue().lab_scenario(LONG_WAIT)
    assert scenario.waits_for_signal, (
        f"{LONG_WAIT} no longer waits for an external signal, so this test is "
        "not testing A10"
    )

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
        records = RecordRepositories(session, headless.project.project_id)
        jobs = records.jobs.for_node(prepared.node_id)
        while_waiting = records.executions.for_node(prepared.node_id)

    assert [job.state for job in jobs] == [JobState.WAITING_EXTERNAL]
    assert while_waiting == [], (
        "the run recorded an execution while it was still waiting, so the wait "
        "was not a wait"
    )

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
        after = RecordRepositories(session, headless.project.project_id).executions.for_node(
            prepared.node_id
        )
    assert len(after) == 1, "the signal produced a record, and only one"


# ── A11 ─────────────────────────────────────────────────────────────────────


async def test_a11_a_report_the_contract_does_not_permit_parks_the_node(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """A11: "Mock Lab reports out-of-contract condition; Worker pauses and
    escalates; does not answer scientifically itself."

    The last clause is the one this test is built around. The lab reports a
    pressure it cannot reach and the contract permits no pressure at all, so the
    Worker has nothing to authorise the substitution — and the assertion is that
    it did not invent one. Four things say so: the node is parked at the state
    the catalogue names, the deviation is unresolved, the contract is still the
    version it was, and the only record against the node is one that ended as a
    DEVIATION rather than as a result. A Worker that had decided the lower
    pressure was scientifically fine would have produced all four differently.

    The run is deliberately left waiting. Nothing in this test answers the
    deviation, because A12 is the item about answering it — and a test that
    answered here would be a test of Master wearing a Worker's name.
    """
    scenario = catalogue().lab_scenario(PRESSURE)
    assert scenario.report is not None, f"{PRESSURE} no longer reports anything"
    assert scenario.expected_worker_state == NodeStatus.WAITING_DECISION.value, (
        f"the catalogue now expects {scenario.expected_worker_state!r} of a "
        "report the contract does not permit"
    )

    headless.lab(PRESSURE)
    prepared = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)

    await headless.client.start_node_run(
        project_id=headless.project.project_id,
        node_id=prepared.node_id,
        actor_id=ACTOR,
        execution_contract_version=prepared.contract.version,
    )

    def parked() -> bool:
        return (
            node_status(headless.database, prepared.node_id)
            == NodeStatus.WAITING_DECISION.value
        )

    await await_state(parked)

    with headless.database.read_only() as session:
        records = RecordRepositories(session, headless.project.project_id)
        deviations = records.deviations.all()
        messages = records.messages.for_node(prepared.node_id)
        jobs = records.jobs.for_node(prepared.node_id)
        executions = records.executions.for_node(prepared.node_id)
        contract = ExecutionContractRepository(session, headless.project.project_id).for_node(
            prepared.node_id
        )

    assert len(deviations) == 1
    deviation = deviations[0]
    assert not deviation.permitted, (
        "the contract permits no pressure, so the request cannot have been permitted"
    )
    assert deviation.requested_action, "a deviation that does not say what was asked for"
    # Raised by the bench, not by the Worker. The Worker's part in this is to
    # notice and pass it on, and an attribution naming an agent would be the
    # system claiming a decision nobody made.
    assert deviation.raised_by == f"backend:{jobs[0].backend}"
    assert [execution.termination_status for execution in executions] == [
        TerminationStatus.DEVIATION
    ], (
        "the run has to end as a deviation rather than as a result: a Worker "
        "that had answered the bench's question would have recorded a result, "
        "and a result is what Review would then judge"
    )
    assert executions[0].completeness.delivered_outputs == ()
    assert deviation.is_open, (
        "the Worker answered its own question; a deviation nobody has resolved "
        "is the only thing that keeps this an escalation"
    )
    assert deviation.resolved_by_decision_ref is None

    assert [message.kind for message in messages] == [WorkerMessageKind.ESCALATE], (
        "a Worker with a question it may not answer says one thing: ESCALATE"
    )

    assert contract.version == prepared.contract.version, (
        "the Worker revised its own contract"
    )
    assert contract.allowed_ranges == prepared.contract.allowed_ranges


# ── A12 ─────────────────────────────────────────────────────────────────────


async def test_a12_master_answers_the_deviation_and_the_history_survives(
    headless: Headless,
) -> None:
    """A12: "Master resolves deviation by authorized decision: revised
    contract/new node/termination; history preserved."

    The revision is the answer this script gives; the other two are different
    scenarios rather than different assertions about this one. What is asserted
    is that the revision was *authorized* — a Decision Record of the right type,
    taken by Master, permitted by the envelope — and that it is visible as a
    revision rather than as an edit: version 1 still exists, still permits what
    it permitted, and still has its own execution record against it.

    The first run ended because a Worker asked a question. The second is the run
    that question made possible, under a contract that names one more action than
    the first. A system that had written version 2 in place of version 1 would
    look identical from the outside and could not answer "what was the bench
    permitted when it stopped".
    """
    headless.lab(PRESSURE)
    master = headless.master(
        (
            Task(
                build=lambda: DagNode.create(
                    project_id=headless.project.project_id,
                    node_type=NodeType.EXPERIMENT,
                    objective=PRESSURISE,
                    created_by=AgentRole.MASTER.value,
                ),
                criteria=("The pressure series separates the samples.",),
                required_outputs=LAB_OUTPUTS,
            ),
        ),
        outcome=ProjectOutcome.INCONCLUSIVE,
    )

    run = await headless.drive(master, headless.review())

    assert run.finished, f"the loop halted at {run.status.value}"
    assert run.status is ProjectStatus.INCONCLUSIVE
    node = master.planned[0]

    with headless.database.read_only() as session:
        records = RecordRepositories(session, master.project_id)
        deviations = records.deviations.all()
        decisions = records.decisions.all()
        executions = records.executions.for_node(node.node_id)
        contracts = ExecutionContractRepository(session, master.project_id)
        first = contracts.for_node(node.node_id, version=1)
        second = contracts.for_node(node.node_id, version=2)

    assert len(deviations) == 1
    deviation = deviations[0]
    assert not deviation.is_open, "Master's answer never reached the deviation"

    resolving = [
        decision
        for decision in decisions
        if decision.decision_id == deviation.resolved_by_decision_ref
    ]
    assert len(resolving) == 1, (
        "the deviation names a decision that does not exist, so nobody can find "
        "out who answered it or why"
    )
    assert resolving[0].decision_type is DecisionType.REVISE_EXECUTION_CONTRACT
    assert resolving[0].authority_check.actor_role == AgentRole.MASTER.value
    assert resolving[0].authority_check.permitted
    assert resolving[0].rationale, "a decision without a stated reason"

    assert deviation.requested_action in second.allowed_actions, (
        "the revised contract does not permit what the bench asked for, so the "
        "answer did not answer anything"
    )
    assert deviation.requested_action not in first.allowed_actions, (
        "the original contract already permitted it, and then there was nothing "
        "to escalate"
    )

    # History preserved: the version that was in force when the bench stopped is
    # still there, saying what it said, with the run it governed.
    assert first.version == 1
    assert first.contract_id != second.contract_id
    assert [execution.execution_contract_version for execution in executions] == [1, 2], (
        "the run that stopped and the run that followed are two executions under "
        f"two versions: {[e.execution_contract_version for e in executions]}"
    )
    assert executions[0].termination_status is TerminationStatus.DEVIATION
    assert executions[0].execution_contract_ref == first.contract_id
    assert executions[1].execution_contract_ref == second.contract_id


def test_a12_a_revision_keeps_the_question_the_node_was_frozen_for(
    database: Database, prepare: Callable[..., Prepared]
) -> None:
    """A12 and A14 meet here, and the meeting is the assertion.

    Widening what a bench may do is not the same as changing what would count as
    an answer. The revised contract carries the acceptance contract forward
    rather than severing it — otherwise a revision would quietly become a way to
    re-ask the question, and the frozen criteria A14 protects would be
    reachable by revising an execution contract instead of by editing them.

    Read at the repository rather than through the loop, because what is being
    asserted is a property of the record a revision produces, and the loop does
    not vary it.
    """
    prepared = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)
    project_id = prepared.project_id
    current = prepared.contract

    with database.transaction() as session:
        contracts = ExecutionContractRepository(session, project_id)
        revised = contracts.add(
            ExecutionContract(
                project_id=project_id,
                node_id=current.node_id,
                version=current.version + 1,
                objective=current.objective,
                procedure=current.procedure,
                allowed_actions=(*current.allowed_actions, "raise_pressure"),
                allowed_retries=current.allowed_retries,
                required_outputs=current.required_outputs,
                acceptance_contract_ref=current.acceptance_contract_ref,
            )
        )

    assert revised.acceptance_contract_ref == current.acceptance_contract_ref, (
        "the revision changed what the node is judged against, which is not a "
        "revision of the terms"
    )

    with database.read_only() as session:
        contracts = ExecutionContractRepository(session, project_id)
        assert contracts.for_node(current.node_id, version=1).allowed_actions == (
            current.allowed_actions
        )
        assert contracts.for_node(current.node_id).version == 2
