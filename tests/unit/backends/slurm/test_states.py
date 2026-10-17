"""Slurm's vocabulary read as RAVEL's, one state at a time.

These are the tests that decide whether a retry happens and, once, whether a
resource limit is treated as a machine fault. Both are cheap to get wrong and
expensive to notice: a `TIMEOUT` read as infrastructure spends a second
allocation asking the same question, and a `FAILED` read as infrastructure
would re-run an experiment whose result was already wrong.
"""

from __future__ import annotations

import pytest

from ravel.backends.slurm.states import (
    SLURM_STATES,
    normalize_state,
    read_exit_code,
    read_state,
)
from ravel.domain.enums import FailureClass, JobState


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("PENDING", "PENDING"),
        ("pending", "PENDING"),
        ("  RUNNING  ", "RUNNING"),
        # Slurm appends `+` when a state changed while the caller was looking.
        ("COMPLETING+", "COMPLETING"),
        # And names who acted, with a space.
        ("CANCELLED by 1000", "CANCELLED"),
        ("FAILED by user", "FAILED"),
        ("OUT_OF_MEMORY", "OUT_OF_MEMORY"),
    ],
)
def test_a_state_string_is_reduced_to_the_word_it_is_about(raw: str, expected: str) -> None:
    assert normalize_state(raw) == expected


@pytest.mark.parametrize(
    ("raw", "state", "failure_class"),
    [
        # Not started. Nothing failed and nothing is retryable, because there
        # is no attempt to classify yet.
        ("PENDING", JobState.SUBMITTED, None),
        ("CONFIGURING", JobState.SUBMITTED, None),
        ("REQUEUED", JobState.SUBMITTED, None),
        ("RESIZING", JobState.SUBMITTED, None),
        # Started.
        ("RUNNING", JobState.RUNNING, None),
        ("COMPLETING", JobState.RUNNING, None),
        ("SUSPENDED", JobState.RUNNING, None),
        ("STOPPED", JobState.RUNNING, None),
        # Ended.
        ("COMPLETED", JobState.COMPLETED, None),
        # The work is what went wrong.
        ("FAILED", JobState.FAILED, FailureClass.NON_RETRYABLE),
        ("OUT_OF_MEMORY", JobState.FAILED, FailureClass.NON_RETRYABLE),
        ("SPECIAL_EXIT", JobState.FAILED, FailureClass.NON_RETRYABLE),
        # The machine is what went wrong.
        ("NODE_FAIL", JobState.FAILED, FailureClass.INFRA_RETRYABLE),
        ("BOOT_FAIL", JobState.FAILED, FailureClass.INFRA_RETRYABLE),
        ("PREEMPTED", JobState.FAILED, FailureClass.INFRA_RETRYABLE),
        ("REVOKED", JobState.FAILED, FailureClass.INFRA_RETRYABLE),
        # A term of the contract the run did not fit inside.
        ("TIMEOUT", JobState.TIMED_OUT, FailureClass.NON_RETRYABLE),
        ("DEADLINE", JobState.TIMED_OUT, FailureClass.NON_RETRYABLE),
        # Stopped by somebody.
        ("CANCELLED", JobState.CANCELLED, None),
        ("CANCELLED by 1000", JobState.CANCELLED, None),
    ],
)
def test_every_state_slurm_reports_has_a_row(
    raw: str, state: JobState, failure_class: FailureClass | None
) -> None:
    reading = read_state(raw)
    assert reading.state is state
    assert reading.failure_class is failure_class
    assert reading.known
    assert reading.detail


def test_a_resource_limit_is_not_a_machine_fault() -> None:
    """The one mapping in this file that is a scientific position, not a lookup.

    A wall-clock limit is a term Master set and Review approved. A job that hit
    it did not fail the way a node failure fails — it did not fit inside the
    terms it was given, which is a fact about the work. Only Master may change a
    contract term, so the backend reports and does not retry; a `TIMED_OUT` read
    as infrastructure would have a worker spend a second allocation to ask a
    question the contract already answered.
    """
    reading = read_state("TIMEOUT")
    assert reading.state is JobState.TIMED_OUT
    assert reading.state.is_terminal
    assert reading.failure_class is FailureClass.NON_RETRYABLE
    assert reading.failure_class is not FailureClass.INFRA_RETRYABLE


@pytest.mark.parametrize("raw", ["BOOT_FAIL", "NODE_FAIL", "PREEMPTED"])
def test_a_machine_that_failed_is_worth_asking_again(raw: str) -> None:
    """The work never had a chance to be wrong, so repeating it is not repeating it."""
    reading = read_state(raw)
    assert reading.state is JobState.FAILED
    assert reading.failure_class is FailureClass.INFRA_RETRYABLE


def test_a_state_ravel_does_not_know_is_reported_rather_than_guessed() -> None:
    """An unrecognised state must not become the nearest state RAVEL has.

    A cluster this version has not seen — a site-configured state, a newer
    Slurm — is terminal in RAVEL's reading and carries no failure class, which
    `decide_retry` treats as non-retryable. The detail quotes what arrived, so
    the missing row is visible rather than mysterious.
    """
    reading = read_state("FUTURE_STATE_FROM_A_NEWER_SLURM")
    assert reading.state is JobState.FAILED
    assert reading.failure_class is None
    assert not reading.known
    assert "FUTURE_STATE_FROM_A_NEWER_SLURM" in reading.detail


def test_the_table_covers_every_state_that_ends_a_job() -> None:
    """Every terminal row says either what failed or that nothing did.

    A completed or cancelled job has no failure to classify, and every other
    terminal state must have one — a terminal state with no class is read as
    "do not retry", which is right for an unknown state and wrong for a state
    this module claims to know.
    """
    for word, (state, failure_class, _detail) in SLURM_STATES.items():
        if not state.is_terminal:
            assert failure_class is None, f"{word} is not terminal but has a failure class"
        elif state in (JobState.COMPLETED, JobState.CANCELLED):
            assert failure_class is None, f"{word} has a failure class it cannot have"
        else:
            assert failure_class is not None, f"{word} is terminal with no failure class"


@pytest.mark.parametrize(
    ("allocation", "batch", "expected", "said"),
    [
        ("0:0", "0:0", 0, ""),
        ("0:0", "1:0", 1, ""),
        ("0:0", "2:0", 2, ""),
        # The batch step's code is the script's own; the allocation's is `0:0`
        # for anything Slurm considers completed, so the step is what is read.
        ("0:0", "", 0, ""),
        ("1:0", "", 1, ""),
        # Killed by a signal, which has no exit status to report.
        ("0:0", "0:9", None, "signal 9"),
        ("0:0", "137:0", 137, ""),
    ],
)
def test_an_exit_code_is_read_from_the_batch_step_first(
    allocation: str, batch: str, expected: int | None, said: str
) -> None:
    code, note = read_exit_code(allocation, batch)
    assert code == expected
    if said:
        assert said in note


def test_an_unreadable_exit_code_says_so_rather_than_reporting_zero() -> None:
    """A zero would be indistinguishable from success, which is the one thing
    it must not be when the scheduler answered something we cannot read."""
    code, note = read_exit_code("", "")
    assert code is None
    assert "no exit code" in note

    # A pair that is shaped right and says something that is not a number. It
    # is reported as unreadable rather than as zero, because zero is success.
    code, note = read_exit_code("", "abc:0")
    assert code is None
    assert "not a number" in note

    # And something not shaped like a pair at all, which is not an exit code.
    code, note = read_exit_code("something-else", "")
    assert code is None
    assert "no exit code" in note
