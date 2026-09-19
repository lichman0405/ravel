"""One node run, as a workflow.

The whole file answers "what does a run do", and it is short on purpose. It
submits work, waits, retries when the policy says to, and ends. It reads no
database, calls no backend, and consults no clock of its own, because Temporal
replays this code from the start after every worker restart and any such call
would happen again.

Two waits, and they are not the same wait:

- A **poll** is a durable timer between status checks, which survives a restart
  the way any workflow state does.
- An **external wait** is the run blocked on something outside RAVEL — a lab,
  an operator. It ends when a signal arrives or when its own clock runs out,
  and the signal is the one thing here that comes from outside.

The retry loop is where the science is. An attempt is not a Temporal activity
retry: it is a new job, a new row, and a new entry in the Execution Record,
because a second experiment is a fact about the project and has to be visible
as one.

**Every `execute_activity` call names its `result_type`, and none of them can
be left out.** Activities are called by name rather than by function so that
the workflow sandbox never imports the module that opens database connections.
The price of the name is the type: the data converter decodes an activity's
result using the type hint the caller supplied, and a named call supplies none,
so without `result_type` the run plan comes back as a plain `dict` and fails at
its first attribute access — inside the workflow, where the traceback names the
symptom and not the cause. Adding a call here means adding its type.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from ravel.domain.enums import JobState, TerminationStatus
    from ravel.execution.policies import RetryVerdict, decide_retry
    from ravel.execution.temporal.contracts import (
        AttemptSummary,
        ExternalResult,
        JobSnapshot,
        RunInput,
        RunOutcome,
        RunPlan,
    )

#: How the job states RAVEL and the Execution Record each name, side by side.
#: Two vocabularies because they answer different questions: `JobState` is where
#: the backend's work got to, `TerminationStatus` is how the execution ended.
_TERMINATION: dict[JobState, TerminationStatus] = {
    JobState.COMPLETED: TerminationStatus.COMPLETED,
    JobState.FAILED: TerminationStatus.FAILED,
    JobState.TIMED_OUT: TerminationStatus.TIMED_OUT,
    JobState.CANCELLED: TerminationStatus.CANCELLED,
}

#: What Temporal retries on its own: infrastructure faults only. A namespace no
#: worker is polling, a database that went away mid-call — things worth trying
#: again without asking anyone.
#:
#: A scientific failure never arrives here. It comes back as a job state, and
#: `ravel.execution.policies` decides it. Conflating the two would let an
#: infrastructure hiccup be recorded as a result, or let an activity retry
#: consume a scientific attempt.
#:
#: A constant rather than a setting, deliberately: the policy is part of the
#: commands the workflow issues, so two deployments replaying one history must
#: agree on it.
#: The first activity runs before the plan exists, so its timeout cannot come
#: from the plan. It reads one node and one contract and writes one status
#: change, so a minute is generous rather than tuned.
_PLANNING_TIMEOUT = timedelta(minutes=1)

_ACTIVITY_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
)


@workflow.defn
class NodeRunWorkflow:
    """A run of one node, from READY to REVIEWING.

    Started with a workflow id derived from the node and the version of its
    contract, so two runs of the same work are impossible rather than merely
    unlikely: Temporal refuses the second start, and that refusal is what makes
    "one node, one Execution Record" true without a database constraint
    guessing at it. A node whose terms were revised is not the same work, which
    is why the version is in the id — answering an escalation with a revision
    is an instruction to run again, and an id that named only the node would
    make that instruction impossible to carry out.
    """

    def __init__(self) -> None:
        self._external: ExternalResult | None = None

    @workflow.signal
    def external_result(self, result: ExternalResult) -> None:
        """Something outside RAVEL happened; the run stops waiting for it.

        A signal is delivered to a running workflow's memory, and memory does
        not survive the worker holding it — so the handler stores the result
        and the run writes it through an activity before acting on it.
        """
        self._external = result

    @workflow.run
    async def run(self, order: RunInput) -> RunOutcome:
        """Run the node until it ends, and report how it ended."""
        plan = await workflow.execute_activity(
            "begin_node_run",
            order,
            result_type=RunPlan,
            start_to_close_timeout=_PLANNING_TIMEOUT,
            retry_policy=_ACTIVITY_RETRY,
        )

        attempts: list[AttemptSummary] = []
        attempt = order.attempt
        retry_reason = ""

        while True:
            began = workflow.now()
            snapshot = await workflow.execute_activity(
                "start_job",
                args=[plan, attempt],
                result_type=JobSnapshot,
                start_to_close_timeout=_step(plan),
                retry_policy=_ACTIVITY_RETRY,
            )
            snapshot = await self._watch(plan, snapshot)
            attempts.append(_summary(attempt, snapshot, began, workflow.now()))

            if snapshot.deviation_id is not None:
                # A deviation ends the run. The work has already been stopped by
                # the poll that found it, and the question it raised is one only
                # Master can answer — so another attempt would put the same
                # request to a backend that has already said it is outside the
                # contract, and would be RAVEL asking twice for a permission it
                # has been told it does not have.
                retry_reason = (
                    "the run was stopped by a deviation; the contract does not "
                    "permit what was asked, and a retry would ask for it again"
                )
                break

            decision = decide_retry(
                state=snapshot.state,
                failure_class=snapshot.failure_class,
                attempt=attempt,
                allowed_retries=plan.allowed_retries,
            )
            retry_reason = decision.reason
            if decision.verdict is not RetryVerdict.RETRY:
                break
            attempt += 1

        return await workflow.execute_activity(
            "finish_node_run",
            args=[
                plan,
                snapshot.job_id,
                attempts,
                retry_reason,
                snapshot.deviation_id,
            ],
            result_type=RunOutcome,
            start_to_close_timeout=_step(plan),
            retry_policy=_ACTIVITY_RETRY,
        )

    async def _watch(self, plan: RunPlan, snapshot: JobSnapshot) -> JobSnapshot:
        """Follow one job until it ends, a wait runs out, or the deadline does.

        Two clocks bound this loop and they mean different things. The deadline
        covers this attempt and is checked on every pass; it is computed here,
        inside the retry loop, rather than once before it, so each attempt gets
        its own. The external wait is restarted each time the job reports that
        it is blocked, so a lab that answers twenty minutes after a fifty-minute
        compute stage is not timed out for the compute stage's sake.

        A deviation ends the loop wherever it is found, and it is checked at the
        top rather than after the poll for a reason: a job can report one while
        its state is `WAITING_EXTERNAL`, and a wait would otherwise be entered
        for a job that has already been stopped.
        """
        deadline = workflow.now() + timedelta(seconds=plan.deadline_seconds)
        poll = timedelta(seconds=plan.poll_interval_seconds)

        while not snapshot.state.is_terminal:
            if snapshot.deviation_id is not None:
                return snapshot
            if workflow.now() >= deadline:
                return await self._abandon(
                    plan, snapshot, "the run exceeded the deadline it was given"
                )
            if snapshot.state is JobState.WAITING_EXTERNAL:
                snapshot = await self._wait_for_external(plan, snapshot)
                continue
            await workflow.sleep(poll)
            snapshot = await workflow.execute_activity(
                "check_job",
                args=[plan.project_id, snapshot.job_id],
                result_type=JobSnapshot,
                start_to_close_timeout=_step(plan),
                retry_policy=_ACTIVITY_RETRY,
            )
        return snapshot

    async def _wait_for_external(
        self, plan: RunPlan, snapshot: JobSnapshot
    ) -> JobSnapshot:
        """Block on something outside RAVEL, for as long as its own clock allows.

        The signal may already have arrived — a lab can answer between the poll
        that saw the wait and this call — and `wait_condition` checks before it
        waits, so that case needs no special handling here.
        """
        timeout = timedelta(seconds=plan.external_wait_seconds)
        try:
            await workflow.wait_condition(
                lambda: self._external is not None, timeout=timeout
            )
        except TimeoutError:
            return await self._abandon(
                plan,
                snapshot,
                "nothing outside RAVEL answered within the time allowed for a wait",
            )

        result = self._external
        assert result is not None  # the condition just held
        self._external = None
        return await workflow.execute_activity(
            "deliver_external_result",
            args=[plan.project_id, snapshot.job_id, result],
            result_type=JobSnapshot,
            start_to_close_timeout=_step(plan),
            retry_policy=_ACTIVITY_RETRY,
        )

    async def _abandon(
        self, plan: RunPlan, snapshot: JobSnapshot, reason: str
    ) -> JobSnapshot:
        """Give up on a job and record that the wait is what ended it."""
        return await workflow.execute_activity(
            "abandon_job",
            args=[plan.project_id, snapshot.job_id, reason],
            result_type=JobSnapshot,
            start_to_close_timeout=_step(plan),
            retry_policy=_ACTIVITY_RETRY,
        )


def _step(plan: RunPlan) -> timedelta:
    """How long any one activity call in this run may take.

    Carried in the plan rather than read from settings, and read through a
    function rather than stored on the workflow, so that every activity call
    in a run is bounded by the same number and a replay derives the same one.
    """
    return timedelta(seconds=plan.activity_timeout_seconds)


def _summary(
    attempt: int, snapshot: JobSnapshot, began: datetime, ended: datetime
) -> AttemptSummary:
    """One attempt, as the Execution Record will hold it."""
    return AttemptSummary(
        attempt=attempt,
        started_at=began,
        ended_at=ended,
        backend_job_ref=snapshot.backend_job_ref,
        outcome=_TERMINATION[snapshot.state],
        note=snapshot.detail or snapshot.backend_state,
    )
