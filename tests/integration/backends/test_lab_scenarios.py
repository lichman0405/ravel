"""Every laboratory scenario in the catalogue, played against the Worker's rules.

A lab scenario is not only a sequence of backend states. Three of the five end
in something a Worker must *do* — ask for what did not arrive, or escalate a
question it may not answer — and the catalogue names that expected action. So
each test here runs the scenario through the mock and then through
`ravel.execution.worker_rules`, which is the code that decides what the Worker
says. The expectation is read from the catalogue's `expected_action` and
`expected_worker_state`, never restated.

The rule being tested is the one in `docs/02_AGENT_MODEL.md` §5: a Worker
executes an explicit contract, and a request the contract does not name is
refused — by the contract, not by the Worker's judgement. Silence is refusal,
and the escalation says which silence it found.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from ravel.backends import catalogue
from ravel.domain.enums import JobState, NodeStatus, NodeType, WorkerMessageKind
from ravel.execution.backends import ExternalDelivery
from ravel.execution.worker_rules import adjudicate, on_incomplete_delivery

pytestmark = pytest.mark.integration

LAB_SCENARIOS = tuple(sorted(catalogue().experiment))

#: What a contract requires when a scenario does not say. Two outputs, so that
#: a lab which sends one of them is visibly short of what it owed.
TWO_OUTPUTS = ("experiment_log", "raw_data")

#: Every scenario whose lab reports something its contract may not permit.
DEVIATION_SCENARIOS = tuple(
    sorted(
        scenario_id
        for scenario_id, scenario in catalogue().experiment.items()
        if scenario.report is not None
    )
)


def _to_terminal(backend, mock_clock, reference: str, *, step: float = 1.0) -> list[JobState]:
    """Advance a lab job to its end, returning the states it passed through."""
    seen = [backend.status(reference).state]
    for _ in range(20):
        if seen[-1].is_terminal:
            return seen
        mock_clock.advance(step)
        state = backend.status(reference).state
        if state is not seen[-1]:
            seen.append(state)
    raise AssertionError(f"{reference} never ended; it is at {seen[-1].value}")


# ── The two scenarios that end in work being delivered ──────────────────────


def test_a_lab_that_answers_delivers_what_the_contract_required(
    prepare, lab, mock_clock
) -> None:
    scenario = catalogue().lab_scenario("LAB_SUCCESS")
    assert scenario.wait_seconds > 0, "this scenario is about the wait"
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)

    backend = lab("LAB_SUCCESS")
    handle = backend.submit(node.request())
    # Nothing is delivered before the lab has had time to do the work.
    assert backend.status(handle.backend_job_ref).state is JobState.SUBMITTED
    mock_clock.advance(scenario.wait_seconds)
    assert backend.status(handle.backend_job_ref).state is JobState.RUNNING

    _to_terminal(backend, mock_clock, handle.backend_job_ref)
    outputs = backend.collect(handle.backend_job_ref)

    assert outputs.delivered_outputs == node.contract.required_outputs
    assert backend.status(handle.backend_job_ref).state is JobState.COMPLETED


def test_a_lab_that_waits_for_an_external_signal_does_not_answer_first(
    prepare, lab, mock_clock
) -> None:
    """A10's backend half: the wait is the scenario, not a duration.

    The lab reaches WAITING_EXTERNAL and stays there however long the mock_clock
    runs. Nothing but a delivery moves it, which is what makes the durable wait
    in the workflow the thing that ends it.
    """
    scenario = catalogue().lab_scenario("LAB_LONG_WAIT")
    assert scenario.waits_for_signal, "the catalogue changed meaning"
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)

    backend = lab("LAB_LONG_WAIT")
    handle = backend.submit(node.request())
    mock_clock.advance(60.0)
    backend.status(handle.backend_job_ref)  # SUBMITTED -> RUNNING
    mock_clock.advance(60.0)
    assert backend.status(handle.backend_job_ref).state is JobState.WAITING_EXTERNAL

    # However long it is left, it does not answer. The mock_clock is not what ends
    # this wait.
    for _ in range(5):
        mock_clock.advance(3600.0)
    assert backend.status(handle.backend_job_ref).state is JobState.WAITING_EXTERNAL

    arrived = backend.deliver(
        handle.backend_job_ref,
        ExternalDelivery(
            summary="The operator confirmed the run.",
            delivered_outputs=node.contract.required_outputs,
        ),
    )

    assert arrived.state is JobState.COMPLETED
    assert backend.collect(handle.backend_job_ref).delivered_outputs == (
        node.contract.required_outputs
    )


# ── A13: the delivery that does not complete ────────────────────────────────


def test_a_partial_delivery_is_found_by_comparing_names_with_the_contract(
    prepare, lab, mock_clock
) -> None:
    """The completeness check is a comparison, not a judgement.

    The lab delivers exactly what the catalogue says it delivers, and what is
    missing is whatever the contract required and the lab did not send. Both
    halves are read from files, so a scenario that started delivering
    everything would fail here rather than quietly stop testing anything.
    """
    scenario = catalogue().lab_scenario("LAB_MISSING_RAW_DATA")
    assert scenario.delivery, "this scenario is about a delivery that is short"
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)

    backend = lab("LAB_MISSING_RAW_DATA")
    handle = backend.submit(node.request())
    _to_terminal(backend, mock_clock, handle.backend_job_ref)
    outputs = backend.collect(handle.backend_job_ref)

    required = set(node.contract.required_outputs)
    delivered = set(outputs.delivered_outputs)
    missing = required - delivered
    assert delivered == set(scenario.delivered_outputs)
    assert missing == required - set(scenario.delivered_outputs)
    assert backend.status(handle.backend_job_ref).state is JobState.COMPLETED, (
        "the lab did answer; it answered incompletely, and those are different "
        "things for the record to say"
    )

    # A13: the Worker asks for what did not arrive. It does not fill the gap,
    # and it does not pass a partial result on as if it were whole.
    statement = on_incomplete_delivery(tuple(sorted(missing)))
    assert statement.kind is WorkerMessageKind.REQUEST_MISSING_INFORMATION
    for name in missing:
        assert name in statement.body


def test_a_complete_delivery_produces_no_message_at_all(
    prepare, lab, mock_clock
) -> None:
    """The check has to be able to come back empty, or it is not a check."""
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)
    backend = lab("LAB_SUCCESS")
    handle = backend.submit(node.request())
    _to_terminal(backend, mock_clock, handle.backend_job_ref)

    delivered = set(backend.collect(handle.backend_job_ref).delivered_outputs)

    assert not set(node.contract.required_outputs) - delivered


# ── A11: the report the Worker may not answer ───────────────────────────────


@pytest.mark.parametrize("scenario_id", DEVIATION_SCENARIOS)
def test_a_report_outside_the_contract_is_escalated_not_answered(
    scenarios, prepare, lab, mock_clock, scenario_id: str
) -> None:
    """A11's backend half, for both shapes a report can take.

    The contract here permits nothing — no ranges, no substitutions — which is
    the case the rule has to get right. A Worker that treated an unnamed
    parameter as unconstrained would act on the lab's number and record a
    result nobody authorised.

    The catalogue describes these two scenarios differently: one names the
    *state* it expects (`WAITING_DECISION`), the other names the *action*
    (`ESCALATE`). Both are read here, and the test asserts they agree — a
    report the contract does not permit is an escalation, and a confirmation
    would leave the run going rather than parked. Which is worth checking,
    because it is the one thing the two catalogue spellings have to mean
    together.
    """
    scenario = scenarios.lab_scenario(scenario_id)
    assert scenario.report is not None
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)

    backend = lab(scenario_id)
    handle = backend.submit(node.request())
    mock_clock.advance(scenario.wait_seconds)
    # The report is not an ending. The lab is still running and has told RAVEL
    # about the question; what happens next is RAVEL's.
    status = backend.status(handle.backend_job_ref)
    assert status.state is JobState.RUNNING
    assert status.deviation is not None

    verdict = adjudicate(status.deviation, node.contract)

    assert not verdict.permitted
    assert verdict.statement.kind is WorkerMessageKind.ESCALATE
    assert not verdict.statement.body.startswith("the execution contract permits")
    assert "Master" in verdict.statement.body, (
        "an escalation that does not say who has to answer it is not an "
        "escalation"
    )
    # What the catalogue says about this scenario, in whichever form it says it.
    if scenario.expected_action:
        assert WorkerMessageKind(scenario.expected_action) is verdict.statement.kind
    if scenario.expected_worker_state:
        assert NodeStatus(scenario.expected_worker_state) is NodeStatus.WAITING_DECISION
    assert scenario.expected_action or scenario.expected_worker_state, (
        f"{scenario_id} says a Worker must do something and does not say what"
    )


def test_a_contract_that_permits_the_substitution_permits_it(
    prepare, lab, mock_clock
) -> None:
    """The other side of the rule, so the tests above are not just "refuse all".

    `LAB_OPERATOR_QUESTION_UNDEFINED` carries `contract_substitution_allowed:
    false`, and this is that contract with the flag flipped — written by hand
    because the catalogue has no such entry, and the point is that the same
    report is now permitted. A Worker that escalated everything would pass the
    tests above and fail this one.
    """
    scenario = catalogue().lab_scenario("LAB_OPERATOR_QUESTION_UNDEFINED")
    assert not scenario.substitution_allowed
    assert scenario.report is not None and scenario.report.substitution is not None
    given, instead = scenario.report.substitution
    node = prepare(
        node_type=NodeType.EXPERIMENT,
        required_outputs=TWO_OUTPUTS,
        allowed_substitutions=(f"{given} -> {instead}",),
    )

    backend = lab("LAB_OPERATOR_QUESTION_UNDEFINED")
    handle = backend.submit(node.request())
    mock_clock.advance(scenario.wait_seconds)
    status = backend.status(handle.backend_job_ref)

    assert status.deviation is not None
    verdict = adjudicate(status.deviation, node.contract)

    assert verdict.permitted
    assert verdict.statement.kind is WorkerMessageKind.CONFIRM


def test_a_reported_value_inside_a_permitted_range_is_a_confirmation(
    prepare, lab, mock_clock
) -> None:
    """`allowed_ranges` is a window, and the value checked is the reachable one.

    The scenario reports both what was asked for and what the lab can reach.
    What would be acted on is the reachable value, so that is what the contract
    is asked about — a contract that permitted the request but not the
    substitute has permitted nothing that happens.
    """
    scenario = catalogue().lab_scenario("LAB_DEVIATION_PRESSURE")
    assert scenario.report is not None and scenario.report.parameter
    parameter = scenario.report.parameter
    reachable = scenario.report.value
    assert reachable is not None
    node = prepare(
        node_type=NodeType.EXPERIMENT,
        required_outputs=TWO_OUTPUTS,
        allowed_ranges={parameter: f"{reachable - 1}..{reachable + 1}"},
    )

    backend = lab("LAB_DEVIATION_PRESSURE")
    handle = backend.submit(node.request())
    mock_clock.advance(scenario.wait_seconds)
    status = backend.status(handle.backend_job_ref)
    assert status.deviation is not None

    verdict = adjudicate(status.deviation, node.contract)

    assert verdict.permitted, verdict.reason
    assert verdict.statement.kind is WorkerMessageKind.CONFIRM


def test_a_report_before_the_lab_has_looked_is_not_a_report(
    prepare, lab, mock_clock
) -> None:
    """A lab that complained instantly would be reporting a question it had not read.

    The wait is supplied here rather than read from the catalogue, and that is
    not a workaround: neither of the catalogue's reporting scenarios carries a
    `wait_seconds_test_mode`, so both report the moment they are asked, and the
    rule that says a report waits for the lab to have had time to look cannot
    be reached through them. The rule belongs to the mock, so the scenario it
    is checked against is the catalogue's with a wait put on it.
    """
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)

    backend = lab("LAB_DEVIATION_PRESSURE")
    backend.scenario = replace(backend.scenario, wait_seconds=2.0)
    handle = backend.submit(node.request())

    mock_clock.advance(1.0)
    assert backend.status(handle.backend_job_ref).deviation is None

    mock_clock.advance(1.0)
    assert backend.status(handle.backend_job_ref).deviation is not None


def test_a_report_is_not_repeated_once_the_job_has_ended(
    prepare, lab, mock_clock
) -> None:
    """The question is about work in flight; a stopped job has no question.

    `LAB_DEVIATION_PRESSURE` never ends on its own — the lab is waiting to be
    told what to do — so the ending here is the cancellation the durable layer
    performs when it stops work over a deviation.
    """
    scenario = catalogue().lab_scenario("LAB_DEVIATION_PRESSURE")
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)
    backend = lab("LAB_DEVIATION_PRESSURE")
    handle = backend.submit(node.request())
    mock_clock.advance(scenario.wait_seconds)
    assert backend.status(handle.backend_job_ref).deviation is not None

    assert backend.cancel(handle.backend_job_ref) is True

    assert backend.status(handle.backend_job_ref).state is JobState.CANCELLED
    assert backend.status(handle.backend_job_ref).deviation is None


def test_a_lab_that_does_not_report_anything_never_does(
    prepare, lab, mock_clock
) -> None:
    """The other four scenarios, so that the deviation is a scenario and not a habit."""
    node = prepare(node_type=NodeType.EXPERIMENT, required_outputs=TWO_OUTPUTS)
    for scenario_id in LAB_SCENARIOS:
        if scenario_id in DEVIATION_SCENARIOS:
            continue
        backend = lab(scenario_id)
        handle = backend.submit(node.request())
        mock_clock.advance(60.0)
        assert backend.status(handle.backend_job_ref).deviation is None, scenario_id


# ── The catalogue's own shape ───────────────────────────────────────────────


def test_the_lab_scenarios_this_gate_plays_are_the_catalogues() -> None:
    assert LAB_SCENARIOS, "the acceptance catalogue names no laboratory scenarios"
    assert set(DEVIATION_SCENARIOS) <= set(LAB_SCENARIOS)
