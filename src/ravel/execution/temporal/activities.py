"""The things a node run does to the world.

Every one of these touches PostgreSQL, a backend, or a clock, and every one of
them can be run twice, because Temporal retries an activity whose result was
never recorded. That is not hypothetical: an activity that submits work to a
lab and then dies before returning has *already submitted the work*, and the
retry is the moment RAVEL either does the right thing or pays for the
experiment twice.

The answer is the same everywhere — write the intent to PostgreSQL before
acting on it, and let a uniqueness constraint decide whether this call is the
first. That is why `BackendJobRepository.start` is an insert that conflicts
rather than a lookup followed by an insert.

No activity here mutates the DAG's shape. `DagRepository.transition_node` is
the status path a run reports along, and it is the only DAG write any of this
makes; who may move a node where is enforced there, not here.

**Backend calls happen outside transactions.** A lab backend can take seconds
to answer, and holding a database transaction open across that would hold a
connection and any row locks it had taken for the duration.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session
from temporalio import activity
from temporalio.exceptions import ApplicationError

from ravel.config import Settings
from ravel.domain.contracts import ExecutionContract
from ravel.domain.enums import (
    CompletenessVerdict,
    JobState,
    NodeStatus,
    TerminationStatus,
)
from ravel.domain.execution import (
    BackendJob,
    CompletenessCheck,
    DeviationRecord,
    ExecutionAttempt,
    ExecutionRecord,
    WorkerMessage,
)
from ravel.domain.ids import new_id
from ravel.domain.roles import AgentRole
from ravel.execution.backends import (
    BackendRegistry,
    ExternalDelivery,
    JobOutputs,
    JobRequest,
    JobStatus,
    WorkBackend,
)
from ravel.execution.policies import RunLimits
from ravel.execution.temporal.contracts import (
    AttemptSummary,
    ExternalResult,
    JobSnapshot,
    RunInput,
    RunOutcome,
    RunPlan,
)
from ravel.execution.worker_rules import adjudicate, on_incomplete_delivery
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import RecordRepositories

#: RAVEL's word for a job, mapped to the word an Execution Record uses. Two
#: vocabularies because they answer different questions: `JobState` is where the
#: backend's work got to, `TerminationStatus` is how the execution ended.
_TERMINATION: dict[JobState, TerminationStatus] = {
    JobState.COMPLETED: TerminationStatus.COMPLETED,
    JobState.FAILED: TerminationStatus.FAILED,
    JobState.TIMED_OUT: TerminationStatus.TIMED_OUT,
    JobState.CANCELLED: TerminationStatus.CANCELLED,
}


@dataclass
class NodeRunActivities:
    """The activities one worker process offers, bound to its runtime.

    An object rather than module-level functions, so the database, the
    settings, and the backend registry are injected once and are visible in the
    signature of the thing that uses them.
    """

    settings: Settings
    database: Database
    registry: BackendRegistry

    # ── Beginning ───────────────────────────────────────────────────────────

    @activity.defn
    async def begin_node_run(self, order: RunInput) -> RunPlan:
        """Move the node into RUNNING and describe the run about to happen.

        The move and the plan are one transaction, so a run that is recorded as
        started is a run whose contract was read.

        Idempotent: a retried call finds the node already RUNNING and returns
        the same plan. `transition_node` writes nothing when the status is
        already the target, so the retry does not put a second `NODE_STARTED`
        in the project's stream.

        Raises:
            ApplicationError: The node cannot begin a run. Non-retryable —
                trying again would find the same node.
        """
        with self.database.transaction() as session:
            dag = DagRepository(session, order.project_id)
            node = dag.node(order.node_id)

            if node.status not in (NodeStatus.READY, NodeStatus.RUNNING):
                raise ApplicationError(
                    f"node {node.display_id} is {node.status.value} and cannot begin a "
                    "run; a run begins from READY, or is re-reported from RUNNING",
                    non_retryable=True,
                )

            contract = ExecutionContractRepository(session, order.project_id).for_node(
                order.node_id
            )
            if contract.version != order.execution_contract_version:
                raise ApplicationError(
                    f"this run was ordered under version "
                    f"{order.execution_contract_version} of {node.display_id}'s "
                    f"contract and the newest is version {contract.version}; the "
                    "terms changed between the order and the start, and a run "
                    "that executed terms it was not ordered under would be "
                    "measured against a contract nobody chose",
                    non_retryable=True,
                )
            if not contract.is_frozen:
                raise ApplicationError(
                    f"node {node.display_id}'s execution contract is not frozen; a "
                    "Worker may not act under terms that could still change",
                    non_retryable=True,
                )

            if node.status is NodeStatus.READY:
                dag.transition_node(
                    order.node_id, NodeStatus.RUNNING, actor_id=order.actor_id
                )

            backend = self.registry.for_node_type(node.node_type)
            limits = RunLimits.from_settings(self.settings)
            return RunPlan(
                project_id=order.project_id,
                node_id=order.node_id,
                display_id=node.display_id,
                attempt=order.attempt,
                actor_id=order.actor_id,
                backend=backend.name,
                execution_contract_ref=contract.contract_id,
                execution_contract_version=contract.version,
                objective=contract.objective,
                required_outputs=contract.required_outputs,
                allowed_retries=contract.allowed_retries,
                task_spec=_task_spec(contract),
                poll_interval_seconds=limits.poll_interval.total_seconds(),
                deadline_seconds=limits.deadline.total_seconds(),
                external_wait_seconds=limits.external_wait.total_seconds(),
                activity_timeout_seconds=limits.activity_timeout.total_seconds(),
            )

    # ── Handing work over ───────────────────────────────────────────────────

    @activity.defn
    async def start_job(self, plan: RunPlan, attempt: int) -> JobSnapshot:
        """Record the job in PostgreSQL, then ask the backend to take it on.

        The attempt is an argument rather than a field of the plan, and that is
        not a detail. The plan describes the run and is built once, before the
        first attempt; the attempt is the workflow's loop counter and changes on
        every retry. Reading it from the plan would give every retry the first
        attempt's number, and the uniqueness constraint on
        `(project, node, attempt)` would then hand the retry the *first*
        attempt's row — one experiment recorded twice, and a second experiment
        that never happened.

        The order of the two steps below is the other point: the row is
        committed before the backend is called, so work that exists on a backend
        always has a row saying so. The reverse order leaves a window in which
        an experiment is running and RAVEL has no record of it.

        What the order cannot fix is the window on the *other* side — a worker
        killed after the backend accepted the work and before the reference was
        recorded leaves a row that looks exactly like one whose `submit` never
        arrived. Both are recovered the same way, by calling `submit` again,
        and that is safe only because the port requires `submit` to be
        idempotent in `(project_id, node_id, attempt)`. See `WorkBackend`.
        """
        backend = self.registry.named(plan.backend)
        with self.database.transaction() as session:
            job = RecordRepositories(session, plan.project_id).jobs.start(
                _pending_job(plan, backend, attempt)
            )
        if job.backend_job_ref is not None:
            # A previous call got as far as the backend and recorded what it
            # got back. Asking is the only safe move: a second `submit` would
            # be relying on the backend's idempotency when a plain status read
            # answers the question outright.
            return _snapshot(job, backend.status(job.backend_job_ref))

        handle = backend.submit(_request(plan, attempt))
        with self.database.transaction() as session:
            stored = RecordRepositories(session, plan.project_id).jobs.record_state(
                job.job_id,
                handle.state,
                backend_state=handle.backend_state,
                backend_job_ref=handle.backend_job_ref,
                detail=f"submitted to {backend.name}",
            )
            return _snapshot(stored)

    @activity.defn
    async def check_job(self, project_id: str, job_id: str) -> JobSnapshot:
        """Ask the backend where the job is and record the answer.

        Also the point at which the *node* catches up with the job: a run
        blocked on something outside RAVEL moves the node to WAITING_EXTERNAL,
        and one that resumes moves it back. Those are reports of what happened
        rather than decisions, which is why they are here and not in a Decision
        Record.

        Re-asserting a state writes nothing, so a poll loop does not fill the
        project's stream with the same sentence every few seconds.

        A poll is also where a job can say it was asked for something its
        contract does not permit. That is handled before anything else, because
        its answer decides whether there is any work left to poll.
        """
        with self.database.transaction() as session:
            job = RecordRepositories(session, project_id).jobs.get(job_id=job_id)
        if job.is_terminal or job.backend_job_ref is None:
            # A job that ended while this poll was in flight is the normal case
            # for the poll that follows the one that ended it.
            return _snapshot(job)

        backend = self.registry.named(job.backend)
        status = backend.status(job.backend_job_ref)

        if status.deviation is not None:
            stopped = await self._stop_for_deviation(project_id, job, status, backend)
            if stopped is not None:
                return stopped

        with self.database.transaction() as session:
            stored = RecordRepositories(session, project_id).jobs.record_state(
                job_id,
                status.state,
                backend_state=status.backend_state,
                failure_class=status.failure_class,
                detail=status.detail,
            )
            _follow_node_status(session, stored)
            return _snapshot(stored, status)

    async def _stop_for_deviation(
        self,
        project_id: str,
        job: BackendJob,
        status: JobStatus,
        backend: WorkBackend,
    ) -> JobSnapshot | None:
        """Judge what a backend reported, and stop the work if it is not permitted.

        Returns the snapshot once the run has been stopped, and `None` when the
        contract permits what was reported — in which case the caller carries
        on as if nothing had been said, because nothing needed to be.

        Three things happen and the order is the point:

        1. The **frozen contract version the job is running under** is read —
           `job.execution_contract_version`, not whatever is current. A contract
           revised while this job ran does not govern what this job was asked
           to do, and reading the latest would let a later decision change the
           verdict on an earlier request.
        2. The **work is stopped**, outside any transaction, because a lab can
           take seconds to answer. A bench already running cannot always be
           un-started, so a refusal is an outcome to record rather than an
           error to raise.
        3. The deviation, the Worker's message, and the stopped job are written
           **in one transaction**, so a deviation that exists always has a job
           that was stopped because of it.

        The node is deliberately not moved here. Where it ends up is decided
        once, by `finish_node_run`, after the workflow has settled how the run
        ended — the same rule the rest of the node's status path follows.
        """
        report = status.deviation
        assert report is not None  # the caller checked
        if job.backend_job_ref is None:
            raise ApplicationError(
                f"job {job.job_id} reports a deviation but has no backend reference, "
                "so there is nothing to stop",
                non_retryable=True,
            )

        with self.database.transaction() as session:
            contract = ExecutionContractRepository(session, project_id).for_node(
                job.node_id, version=job.execution_contract_version
            )
        verdict = adjudicate(report, contract)
        if verdict.permitted:
            return None

        accepted = backend.cancel(job.backend_job_ref)
        outcome = "accepted" if accepted else "did not accept"

        with self.database.transaction() as session:
            records = RecordRepositories(session, project_id)
            deviation = records.deviations.raise_(
                DeviationRecord(
                    project_id=project_id,
                    node_id=job.node_id,
                    execution_contract_ref=job.execution_contract_ref,
                    requested_action=verdict.requested_action,
                    description=verdict.reason,
                    permitted=False,
                    raised_by=f"backend:{job.backend}",
                )
            )
            records.messages.record(
                WorkerMessage(
                    project_id=project_id,
                    node_id=job.node_id,
                    kind=verdict.statement.kind,
                    body=verdict.statement.body,
                )
            )
            stored = records.jobs.record_state(
                job.job_id,
                JobState.CANCELLED,
                backend_state=status.backend_state,
                detail=(
                    f"{verdict.reason}; the backend {outcome} the cancellation and "
                    "the work has stopped pending Master's decision"
                ),
            )
            return _snapshot(stored, deviation_id=deviation.deviation_id)

    @activity.defn
    async def deliver_external_result(
        self, project_id: str, job_id: str, result: ExternalResult
    ) -> JobSnapshot:
        """Hand a waiting job the thing it was waiting for.

        The wait itself was Temporal's — a durable timer, so it survived every
        restart in between. This is what makes the result a fact: the signal
        that carried it lived in the workflow's memory, and memory does not
        survive the process that holds it.
        """
        with self.database.transaction() as session:
            job = RecordRepositories(session, project_id).jobs.get(job_id=job_id)
        if job.backend_job_ref is None:
            raise ApplicationError(
                f"job {job_id} has no backend reference, so there is nothing to "
                "deliver an external result to",
                non_retryable=True,
            )

        backend = self.registry.named(job.backend)
        status = backend.deliver(job.backend_job_ref, _delivery(result))
        with self.database.transaction() as session:
            stored = RecordRepositories(session, project_id).jobs.record_state(
                job_id,
                status.state,
                backend_state=status.backend_state,
                failure_class=status.failure_class,
                detail=status.detail,
            )
            _follow_node_status(session, stored)
            return _snapshot(stored, status)

    @activity.defn
    async def abandon_job(self, project_id: str, job_id: str, reason: str) -> JobSnapshot:
        """Stop waiting on a job and record that the wait is what ended it.

        Two things happen, and the order between them is deliberately this one:
        the backend is asked to stop, and whether it agreed is recorded. A
        backend that refuses is not an error — a lab already at the bench
        cannot un-start an experiment — so the refusal is written into the
        detail rather than raised.

        The state is TIMED_OUT rather than CANCELLED. Cancelling is a decision
        somebody makes; this is a wait that ran out, and the Execution Record
        should not attribute it to a person.

        This is the one path that ends a run without the job ever resuming, so
        it moves the node back to RUNNING itself. Without that the node would
        still be WAITING_EXTERNAL when `finish_node_run` ran, and the DAG would
        refuse the move to REVIEWING — correctly, since a wait that has not
        ended is not a run that has.
        """
        with self.database.transaction() as session:
            job = RecordRepositories(session, project_id).jobs.get(job_id=job_id)
        if job.is_terminal:
            return _snapshot(job)

        accepted = False
        if job.backend_job_ref is not None:
            accepted = self.registry.named(job.backend).cancel(job.backend_job_ref)
        outcome = "accepted" if accepted else "did not accept"
        detail = f"{reason}; the backend {outcome} the cancellation"

        with self.database.transaction() as session:
            stored = RecordRepositories(session, project_id).jobs.record_state(
                job_id, JobState.TIMED_OUT, backend_state=job.backend_state, detail=detail
            )
            _follow_node_status(session, stored)
            return _snapshot(stored)

    # ── Ending ──────────────────────────────────────────────────────────────

    @activity.defn
    async def finish_node_run(
        self,
        plan: RunPlan,
        job_id: str,
        attempts: list[AttemptSummary],
        retry_reason: str,
        deviation_id: str | None = None,
    ) -> RunOutcome:
        """Write the Execution Record and move the node on.

        The node goes to REVIEWING whatever the ending was, failure and
        cancellation included. That is the separation of powers rather than a
        shortcut: a Worker reports what its execution did, and Review is the
        role that decides what the result means. A Worker that moved a node to
        FAILED would be judging its own work.

        The one ending that goes elsewhere is a **deviation**. A run stopped
        because the contract did not permit something is not a result to be
        reviewed — there is nothing to measure against the acceptance criteria —
        so the node goes to WAITING_DECISION instead, where Master can answer.
        The Execution Record is still written, because the work that did happen
        is a fact about the project.

        **Where a deviated node goes is read now, not inferred from the
        ending.** Master may have answered the deviation while this run was
        finishing: the deviation is visible to the loop the moment it is
        recorded, and the loop asks Master about it immediately, so an answer
        can arrive in the window between a Worker raising the question and this
        activity writing the node's fate. Recording the question's own
        destination then would park the node at WAITING_DECISION for an answer
        already given — and nothing in RAVEL moves a node out of that state
        except Master answering the deviation, which is no longer open, so
        nobody is asked and the work stops forever. A run that finds its terms
        revised carries the answer out instead, which `_ending_steps` spells as
        the two transitions it is: the parking, and Master's revision resuming
        the work under the new version.

        A node something else has already retired stays retired. If Master
        answered by replacing the work or ending the project, the node is
        CANCELLED by the time this runs; the Execution Record is still written,
        because the cancellation is a statement about the plan and the execution
        is a statement about what happened.

        The node must be RUNNING, not WAITING_EXTERNAL: a run that was blocked on
        something outside RAVEL has already been brought back by whichever
        activity ended the wait, because the DAG's rule is that a wait ends by
        resuming. Reaching here with the node still waiting would mean a wait
        that nobody ended.

        Idempotent: a retried call finds the node already moved on and returns
        the record the first call wrote, so the run does not produce two
        Execution Records.

        Raises:
            ApplicationError: The node moved on without this run, or the job is
                not over. Non-retryable in both cases — neither is a condition
                that time will fix.
        """
        with self.database.transaction() as session:
            records = RecordRepositories(session, plan.project_id)
            job = records.jobs.get(job_id=job_id)
            if job.state not in _TERMINATION:
                raise ApplicationError(
                    f"job {job_id} is {job.state.value} and the run cannot be finished "
                    "while its work is still in flight",
                    non_retryable=True,
                )

            dag = DagRepository(session, plan.project_id)
            node = dag.node(plan.node_id)
            if node.status is not NodeStatus.RUNNING:
                written = [
                    record
                    for record in records.executions.for_node(plan.node_id)
                    if record.execution_contract_version == plan.execution_contract_version
                ]
                if written:
                    return _outcome(written[-1], retry_reason)
                if not node.is_terminal:
                    raise ApplicationError(
                        f"node {node.display_id} is {node.status.value} but the run "
                        "that started it has only just ended; something else has "
                        "moved it",
                        non_retryable=True,
                    )
                # Retired while its run was ending. The run still happened and
                # the record below is what says so; the cancellation was
                # somebody else's statement and stays theirs.

        backend = self.registry.named(plan.backend)
        outputs = (
            backend.collect(job.backend_job_ref) if job.backend_job_ref else JobOutputs()
        )
        record = _execution_record(plan, job, attempts, outputs, deviation_id)
        short = _delivery_message(plan, record)

        with self.database.transaction() as session:
            records = RecordRepositories(session, plan.project_id)
            records.executions.record(record, actor_id=plan.actor_id)
            if short is not None:
                records.messages.record(short)
            dag = DagRepository(session, plan.project_id)
            for artifact_id in record.output_refs:
                # Attaching what the run produced is not a change to the graph:
                # the artifacts are the execution's, and Review reads them from
                # the node it was handed.
                dag.record_artifact(plan.node_id, artifact_id)
            if dag.node(plan.node_id).status is NodeStatus.RUNNING:
                answer = _answer_to(records, deviation_id)
                for step in _ending_steps(
                    record,
                    actor_id=plan.actor_id,
                    deviation=answer,
                    terms_revised=(
                        ExecutionContractRepository(session, plan.project_id)
                        .for_node(plan.node_id)
                        .version
                        > plan.execution_contract_version
                    ),
                ):
                    dag.transition_node(
                        plan.node_id,
                        step.status,
                        actor_id=step.actor_id,
                        decision_ref=step.decision_ref,
                    )
        return _outcome(record, retry_reason)


# ── Building the values that cross between the two layers ───────────────────


def _request(plan: RunPlan, attempt: int) -> JobRequest:
    """The frozen contract's content, as the work being asked for.

    `is_retry` is derived rather than passed in, so it cannot disagree with the
    attempt it describes: work numbered above the run's first attempt has, by
    definition, been tried before.
    """
    return JobRequest(
        project_id=plan.project_id,
        node_id=plan.node_id,
        attempt=attempt,
        execution_contract_ref=plan.execution_contract_ref,
        execution_contract_version=plan.execution_contract_version,
        objective=plan.objective,
        task_spec=plan.task_spec,
        required_outputs=plan.required_outputs,
        is_retry=attempt > plan.attempt,
    )


def _delivery(result: ExternalResult) -> ExternalDelivery:
    """A signal's payload, as what the backend is told."""
    return ExternalDelivery(
        summary=result.summary,
        detail=result.detail,
        delivered_outputs=result.delivered_outputs,
        payload=result.payload,
    )


def _pending_job(plan: RunPlan, backend: WorkBackend, attempt: int) -> BackendJob:
    """The job as recorded before the backend has said anything.

    A fresh `job_id` per attempt, because each attempt is its own job: the table
    is keyed on `(project, node, attempt)`, and an insert that conflicts is how
    a retried activity finds the job it already created rather than making a
    second one.
    """
    return BackendJob(
        job_id=new_id(),
        project_id=plan.project_id,
        node_id=plan.node_id,
        attempt=attempt,
        execution_contract_ref=plan.execution_contract_ref,
        execution_contract_version=plan.execution_contract_version,
        backend=backend.name,
        detail=f"submitting to {backend.name}",
    )


def _snapshot(
    job: BackendJob, status: JobStatus | None = None, *, deviation_id: str | None = None
) -> JobSnapshot:
    """A job as the workflow sees it.

    `deviation_id` is passed rather than read off the job because a deviation
    is not a property of the job — it is a separate record that the job's report
    caused. A snapshot carrying one is how the workflow learns that the run has
    stopped for a reason no job state describes.
    """
    return JobSnapshot(
        job_id=job.job_id,
        backend_job_ref=job.backend_job_ref,
        state=job.state,
        backend_state=job.backend_state,
        failure_class=job.failure_class,
        detail=job.detail,
        progress=dict(status.progress) if status is not None else {},
        deviation_id=deviation_id,
    )


def _task_spec(contract: ExecutionContract) -> dict[str, object]:
    """The contract's content, as what the backend is being asked to do.

    Everything here is already frozen, so the dict a backend receives cannot
    change under it. Project and contract identity are added by `JobRequest`
    rather than repeated here.
    """
    return {
        "procedure": contract.procedure,
        "inputs": list(contract.inputs),
        "parameter_targets": dict(contract.parameter_targets),
        "allowed_ranges": dict(contract.allowed_ranges),
        "allowed_actions": list(contract.allowed_actions),
        "allowed_substitutions": list(contract.allowed_substitutions),
        "stop_conditions": list(contract.stop_conditions),
        "escalation_conditions": list(contract.escalation_conditions),
        "resource_limits": dict(contract.resource_limits),
    }


def _completeness(required: tuple[str, ...], outputs: JobOutputs) -> CompletenessCheck:
    """Whether everything the contract required arrived.

    Not a statement about whether the result is any good — that is Review's
    question, answered against the frozen acceptance criteria. This one is
    mechanical: the contract named outputs, and either they arrived or they did
    not.
    """
    delivered = set(outputs.delivered_outputs)
    missing = tuple(name for name in required if name not in delivered)
    arrived = tuple(name for name in required if name in delivered)
    return CompletenessCheck(
        verdict=(
            CompletenessVerdict.COMPLETE
            if not missing
            else CompletenessVerdict.INCOMPLETE_DELIVERY
        ),
        required_outputs=required,
        delivered_outputs=arrived,
        missing_outputs=missing,
        checked_by="delivery-completeness-check",
    )


def _execution_record(
    plan: RunPlan,
    job: BackendJob,
    attempts: list[AttemptSummary],
    outputs: JobOutputs,
    deviation_id: str | None = None,
) -> ExecutionRecord:
    """The immutable record of what this run did.

    A run that raised a deviation is recorded as ending `DEVIATION` rather than
    as whatever the job's last state happened to be. The job was cancelled to
    stop it, and `CANCELLED` would attribute that to somebody deciding to stop —
    which is true of a person stopping a run and false of this, where the stop
    is a consequence of the contract being silent.
    """
    return ExecutionRecord(
        project_id=plan.project_id,
        node_id=plan.node_id,
        execution_contract_ref=plan.execution_contract_ref,
        execution_contract_version=plan.execution_contract_version,
        backend=job.backend,
        backend_job_ref=job.backend_job_ref,
        attempts=tuple(
            ExecutionAttempt(
                attempt=summary.attempt,
                started_at=summary.started_at,
                ended_at=summary.ended_at,
                backend_job_ref=summary.backend_job_ref,
                outcome=summary.outcome,
                note=summary.note,
            )
            for summary in attempts
        ),
        output_refs=outputs.artifacts,
        log_refs=outputs.logs,
        completeness=_completeness(plan.required_outputs, outputs),
        deviations=(deviation_id,) if deviation_id else (),
        termination_status=(
            TerminationStatus.DEVIATION
            if deviation_id
            else _TERMINATION[job.state]
        ),
        executed_by=plan.actor_id,
        started_at=attempts[0].started_at if attempts else None,
    )


def _answer_to(
    records: RecordRepositories, deviation_id: str | None
) -> DeviationRecord | None:
    """The question this run raised, as it now stands.

    Read from the repository rather than carried in the run's own state,
    because whether it has been answered is a fact about the project and not
    about this execution: Master may have answered while the run was still
    ending. `None` means the run raised no question at all.
    """
    if deviation_id is None:
        return None
    return records.deviations.get(deviation_id=deviation_id)


@dataclass(frozen=True, slots=True)
class EndingStep:
    """One status change the end of a run makes, and whose statement it is."""

    #: Where the node goes.
    status: NodeStatus
    #: Who is saying so. The run for its own ending, Master for a revision.
    actor_id: str
    #: The Decision Record behind a step that carries out a decision.
    decision_ref: str | None = None


def _ending_steps(
    record: ExecutionRecord,
    *,
    actor_id: str,
    deviation: DeviationRecord | None,
    terms_revised: bool,
) -> tuple[EndingStep, ...]:
    """Where the node goes once the run is over, as the steps that get it there.

    Two destinations for an ordinary ending, and the difference is what the
    next role is being asked. Review is asked what a result means; Master is
    asked what to do about a run that stopped because nobody had said it was
    allowed. A deviation has no result to review, so sending it to Review would
    be asking a question with no material to answer it from.

    **A deviation is read as it stands now.** A question that is still open
    parks the node where Master will find it. A question Master has already
    answered — which can happen while the run is still ending — does not, and
    the answer decides instead: terms revised means the work runs again under
    them, and anything else means the answer dealt with the node itself, which
    leaves this transition nothing to say.

    The revised case is **two steps rather than one**, because it is two
    statements and the DAG records transitions one at a time. The run's own
    statement is that it stopped to ask, and where that puts a node is
    WAITING_DECISION; the revision's statement is that the work runs again under
    the new terms, and where *that* puts a node is READY, under Master's
    authority and against Master's Decision Record — not the Worker's. Written
    as one transition it would be the Worker moving a node on the strength of a
    decision it did not make, and there is no status for the pair of statements
    taken together: a node whose run is over and whose terms have changed is
    ready to run again, and RUNNING has no edge to READY because a run that is
    still going is not.

    A deviated node still in RUNNING with its question answered and no revision
    is not reachable through the services (`ReplaceWork` and `Terminate` both
    cancel the node), and REVIEWING is what this returns for it: Review is the
    role that says a run produced nothing.
    """
    if record.termination_status is not TerminationStatus.DEVIATION:
        return (EndingStep(NodeStatus.REVIEWING, actor_id),)
    if deviation is None or deviation.is_open:
        return (EndingStep(NodeStatus.WAITING_DECISION, actor_id),)
    if not terms_revised:
        return (EndingStep(NodeStatus.REVIEWING, actor_id),)
    return (
        EndingStep(NodeStatus.WAITING_DECISION, actor_id),
        EndingStep(
            NodeStatus.READY,
            AgentRole.MASTER.value,
            deviation.resolved_by_decision_ref,
        ),
    )


def _delivery_message(plan: RunPlan, record: ExecutionRecord) -> WorkerMessage | None:
    """What the Worker asks for when required outputs did not arrive.

    Only after a run that *completed*. A run that failed did not leave a file
    behind to chase, and asking for one would put a request in the record for
    something the work never got as far as producing — which reads as an
    operator's oversight rather than as the failure it was.
    """
    if record.termination_status is not TerminationStatus.COMPLETED:
        return None
    if record.delivery_is_complete:
        return None
    statement = on_incomplete_delivery(record.completeness.missing_outputs)
    return WorkerMessage(
        project_id=plan.project_id,
        node_id=plan.node_id,
        kind=statement.kind,
        body=statement.body,
    )


def _outcome(record: ExecutionRecord, retry_reason: str) -> RunOutcome:
    """The workflow's return value, built from the record that was written."""
    return RunOutcome(
        execution_id=record.execution_id,
        node_id=record.node_id,
        termination_status=record.termination_status,
        completeness=record.completeness.verdict,
        delivered_outputs=record.completeness.delivered_outputs,
        missing_outputs=record.completeness.missing_outputs,
        attempts=tuple(
            AttemptSummary(
                attempt=attempt.attempt,
                started_at=attempt.started_at,
                ended_at=attempt.ended_at or attempt.started_at,
                backend_job_ref=attempt.backend_job_ref,
                outcome=attempt.outcome or record.termination_status,
                note=attempt.note,
            )
            for attempt in record.attempts
        ),
        output_refs=record.output_refs,
        log_refs=record.log_refs,
        retry_reason=retry_reason,
        deviation_id=record.deviations[0] if record.deviations else None,
    )


def _follow_node_status(session: Session, job: BackendJob) -> None:
    """Let the node say what the job is doing, and only that.

    Two states, and the node mirrors the job between them: blocked on something
    outside RAVEL is WAITING_EXTERNAL, and anything else is RUNNING.

    **Coming back matters as much as going out.** The DAG's rule is that a wait
    ends by resuming, not by moving straight on (see `NodeStatus`), because a
    node that went from a wait directly to its next state would skip the step
    where the run decided what it now had. So a job that stopped waiting — it
    answered, it failed, it was cancelled, it timed out — puts the node back in
    RUNNING, and `finish_node_run` takes it from there.

    That a job has *ended* is deliberately not a special case. Its wait is over
    like any other, and moving the node to REVIEWING belongs to
    `finish_node_run`, which runs once, after the workflow has decided how the
    run ended.
    """
    target = (
        NodeStatus.WAITING_EXTERNAL
        if job.state is JobState.WAITING_EXTERNAL
        else NodeStatus.RUNNING
    )
    dag = DagRepository(session, job.project_id)
    node = dag.node(job.node_id)
    if node.status not in (NodeStatus.RUNNING, NodeStatus.WAITING_EXTERNAL):
        # The node has moved on without this run — Review has it, or Master
        # stopped it. Re-asserting RUNNING here would undo that.
        return
    dag.transition_node(job.node_id, target, actor_id=f"backend:{job.backend}")
