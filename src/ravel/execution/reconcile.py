"""Finding the runs that are gone, and putting their nodes back in play.

L-24 is the hole this closes: every step of a run after planning is an activity
retried five times, and the last of them — `finish_node_run` — is the one that
writes. If it exhausts its retries the workflow ends as FAILED, PostgreSQL is
left holding a node in RUNNING, and nothing in RAVEL ever asks again. The loop
sees `in_flight`, which is true and useless, and the project cannot reach an
ending while it holds a stranded node.

**What this is.** A deterministic recovery layer, not an agent. It is not in
`NODE_EXECUTOR`, it holds no seat, and it appears in no roster. It exists
because the fact it needs — *is this run still alive* — is one PostgreSQL
cannot derive and only Temporal can answer, and because nothing else in the
control plane reads it. It decides nothing scientific and it starts nothing.

**What it does, exactly.** For a node whose run is gone:

1. Ends the run's job, if it had one and it was still in flight. Nothing will
   report for it, and a job left in `RUNNING` for a run that no longer exists
   is the same lie one layer down.
2. Moves the node to `WAITING_DECISION`, which is the one state that means
   "Master is asked". A node parked there keeps the project unfinished, so the
   project waits for the decision rather than concluding without one.
3. Writes an immutable `RunReconciliation` saying what it saw, in the same
   transaction as both.

**What it deliberately does not do**, and each omission is a decision:

- **It does not write an Execution Record.** That record says what a Worker
  did, and a Worker writes it. A run that never reported did nothing RAVEL can
  attest to. See `ravel.domain.reconciliation`.
- **It does not send the node to REVIEWING.** Review measures a delivered
  result against criteria frozen before the run; a run that produced nothing
  has nothing to measure, and a verdict on an empty result is the conflation
  the plan forbids by name.
- **It does not fail the node.** A node status of `FAILED` is RAVEL saying the
  work failed. Work that never happened did not fail — and `finish_node_run`
  refuses to make that judgement too, sending every ending to Review instead.
  Master decides what a lost run means for the plan.
- **It does not start another run.** Temporal's id carries
  `REJECT_DUPLICATE`, so the same terms cannot run twice; and running work a
  second time is a decision that opens a new node or a new contract version.
- **It does not act on a probe that established nothing.** `UNKNOWN` — a
  frontend that could not be reached, a credential that expired — is not
  evidence that a run is dead, and a reconciler that read it as evidence would
  end a healthy run's node the first time the network hiccuped.

**Which run it asks about is the same run the start path would create.** The
workflow id is `node-run:{node_id}:v{contract_version}` with the version read
the way `TemporalNodeRuns.contract_version` reads it — the newest for the node.
That matters where a Worker stopped, Master revised the contract, and the work
resumed: the previous version's workflow is finished and *should* look dead,
while the node is running perfectly well under the new version. Asking about
the older one would end a healthy run.

**Only the node types a Worker runs.** `WORKER_RUN_NODE_TYPES` is that set, and
it is not the same question as "which nodes are live". A RESEARCH node is
`RUNNING` for as long as its agent is searching, and no workflow was ever
started for it — the Research Agent does that work in its own turn. Ask
Temporal about it and the answer is `NOT_FOUND`, which is true, means nothing,
and would park every research node in the project thirty seconds into its
first search. This was not a hypothetical: the first version of this module
asked about every live node, and the live five-agent certification found it —
three research nodes of one project moved to `WAITING_DECISION` with a
reconciliation each saying their run had been lost. The status alone cannot
tell the two apart, which is why the node's *type* is read rather than inferred
from the state it is in.

**Idempotent, twice over.** The candidate set is derived from nodes in a live
status, so a node this reconciler already moved is not a candidate again; and
`one_reconciliation_per_run` makes a second write for the same run a conflict
that returns the first record rather than a duplicate. A scan that runs on
every supervisor tick is a scan that runs many times over the same rows, and
both guards are load-bearing rather than defensive.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Protocol, runtime_checkable

from sqlalchemy.orm import Session
from temporalio.client import WorkflowExecutionStatus
from temporalio.exceptions import TemporalError
from temporalio.service import RPCError, RPCStatusCode

from ravel.config import Settings
from ravel.domain.clock import utcnow
from ravel.domain.dag import DagNode
from ravel.domain.enums import FailureClass, JobState, NodeStatus
from ravel.domain.execution import BackendJob
from ravel.domain.reconciliation import (
    RunFailureClass,
    RunReconciliation,
    WorkflowLiveness,
    classify_run_failure,
)
from ravel.domain.state_machines import WORKER_RUN_NODE_TYPES
from ravel.execution.temporal.client import NodeRunClient
from ravel.execution.temporal.worker import workflow_id_for
from ravel.state.database import Database
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.reconciliation import RunReconciliationRepository
from ravel.state.repositories.records import BackendJobRepository

logger = logging.getLogger(__name__)

__all__ = ["ExecutionReconciler", "TemporalWorkflowProbe", "WorkflowProbe"]

#: The statuses a node occupies while a run of it is supposed to be moving it.
#: REVIEWING is deliberately not here: a node waiting on Review is waiting on a
#: seat, not on a run, and there is no workflow whose loss could strand it.
#: `NodeStatus` is a closed vocabulary and this is a stated subset of it rather
#: than "everything that is not terminal", because a status added later must be
#: considered here rather than swept up.
LIVE_NODE_STATUSES: frozenset[NodeStatus] = frozenset(
    {NodeStatus.RUNNING, NodeStatus.WAITING_EXTERNAL}
)

#: The one status that means "Master is asked". `Situation.needs_decision`
#: reads it, and a node sitting here keeps its project unfinished, so the
#: project waits on the decision instead of concluding around it.
AWAITING_MASTER = NodeStatus.WAITING_DECISION

#: Temporal's status vocabulary, onto RAVEL's. A module constant for the reason
#: `_LIVENESS_CLASS` is one: a status Temporal adds without a line here is a
#: `KeyError` at the first probe rather than a silent misreading.
_LIVENESS: dict[WorkflowExecutionStatus, WorkflowLiveness] = {
    WorkflowExecutionStatus.RUNNING: WorkflowLiveness.RUNNING,
    # `CONTINUED_AS_NEW` is a live run under a new execution of the same id.
    # Nothing in RAVEL continues a run as new, and if one ever did, the work it
    # represents is still in flight — which is the only thing this mapping is
    # asked. The alternative, reading it as finished, would strand the node.
    WorkflowExecutionStatus.CONTINUED_AS_NEW: WorkflowLiveness.RUNNING,
    WorkflowExecutionStatus.COMPLETED: WorkflowLiveness.COMPLETED,
    WorkflowExecutionStatus.FAILED: WorkflowLiveness.FAILED,
    # Temporal spells it with one L and RAVEL's vocabulary with two. The
    # translation happens here, at the boundary, rather than by renaming a
    # member of either.
    WorkflowExecutionStatus.CANCELED: WorkflowLiveness.CANCELLED,
    WorkflowExecutionStatus.TERMINATED: WorkflowLiveness.TERMINATED,
    WorkflowExecutionStatus.TIMED_OUT: WorkflowLiveness.TIMED_OUT,
}


@runtime_checkable
class WorkflowProbe(Protocol):
    """Asking Temporal whether a run is still there.

    A port rather than a direct call, for the reason every port in this project
    is one: the reconciler's logic is the classification and the recovery, and
    a test of either should not need a Temporal cluster to arrange the case.

    **Implementations do not raise.** A probe that cannot answer says
    `UNKNOWN`, and the reconciler treats that as "do nothing" — so the failure
    mode of a broken probe is an unreconciled node rather than a healthy run
    that was ended by a network fault.
    """

    async def liveness(self, workflow_id: str) -> WorkflowLiveness:
        """What Temporal says about one run, or `UNKNOWN` if it cannot say."""
        ...


@dataclass
class TemporalWorkflowProbe:
    """The real probe: one read of one workflow's description.

    It holds a `NodeRunClient` — the same handle the start path uses, so both
    ends speak to the same namespace — and connects on first use for the reason
    `ExecutionService` does: most ticks of a healthy deployment have nothing to
    ask about, and a supervisor that opened a Temporal connection in order to
    discover that is a connection held for nothing.

    **It reads and does nothing else.** No start, no signal, no terminate: the
    control plane may learn what the data plane is doing and may not join it,
    which is the property `ravel.execution.supervisor` states for itself.
    """

    settings: Settings | None = None
    #: How long one describe may take. A frontend that has stopped answering
    #: must not hold the supervisor's tick open; the answer would be `UNKNOWN`
    #: either way, and a timeout says so sooner.
    timeout: timedelta = timedelta(seconds=10)
    _client: NodeRunClient | None = field(default=None, init=False, repr=False)
    _connecting: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    async def liveness(self, workflow_id: str) -> WorkflowLiveness:
        """Describe one workflow and read its status."""
        try:
            client = await self._connect()
        except (TemporalError, OSError, RuntimeError) as error:
            logger.warning("cannot reach Temporal to check %s: %s", workflow_id, error)
            return WorkflowLiveness.UNKNOWN

        handle = client.client.get_workflow_handle(workflow_id)
        try:
            described = await handle.describe(rpc_timeout=self.timeout)
        except RPCError as error:
            if error.status is RPCStatusCode.NOT_FOUND:
                return WorkflowLiveness.NOT_FOUND
            logger.warning("describing %s failed: %s", workflow_id, error)
            return WorkflowLiveness.UNKNOWN
        except (TemporalError, OSError) as error:
            logger.warning("describing %s failed: %s", workflow_id, error)
            return WorkflowLiveness.UNKNOWN
        if described.status is None:  # pragma: no cover - a description always has one
            return WorkflowLiveness.UNKNOWN
        return _LIVENESS[described.status]

    async def close(self) -> None:
        """Release the client reference, as the start path's service does."""
        self._client = None

    async def _connect(self) -> NodeRunClient:
        """Connect once, and keep it: a probe per node would pay setup per node."""
        if self._client is None:
            async with self._connecting:
                if self._client is None:
                    self._client = await NodeRunClient.connect(self.settings)
        return self._client


@dataclass(frozen=True, slots=True)
class _Candidate:
    """A node in a live status, and the run that is supposed to be moving it.

    Everything here is read from PostgreSQL in one transaction, before anything
    is asked of Temporal. Which version is named is the whole reason this is a
    value rather than three arguments passed around: it is the same version
    `TemporalNodeRuns.contract_version` would read when starting a run, taken
    once so that the probe and the recovery cannot disagree about which run
    they are talking about.
    """

    node: DagNode
    execution_contract_version: int
    workflow_id: str
    job: BackendJob | None


@dataclass(frozen=True, slots=True)
class _Stranded:
    """A candidate Temporal says is not there, with what RAVEL saw.

    Assembled outside the write transaction — the probe is a network call and
    holding a transaction across one is the thing this project does not do —
    so everything is re-read inside it before anything is written.
    """

    candidate: _Candidate
    observed: WorkflowLiveness
    detail: str

    @property
    def node(self) -> DagNode:
        """The node whose run is gone."""
        return self.candidate.node


@dataclass
class ExecutionReconciler:
    """Compares PostgreSQL's live runs against Temporal, and recovers the lost."""

    database: Database
    settings: Settings | None = None
    #: The probe, or `None` to build the Temporal one on first use. A test
    #: hands in its own; a deployment does not have to know this is a port.
    probe: WorkflowProbe | None = None
    #: How long a node must have been in a live status before its run is
    #: questioned. Not a tuning knob for correctness — a run whose workflow has
    #: gone is gone immediately — but a guard against reading a *starting* run
    #: as a lost one, which is the one mistake that would end healthy work.
    grace_seconds: float = 30.0
    #: Recorded on every reconciliation, so a reader can tell which process
    #: found it. The supervisor and a one-off sweep are different actors.
    detected_by: str = "execution-reconciler"
    _fallback: TemporalWorkflowProbe | None = field(default=None, init=False, repr=False)

    async def reconcile(self, project_ids: Iterable[str]) -> list[RunReconciliation]:
        """Recover every stranded run in these projects.

        Returns the records this call wrote. A run reconciled by an earlier
        call is not returned, because it was not written again — the same
        answer a caller gets by reading the table, which is the point.
        """
        written: list[RunReconciliation] = []
        for project_id in project_ids:
            for stranded in await self._stranded(project_id):
                recovered = self._recover(stranded)
                if recovered is None:
                    continue
                written.append(recovered)
                logger.warning(
                    "reconciled a lost run: project=%s node=%s version=%d "
                    "workflow=%s observed=%s class=%s node now=%s",
                    project_id,
                    stranded.node.display_id,
                    stranded.candidate.execution_contract_version,
                    stranded.candidate.workflow_id,
                    stranded.observed.value,
                    recovered.failure_class.value,
                    recovered.node_status_after.value,
                )
        return written

    async def close(self) -> None:
        """Release the probe, if this reconciler owns one."""
        if isinstance(self.probe, TemporalWorkflowProbe):
            await self.probe.close()
        if self._fallback is not None:
            await self._fallback.close()
            self._fallback = None

    # ── Finding them ────────────────────────────────────────────────────────

    async def _stranded(self, project_id: str) -> list[_Stranded]:
        """Every run in one project that Temporal says is not there any more.

        The read is one transaction and the probe is not in it. An agent turn
        is not being held up here, but a network call inside a transaction
        holds a connection and every row the read touched, and a probe that
        waited ten seconds for a frontend would hold them for ten seconds.
        """
        found: list[_Stranded] = []
        for candidate in self._candidates(project_id):
            try:
                observed = await self._probe().liveness(candidate.workflow_id)
            except Exception as error:
                # A probe that raised is a bug in a port whose contract says it
                # does not. It is logged and this node is skipped rather than
                # allowed to abandon the project's other nodes — and it is not
                # read as evidence about the run.
                logger.exception(
                    "probe raised for %s: %s", candidate.workflow_id, error
                )
                continue
            if observed.is_alive or not observed.is_known:
                continue
            found.append(
                _Stranded(
                    candidate=candidate,
                    observed=observed,
                    detail=_observed_detail(observed, candidate.job),
                )
            )
        return found

    def _candidates(self, project_id: str) -> list[_Candidate]:
        """Nodes in a live status whose run is worth asking about.

        The version comes from the node's *newest* contract, which is the rule
        the start path uses, and not from the newest job — see the module
        docstring for the revision case that separates the two.
        """
        with self.database.read_only() as session:
            live = [
                node
                for node in DagRepository(session, project_id).nodes()
                if node.status in LIVE_NODE_STATUSES
                and node.node_type in WORKER_RUN_NODE_TYPES
                and self._past_grace(node)
            ]
            if not live:
                return []
            contracts = ExecutionContractRepository(session, project_id)
            jobs = BackendJobRepository(session, project_id)
            found: list[_Candidate] = []
            for node in live:
                try:
                    contract = contracts.for_node(node.node_id)
                except NotFound:
                    # A node in a live status always has one: `can_enter_running`
                    # refuses without it. If one does not, the run cannot be
                    # named, and guessing a version would probe a workflow id
                    # nobody ever started.
                    logger.error(
                        "node %s is %s with no execution contract; not reconciling",
                        node.display_id,
                        node.status.value,
                    )
                    continue
                found.append(
                    _Candidate(
                        node=node,
                        execution_contract_version=contract.version,
                        workflow_id=workflow_id_for(node.node_id, contract.version),
                        job=jobs.latest_for_version(node.node_id, contract.version),
                    )
                )
            return found

    def _past_grace(self, node: DagNode) -> bool:
        """Whether a node has been live long enough to be questioned.

        A node that started moments ago is not a candidate whatever Temporal
        would say about it, because the window between a node entering RUNNING
        and its first activity writing anything is a window in which a probe
        can only be wrong.
        """
        if self.grace_seconds <= 0 or node.started_at is None:
            return True
        return utcnow() - node.started_at >= timedelta(seconds=self.grace_seconds)

    def _probe(self) -> WorkflowProbe:
        """The probe to ask, building the Temporal one on first need."""
        if self.probe is not None:
            return self.probe
        if self._fallback is None:
            self._fallback = TemporalWorkflowProbe(settings=self.settings)
        return self._fallback

    # ── Recovering them ─────────────────────────────────────────────────────

    def _recover(self, stranded: _Stranded) -> RunReconciliation | None:
        """Put one stranded node back in play, or say it has been done already.

        Everything is re-read inside the write transaction. The probe happened
        outside it, and in between, Master may have cancelled the node or
        Review may have moved it on — in which case this run's loss is history,
        and recovering it would be an edit to a decision somebody already made.
        """
        node = stranded.node
        with self.database.transaction() as session:
            dag = DagRepository(session, node.project_id)
            current = dag.node(node.node_id)
            if current.status not in LIVE_NODE_STATUSES:
                logger.info(
                    "node %s is %s; its lost run is history and nothing is recovered",
                    current.display_id,
                    current.status.value,
                )
                return None

            version = stranded.candidate.execution_contract_version
            reconciliations = RunReconciliationRepository(session, node.project_id)
            already = reconciliations.for_run(node.node_id, version)
            if already is not None:
                return already

            failure_class = classify_run_failure(stranded.observed, job=stranded.candidate.job)
            job_state_after = self._end_job(session, stranded, failure_class)
            status_before = current.status
            self._ask_master(dag, stranded)
            return reconciliations.record(
                RunReconciliation(
                    project_id=node.project_id,
                    node_id=node.node_id,
                    execution_contract_version=version,
                    workflow_id=stranded.candidate.workflow_id,
                    observed=stranded.observed,
                    failure_class=failure_class,
                    node_status_before=status_before,
                    node_status_after=AWAITING_MASTER,
                    job_id=(
                        stranded.candidate.job.job_id
                        if stranded.candidate.job is not None
                        else None
                    ),
                    job_state_before=(
                        stranded.candidate.job.state
                        if stranded.candidate.job is not None
                        else None
                    ),
                    job_state_after=job_state_after,
                    detail=stranded.detail,
                    detected_by=self.detected_by,
                )
            )

    def _end_job(
        self, session: Session, stranded: _Stranded, failure_class: RunFailureClass
    ) -> JobState | None:
        """End the lost run's job, if it had one that had not already ended.

        A job already in a terminal state is left exactly as it is. It may be
        the backend's own report — a `TIMED_OUT` from a wait that ran out, a
        `FAILED` the backend classified — and rewriting it here would put
        RAVEL's guess on top of the only observation anybody made.

        Returns the state the job is in afterwards, which is the state it was
        already in when nothing was changed.
        """
        job = stranded.candidate.job
        if job is None or job.is_terminal:
            return job.state if job is not None else None

        # A cancelled run's job is cancelled, not failed: the constraint is
        # that only a failure carries a failure class, and a job somebody
        # stopped did not fail. Everything else is RAVEL's own machinery giving
        # up on work that will now never be reported, which is what
        # `INFRA_RETRYABLE` says — "a scheduler lost the job".
        cancelled = failure_class is RunFailureClass.CANCELLED
        target = JobState.CANCELLED if cancelled else JobState.FAILED
        stored = BackendJobRepository(session, job.project_id).record_state(
            job.job_id,
            target,
            backend_state=job.backend_state,
            failure_class=None if cancelled else FailureClass.INFRA_RETRYABLE,
            detail=(
                "RAVEL ended this job: the run that held it is gone "
                f"({stranded.observed.value}), so nothing will ever report for it"
            ),
            actor_id=self.detected_by,
        )
        return stored.state

    def _ask_master(self, dag: DagRepository, stranded: _Stranded) -> None:
        """Move the node to where Master is asked, by legal edges only.

        Two transitions rather than one where the node was waiting, and that is
        the DAG's own rule rather than a detour: a wait ends by resuming
        (`NodeStatus`, and `_follow_node_status` in the activities module), and
        `WAITING_EXTERNAL` has no edge to `WAITING_DECISION`. A node that went
        straight from one wait to another would skip the step where the run
        decided what it now had.

        The actor is this reconciler and not a role. Nothing here is a Worker
        reporting, a Master deciding, or Review judging; it is RAVEL noticing,
        and the node's own events say so by naming this in `actor_id`.
        """
        node_id = stranded.node.node_id
        if stranded.node.status is NodeStatus.WAITING_EXTERNAL:
            dag.transition_node(node_id, NodeStatus.RUNNING, actor_id=self.detected_by)
        dag.transition_node(node_id, AWAITING_MASTER, actor_id=self.detected_by)


def _observed_detail(observed: WorkflowLiveness, job: BackendJob | None) -> str:
    """What RAVEL saw, written for a person reading the record later.

    The workflow's status in RAVEL's words, the job's state and its own
    backend's word for it, and — where the backend classified its failure —
    that classification. Nothing here is inferred: every clause is a value some
    other component reported.
    """
    said = f"Temporal reports {observed.value} for this run"
    if job is None:
        return f"{said}; the run had not recorded a job"
    described = f"the job is {job.state.value}"
    if job.backend_state:
        described = f"{described} (the backend said {job.backend_state!r})"
    if job.failure_class is not None:
        described = f"{described}, classified {job.failure_class.value} by the backend"
    return f"{said}; {described}"
