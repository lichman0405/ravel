"""The two decisions the durable layer makes without asking anyone.

*Should this failed work run again?* and *how long do we wait?* Both are pure
functions of the contract and the job record, and both are here rather than
inside an activity because a decision that lives in a workflow is a decision
nobody can unit-test.

The retry rule is the one worth reading carefully. Retrying is a scientific
cost — a second experiment, a second charge, a second result that will be
compared against the first — so the rule is not "try again if it looks
transient". It is: **only a failure the backend classified as retryable, within
a budget the contract granted, is retried.** Everything else stops.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from enum import StrEnum

from ravel.domain.enums import FailureClass, JobState


class RetryVerdict(StrEnum):
    """Whether the work is tried again."""

    RETRY = "RETRY"
    STOP = "STOP"


@dataclass(frozen=True)
class RetryDecision:
    """A verdict and the sentence explaining it.

    The reason travels with the decision because it ends up in the Execution
    Record, and "why did this not run again" is a question the record has to be
    able to answer without a reader reconstructing it from the event stream.
    """

    verdict: RetryVerdict
    reason: str

    @property
    def is_retry(self) -> bool:
        """Whether the work runs again."""
        return self.verdict is RetryVerdict.RETRY


def decide_retry(
    *,
    state: JobState,
    failure_class: FailureClass | None,
    attempt: int,
    allowed_retries: int,
) -> RetryDecision:
    """Whether to run another attempt of work that has just ended.

    Two counters that look alike and are not:

    - `attempt` is the attempt that just ended, numbered from 1. It lives in the
      workflow, so a Temporal activity retry re-uses it and does not burn one.
    - `allowed_retries` is the contract's grant. One retry means the work may be
      tried twice in total.

    The order of the checks is the policy. A job that has not ended is not
    retried, because it may still be running; a job that succeeded is not
    retried, because there is nothing left to do; and a job that was cancelled
    is not retried, because cancelling was somebody's decision and running it
    again would overrule them without saying so.
    """
    if not state.is_terminal:
        return RetryDecision(
            RetryVerdict.STOP, f"the job is {state.value} and has not ended"
        )
    if state is JobState.COMPLETED:
        return RetryDecision(RetryVerdict.STOP, "the work was delivered")
    if state is JobState.CANCELLED:
        return RetryDecision(
            RetryVerdict.STOP, "the work was cancelled, and a cancellation is a decision"
        )
    if failure_class is None:
        return RetryDecision(
            RetryVerdict.STOP,
            f"the backend ended the work as {state.value} without classifying the "
            "failure, and an unclassified failure is not retried",
        )
    if failure_class is FailureClass.NON_RETRYABLE:
        return RetryDecision(
            RetryVerdict.STOP, "the backend classified the failure as non-retryable"
        )

    retries_spent = attempt - 1
    if retries_spent >= allowed_retries:
        return RetryDecision(
            RetryVerdict.STOP,
            f"the failure is retryable but the contract allows {allowed_retries} "
            f"retr{'y' if allowed_retries == 1 else 'ies'} and {retries_spent} "
            f"{'has' if retries_spent == 1 else 'have'} been spent",
        )
    return RetryDecision(
        RetryVerdict.RETRY,
        f"the backend classified the failure as retryable and the contract allows "
        f"{allowed_retries} retr{'y' if allowed_retries == 1 else 'ies'}, of which "
        f"{retries_spent} {'has' if retries_spent == 1 else 'have'} been spent",
    )


@dataclass(frozen=True)
class RunLimits:
    """How long the durable layer is willing to wait, and how often it asks.

    `external_wait` and `deadline` are different clocks and are not
    interchangeable. The deadline runs from the moment one attempt's work is
    submitted; the external wait starts each time that attempt reports it is
    blocked on something outside RAVEL. A lab that answers after twenty minutes
    should not be timed out because the compute stage before it took fifty.

    Both clocks are per *attempt*, not per run. A retry is a fresh piece of
    work with a fresh budget, and charging it for what an earlier attempt spent
    would time out a job that had barely started — while still bounding each
    attempt, which is the whole point of having the ceiling.
    """

    poll_interval: timedelta
    deadline: timedelta
    external_wait: timedelta
    activity_timeout: timedelta

    @classmethod
    def from_settings(cls, settings: object) -> RunLimits:
        """Read the limits off `Settings`.

        Takes the object rather than a settings module so this stays callable
        from a unit test with a stand-in. The caller then carries the result in
        the run plan; the workflow itself never reads settings, because a file
        can differ between a run and its replay.
        """
        return cls(
            poll_interval=timedelta(seconds=_number(settings, "job_poll_seconds")),
            deadline=timedelta(seconds=_number(settings, "job_deadline_seconds")),
            external_wait=timedelta(seconds=_number(settings, "external_wait_seconds")),
            activity_timeout=timedelta(
                seconds=_number(settings, "job_activity_timeout_seconds")
            ),
        )


def _number(settings: object, name: str) -> float:
    """Read one numeric setting, refusing anything that is not a positive number.

    Raises:
        AttributeError: The setting is missing.
        ValueError: It is not a positive number. A zero or negative interval
            would make the poll loop spin or the deadline already passed, and
            both would look like a backend fault rather than a typo.
    """
    value = getattr(settings, name)
    if not isinstance(value, int | float) or isinstance(value, bool) or value <= 0:
        raise ValueError(f"{name} must be a positive number, not {value!r}")
    return float(value)
