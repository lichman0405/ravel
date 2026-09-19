"""Every compute scenario in the catalogue, played exactly as it is written.

The assertions read the catalogue. `scenario.sequence_for(attempt)` is what the
catalogue says one attempt passes through, and each test walks the mock forward
on an injected mock_clock and compares what it observed with that tuple. If someone
changes the acceptance file, these tests change with it; if someone changes the
mock so that it no longer produces what the file describes, they fail.

Two things are checked beyond the state sequence, because a sequence alone is
not enough to make a scenario what it claims to be:

- **The failure class.** `INFRA_RETRYABLE` and `NON_RETRYABLE` are the same
  `FAILED` state and mean opposite things to the retry policy. A mock that lost
  the class would still pass a states-only test and would then be retried
  forever, or never.
- **What was delivered.** `outputs: {complete: false}` is the whole of
  `COMPUTE_MISSING_OUTPUT`, and it is expressed as which of the *contract's*
  required outputs arrived — so the test asks the contract, not the mock.
"""

from __future__ import annotations

import pytest
from sqlalchemy import String

from ravel.backends import catalogue
from ravel.domain.enums import FailureClass, JobState
from ravel.execution.backends import JobOutputs
from ravel.state.tables import BackendJobRow

pytestmark = pytest.mark.integration

#: Read from the acceptance file rather than written out here. A hand-kept list
#: would be a second place for "which scenarios exist" to be answered, and the
#: one that is wrong is always the copy — so a scenario added to
#: `acceptance/MOCK_SCENARIOS.yaml` is played by this gate the moment it is
#: added, with no edit here.
COMPUTE_SCENARIOS = tuple(sorted(catalogue().compute))

def _ref_width() -> int:
    """How wide a job reference column is, read from the model rather than stated.

    Read rather than written down so that this gate tracks the column instead of
    a number stated twice — and read defensively, because the reading is also a
    check: a column that stopped being a bounded string would have no width to
    be over, and the test that uses this would then assert nothing in
    particular.
    """
    column = BackendJobRow.__table__.c.backend_job_ref
    assert isinstance(column.type, String), f"{column.key} is no longer a string column"
    width = column.type.length
    assert width is not None, f"{column.key} is unbounded, so it constrains nothing"
    return width


#: The width of `backend_jobs.backend_job_ref`, from the schema itself.
REF_WIDTH = _ref_width()


def _walk(backend, mock_clock, request, *, step: float = 0.5) -> list[JobState]:
    """Submit work and poll it to its end, collecting the states it passed through.

    The mock_clock moves one step per poll, which is what the mock's timeline is
    measured in. Consecutive repeats are collapsed: the catalogue describes a
    sequence of *states*, and a mock that reported RUNNING five times before
    completing did not thereby pass through RUNNING five times.
    """
    handle = backend.submit(request)
    seen = [handle.state]
    for _ in range(20):
        if seen[-1].is_terminal:
            break
        mock_clock.advance(step)
        state = backend.status(handle.backend_job_ref).state
        if state is not seen[-1]:
            seen.append(state)
    return seen


@pytest.mark.parametrize("scenario_id", COMPUTE_SCENARIOS)
def test_a_compute_scenario_reaches_exactly_the_states_it_names(
    scenarios, prepare, compute, mock_clock, scenario_id: str
) -> None:
    scenario = scenarios.compute_scenario(scenario_id)
    node = prepare()
    backend = compute(scenario_id)

    seen = _walk(backend, mock_clock, node.request())

    assert seen == list(scenario.sequence_for(1)), (
        f"{scenario_id} passed through {[state.value for state in seen]}, and the "
        f"catalogue says {[state.value for state in scenario.sequence_for(1)]}"
    )
    assert seen[-1] in (JobState.COMPLETED, JobState.FAILED, JobState.TIMED_OUT), (
        f"{scenario_id} never ended; a scenario that leaves work running would "
        "make a run wait for a deadline it should not have reached"
    )


def test_every_named_compute_scenario_is_covered_by_this_gate() -> None:
    """The gate plays the catalogue, so this asserts the catalogue is not empty.

    A file that read back as no scenarios would make every test above vanish
    silently, and a gate that has quietly stopped testing anything is worse than
    one that fails.
    """
    assert COMPUTE_SCENARIOS, "the acceptance catalogue names no compute scenarios"


# ── What each ending means ──────────────────────────────────────────────────


def test_a_successful_compute_delivers_every_required_output(
    scenarios, prepare, compute, mock_clock
) -> None:
    scenario = scenarios.compute_scenario("COMPUTE_SUCCESS")
    assert scenario.delivers_complete_outputs, "the catalogue changed meaning"
    node = prepare()

    backend = compute("COMPUTE_SUCCESS")
    outputs = _collect(backend, mock_clock, node)

    assert outputs.delivered_outputs == node.contract.required_outputs
    assert outputs.completion_metadata["simulated"] is True, (
        "a run assembled from a mock has to say so where a reader will look"
    )


def test_a_retryable_infrastructure_failure_says_so_and_the_second_attempt_differs(
    scenarios, prepare, compute, mock_clock
) -> None:
    """A07's backend half.

    The catalogue writes this scenario as a pair of attempts, and the pair is
    the point: a mock that failed both would make "the retry was allowed" and
    "the retry was pointless" indistinguishable to whoever reads the record.
    """
    scenario = scenarios.compute_scenario("COMPUTE_RETRYABLE_INFRA_FAILURE")
    assert scenario.requires_contract_retry_permission, "the catalogue changed meaning"
    node = prepare(allowed_retries=1)

    backend = compute("COMPUTE_RETRYABLE_INFRA_FAILURE")
    first = backend.submit(node.request(attempt=1))
    _to_terminal(backend, mock_clock, first.backend_job_ref)
    first_status = backend.status(first.backend_job_ref)

    second = backend.submit(node.request(attempt=2))
    _to_terminal(backend, mock_clock, second.backend_job_ref)
    second_status = backend.status(second.backend_job_ref)

    assert first_status.state is JobState.FAILED
    assert first_status.failure_class is FailureClass.INFRA_RETRYABLE, (
        "infrastructure faults are the only ones the retry policy may act on"
    )
    assert first.backend_job_ref != second.backend_job_ref, (
        "a retry is a new job; the same reference would record one experiment "
        "twice"
    )
    assert second_status.state is JobState.COMPLETED


def test_a_scientific_failure_is_not_retryable(prepare, compute, mock_clock) -> None:
    """A07's other half: the class is what stops a scientific failure looping."""
    node = prepare(allowed_retries=3)
    backend = compute("COMPUTE_SCIENTIFIC_FAILURE")

    handle = backend.submit(node.request())
    _to_terminal(backend, mock_clock, handle.backend_job_ref)
    status = backend.status(handle.backend_job_ref)

    assert status.state is JobState.FAILED
    assert status.failure_class is FailureClass.NON_RETRYABLE


def test_a_missing_output_is_absent_from_the_contracts_required_set(
    scenarios, prepare, compute, mock_clock
) -> None:
    """A13's backend half: the run completes and is still short of what it owed.

    "Complete" and "delivered everything required" are different questions, and
    this scenario is the one that makes the difference visible. Which output is
    missing comes from the contract, so the gap is a property of the run rather
    than of the mock.
    """
    scenario = scenarios.compute_scenario("COMPUTE_MISSING_OUTPUT")
    assert not scenario.delivers_complete_outputs, "the catalogue changed meaning"
    node = prepare(required_outputs=("conductivity.csv", "notes.json", "trace.log"))

    outputs = _collect(backend=compute("COMPUTE_MISSING_OUTPUT"), mock_clock=mock_clock, node=node)

    required = set(node.contract.required_outputs)
    delivered = set(outputs.delivered_outputs)
    assert delivered < required, (
        "COMPUTE_MISSING_OUTPUT delivered everything the contract required, so "
        "there is nothing for the completeness check to find"
    )
    assert len(required - delivered) == 1
    assert len(outputs.artifacts) == len(outputs.delivered_outputs), (
        "every delivered output is supposed to have been written as an artifact"
    )


def test_a_timed_out_compute_ends_as_timed_out(prepare, compute, mock_clock) -> None:
    node = prepare()
    backend = compute("COMPUTE_TIMEOUT")

    handle = backend.submit(node.request())
    seen = _to_terminal(backend, mock_clock, handle.backend_job_ref)

    assert seen[-1] is JobState.TIMED_OUT
    assert backend.status(handle.backend_job_ref).failure_class is None, (
        "a timeout is not a failure class; reporting one would let the retry "
        "policy treat it as infrastructure"
    )


# ── The properties every mock backend owes the durable layer ────────────────


def test_submitting_the_same_attempt_twice_returns_the_same_job(
    prepare, compute
) -> None:
    """The port's idempotency requirement, which L-01 records the reason for.

    A worker killed between the backend accepting work and the row being
    written is retried, and that retry calls `submit` again. If this returned a
    second reference, the retry would be a second experiment.
    """
    node = prepare()
    backend = compute("COMPUTE_SUCCESS")
    request = node.request()

    first = backend.submit(request)
    second = backend.submit(request)

    assert first.backend_job_ref == second.backend_job_ref
    assert len(backend._jobs) == 1


def test_a_reference_is_derived_from_the_work_rather_than_generated(
    prepare, compute
) -> None:
    """So that a *restarted* mock recognises work it never saw submitted.

    The job is not in this backend's memory; the reference it would use for it
    is still the same string, which is the whole of what makes a resumed run
    resume rather than restart. Two mocks that have never met must agree, and
    two different pieces of work must not collide.
    """
    node = prepare()

    first = compute("COMPUTE_SUCCESS").reference_for(node.request())
    second = compute("COMPUTE_SUCCESS").reference_for(node.request(attempt=2))

    assert first == compute("COMPUTE_SUCCESS").reference_for(node.request())
    assert first != second, "two attempts are two jobs"
    assert first.startswith("mock-compute/")


def test_a_reference_fits_the_column_it_is_stored_in(prepare, compute) -> None:
    """Found by this gate rather than by a reviewer: a reference is a `REF`.

    `backend_jobs.backend_job_ref` is `String(64)`, and the first version of
    this mock derived a reference by spelling out the project, the node and the
    attempt — seventy-six characters through three hex identifiers. Nothing
    failed at submission; the run failed later, at the write, with a truncation
    error that named a column rather than the mock that overflowed it.
    """
    node = prepare()
    reference = compute("COMPUTE_SUCCESS").reference_for(node.request())

    assert len(reference) <= REF_WIDTH


def test_status_for_work_a_mock_never_accepted_is_an_error(
    compute,
) -> None:
    """A mock that answered for work it does not have would be inventing one."""
    backend = compute("COMPUTE_SUCCESS")

    with pytest.raises(KeyError, match="process restarts"):
        backend.status("mock-compute/proj/x/1")


def test_a_scenario_the_catalogue_does_not_name_is_refused(
    database, artifact_store
) -> None:
    """Refused where it is asked for, not halfway through a project."""
    from ravel.backends import MockComputeBackend

    with pytest.raises(KeyError, match="no compute scenario named"):
        MockComputeBackend(
            database=database, store=artifact_store, scenario="COMPUTE_PROBABLY_FINE"
        )


def test_cancelling_a_finished_job_is_reported_rather_than_pretended(
    prepare, compute, mock_clock
) -> None:
    """`cancel` returns whether it worked, and a lab at the bench says no."""
    node = prepare()
    backend = compute("COMPUTE_SUCCESS")
    handle = backend.submit(node.request())
    _to_terminal(backend, mock_clock, handle.backend_job_ref)

    assert backend.cancel(handle.backend_job_ref) is False


def test_cancelling_a_running_job_stops_it(prepare, compute) -> None:
    node = prepare()
    backend = compute("COMPUTE_SUCCESS")
    handle = backend.submit(node.request())

    assert backend.cancel(handle.backend_job_ref) is True
    assert backend.status(handle.backend_job_ref).state is JobState.CANCELLED


# ── Helpers ─────────────────────────────────────────────────────────────────


def _to_terminal(backend, mock_clock, reference: str, *, step: float = 0.5) -> list[JobState]:
    """Advance one job to its end, returning the states it passed through."""
    seen = [backend.status(reference).state]
    for _ in range(20):
        if seen[-1].is_terminal:
            return seen
        mock_clock.advance(step)
        state = backend.status(reference).state
        if state is not seen[-1]:
            seen.append(state)
    raise AssertionError(f"{reference} never ended; it is at {seen[-1].value}")


def _collect(backend, mock_clock, node) -> JobOutputs:
    """Run a submission to its end and collect what it produced."""
    handle = backend.submit(node.request())
    _to_terminal(backend, mock_clock, handle.backend_job_ref)
    return backend.collect(handle.backend_job_ref)
