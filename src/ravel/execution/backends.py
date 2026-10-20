"""The port a compute or experiment backend is reached through.

`schemas/compute_backend_contract.yaml` and `schemas/experiment_backend_contract.yaml`
describe two vocabularies for the same journey. A compute job is submitted,
runs, and completes; a lab task is created, instructed, and answers — and both
say `wait` means a durable wait, because a worker cannot hold a process open
for two days.

What the durable layer needs from either is the subset below, in RAVEL's own
words, so that the workflow never learns which kind of backend it is talking
to. Each backend maps its own states onto `JobState` and keeps its own word
beside RAVEL's in `JobStatus.backend_state`, which is where a reader can see
the two disagree without opening a table.

The experiment-specific operations — `send_instruction`,
`request_missing_output` — are still not here, and Phase 6 is what settled
that they should not be. A Worker asking a lab for a missing file is a
`WorkerMessage` recorded in RAVEL, not a call into this port: the four
permitted kinds are a closed vocabulary about what a Worker may say, and a
port method per kind would make the vocabulary open again, one backend
implementation at a time. What a backend does is run work and report on it;
what a Worker says is RAVEL's record.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from ravel.domain.enums import FailureClass, JobState, NodeStatus, NodeType


@dataclass(frozen=True)
class JobRequest:
    """Everything a backend is told before it starts.

    `task_spec` is the frozen Execution Contract's content rather than a
    reference to it: a backend that had to read RAVEL's database to find out
    what it was asked to do would be a second reader of authoritative state,
    and one that could read a contract version other than the one it ran under.
    """

    project_id: str
    node_id: str
    attempt: int
    execution_contract_ref: str
    execution_contract_version: int
    objective: str
    task_spec: dict[str, object] = field(default_factory=dict)
    required_outputs: tuple[str, ...] = ()
    #: Where the work happens: the directory preparation built for this
    #: contract version, and the file in it that starts the work. Empty for a
    #: run whose contract named no environment, which is every run that needs
    #: nothing built — a backend that requires one and finds it empty must say
    #: so rather than inventing a directory to work in.
    #:
    #: A backend is told the path rather than handed the files because the
    #: workspace is on the machine that will run the work, and the backend is
    #: the thing that can put it there.
    workspace_path: str = ""
    entrypoint: str = ""
    #: True when this attempt exists only because the previous one failed
    #: retryably. A backend that treats a retry as a first attempt would, for
    #: example, re-charge a lab for the same run.
    is_retry: bool = False
    #: Which operator-published HPC target this request names, for a backend
    #: that has more than one. It is an id from a fixed allowlist, never a
    #: host or a credential: the backend resolves it, in the execution process,
    #: to the endpoint and secret it already holds. Empty keeps the legacy
    #: single-target behaviour.
    target_id: str = ""


@dataclass(frozen=True)
class JobHandle:
    """What a backend returns when it accepts work."""

    backend_job_ref: str
    state: JobState
    backend_state: str = ""


@dataclass(frozen=True)
class DeviationReport:
    """Something a backend says it was asked to do that may be outside its terms.

    A *report*, not a decision, and the distinction is the whole reason this
    type exists. A lab asked for ten bar that can only reach five is not
    refusing — it is telling RAVEL what happened. Whether five bar is permitted
    is a question about the Execution Contract, and the contract belongs to
    RAVEL: a backend that decided for itself would be a second implementation
    of the contract's rules, free to drift from the first and impossible to
    audit against it.

    So a backend says what it was asked for, what it can do, and stops. The
    Worker asks the contract, and the contract's answer is the answer.
    """

    #: What the backend was asked for, in the contract's vocabulary. Always
    #: present: a report that cannot say what it is about cannot be checked.
    requested_action: str = ""
    #: What the backend can do instead, or what it observed. Prose, for a
    #: reader; the checkable parts are the fields below.
    description: str = ""
    #: The parameter whose value is in question, when one is.
    parameter: str = ""
    #: The value the backend can actually reach.
    #:
    #: This is the value checked against the contract, not the one that was
    #: asked for. It is what would be acted on, and a contract that permitted
    #: the request but not the substitute has not permitted anything that
    #: happens.
    value: float | None = None
    #: The substitution the backend was asked about, as `(given, instead)`.
    substitution: tuple[str, str] | None = None
    #: The deviation record this report *is*, when there is already one.
    #:
    #: Most reports are the backend's own observation, and the record of them is
    #: raised by the Worker that adjudicates. A laboratory is different: the
    #: report is a person's, written by the route they reported through, and it
    #: exists before anything adjudicates it. Naming it here is how the Worker
    #: stops a run *against that record* instead of raising a second one for the
    #: same sentence — two rows for one observation, one attributed to the
    #: person and one to the backend, is the sort of duplication that makes an
    #: audit trail read as two events.
    deviation_id: str | None = None


@dataclass(frozen=True)
class JobStatus:
    """What a backend says about work when asked.

    `failure_class` is the backend classifying its own failure. A backend that
    cannot classify one reports `None`, and RAVEL reads that as non-retryable —
    the closed list in `acceptance/MOCK_SCENARIOS.yaml` has no "unknown", and
    inventing a third value for it would mean guessing on the backend's behalf.

    `deviation` is the other thing a job can report: not that it failed, but
    that it was asked for something it may not be permitted to do. It is
    orthogonal to `state` — a job can be RUNNING and report one — because the
    job is still going until somebody rules on the report.
    """

    state: JobState
    backend_state: str = ""
    failure_class: FailureClass | None = None
    detail: str = ""
    progress: dict[str, object] = field(default_factory=dict)
    deviation: DeviationReport | None = None


@dataclass(frozen=True)
class JobOutputs:
    """What a backend hands back when the work is over.

    `delivered_outputs` is deliberately not `artifacts`: the contract names
    required outputs, an artifact is a stored file, and one required output may
    arrive as several artifacts or as none. The completeness check compares
    names against names, so the backend names what it delivered.
    """

    artifacts: tuple[str, ...] = ()
    logs: tuple[str, ...] = ()
    delivered_outputs: tuple[str, ...] = ()
    completion_metadata: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExternalDelivery:
    """Something that happened outside RAVEL, handed to a backend that was waiting.

    A lab answers, an operator confirms, a person uploads a file. The wait
    itself is Temporal's; this is what the backend is told once the wait ends,
    so that its next status report is about the work rather than about the wait.
    """

    summary: str
    detail: str = ""
    delivered_outputs: tuple[str, ...] = ()
    payload: dict[str, object] = field(default_factory=dict)


@runtime_checkable
class WorkBackend(Protocol):
    """Work RAVEL can hand to something that runs it.

    **`submit` must be idempotent in `(project_id, node_id, attempt)`.** This is
    a requirement on every implementor, not a property RAVEL can supply, and it
    is the one thing standing between a worker crash and a duplicated
    experiment.

    RAVEL writes the job row, commits, and *then* calls `submit`. A worker
    killed between the backend accepting the work and the job reference being
    recorded leaves a row that says "submitted, no reference" — and RAVEL
    cannot tell that state apart from "the call never arrived". It resolves the
    ambiguity by calling `submit` again, which is only safe because the key
    identifies the work rather than the request: a backend that recognises
    `(project_id, node_id, attempt)` returns the job it already has, and one
    that does not starts a second experiment.

    `JobRequest` therefore carries those three fields together, and a backend
    that ignores them is not implementing this port.
    """

    #: How this backend is named in `ExecutionRecord.backend` and in every
    #: `BACKEND_STATUS_CHANGED` event. A mock says so in its name, because a
    #: record that came from a mock has to be recognisable as one.
    name: str

    def submit(self, request: JobRequest) -> JobHandle:
        """Accept work and return a reference to it.

        Idempotent in `(project_id, node_id, attempt)`: a second call for the
        same attempt returns the handle for the work already accepted rather
        than starting it again. Returning the same `backend_job_ref` is what
        makes a retried activity resume rather than re-run.
        """
        ...

    def status(self, backend_job_ref: str) -> JobStatus:
        """Report what the job is doing, in RAVEL's vocabulary."""
        ...

    def deliver(self, backend_job_ref: str, delivery: ExternalDelivery) -> JobStatus:
        """Tell a waiting job that what it waited for has happened.

        Returns the state the job is in once the delivery is recorded, so the
        caller does not have to ask again to find out whether it landed.
        """
        ...

    def collect(self, backend_job_ref: str) -> JobOutputs:
        """Hand back artifacts, logs, and which required outputs arrived."""
        ...

    def cancel(self, backend_job_ref: str) -> bool:
        """Ask for the work to stop. Returns whether the backend accepted.

        A backend that refuses is not an error — a lab already at the bench
        cannot un-start an experiment — so the caller records the refusal
        rather than treating it as a fault.
        """
        ...


@dataclass(frozen=True)
class BackendRegistry:
    """Which backend runs which kind of node.

    Explicit rather than discovered: which backend a node type reaches is a
    deployment decision, and a registry that resolved it by import side effect
    would make the answer depend on what happened to be imported.
    """

    by_node_type: dict[NodeType, WorkBackend] = field(default_factory=dict)
    by_name: dict[str, WorkBackend] = field(default_factory=dict)

    def register(self, node_type: NodeType, backend: WorkBackend) -> None:
        """Point a node type at a backend, and make it findable by name."""
        self.by_node_type[node_type] = backend
        self.by_name[backend.name] = backend

    def for_node_type(self, node_type: NodeType) -> WorkBackend:
        """The backend that runs this kind of node.

        Raises:
            KeyError: No backend is registered for it. A node type with no
                backend is a deployment mistake, not a runtime condition to
                paper over — running it would mean inventing a result.
        """
        try:
            return self.by_node_type[node_type]
        except KeyError:
            raise KeyError(
                f"no backend is registered for {node_type.value} nodes; "
                f"registered: {sorted(t.value for t in self.by_node_type) or 'none'}"
            ) from None

    def named(self, name: str) -> WorkBackend:
        """The backend with this name.

        Raises:
            KeyError: No backend is registered under it.
        """
        try:
            return self.by_name[name]
        except KeyError:
            raise KeyError(
                f"no backend named {name!r}; registered: "
                f"{sorted(self.by_name) or 'none'}"
            ) from None


# ── Parallel submission, bounded by quota ───────────────────────────────────
#
# A scheduler accepts many jobs at once; a project's budget does not pay for
# many at once. The coordinator is the small piece of software between the two.
# It decides *which* READY nodes may be submitted now, using limits that were
# chosen elsewhere: the DAG says which nodes are READY and what they depend on,
# Review says whether a dependency was accepted, T06 says how much compute the
# project may spend, and the target's `max_jobs` caps the shared cluster. None
# of those decisions is re-made here.
#
# It is deliberately not an agent and not a durable workflow. It holds one
# worker's view of its own submissions; the authoritative record remains the
# database and the remote submission record. A worker that dies loses this
# object, and the next one rebuilds the same answer from READY nodes and the
# backend's idempotent `submit`.


class DispatchState(StrEnum):
    """What the coordinator did with one READY node in one pass."""

    #: `backend.submit` accepted the work within quota.
    SUBMITTED = "SUBMITTED"
    #: The node was eligible but every slot was busy. It keeps its place; a
    #: later pass after a completion submits it.
    QUEUED = "QUEUED"
    #: The DAG, Review, or the budget did not allow it, or a dependency has not
    #: finished. Nothing was submitted.
    NOT_READY = "NOT_READY"
    #: The node's contract was refused by the backend for a stated reason
    #: (partition, account, software catalog, or a generic rejection).
    REFUSED = "REFUSED"
    #: The backend could not establish whether the job exists. The slot stays
    #: held: an unobservable submission may be a running allocation, and
    #: treating it as free would let quota turn one lost response into two.
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ReadyNode:
    """One node offered to the coordinator, with the facts it schedules on.

    The facts are passed in rather than read here. The coordinator is not a
    reader of authoritative state; the activity that owns the transaction
    supplies what the DAG, Review, and the budget have already decided.
    """

    node_id: str
    request: JobRequest
    #: The node's current DAG status. Only `READY` is eligible.
    status: NodeStatus = NodeStatus.READY
    #: Node ids this node depends on. All must appear in the `completed` set a
    #: dispatch pass is given.
    dependencies: tuple[str, ...] = ()
    #: Whether Review has accepted the work this node depends on. A node whose
    #: dependency is un-reviewed is not allowed to run yet, even if the DAG
    #: arrow exists.
    review_allowed: bool = True
    #: Whether T06's budget check allows another compute action for the
    #: project. This is the budget's answer, not a second budget calculation.
    budget_allowed: bool = True


@dataclass(frozen=True)
class DispatchResult:
    """What happened to one offered node."""

    node_id: str
    state: DispatchState
    handle: JobHandle | None = None
    detail: str = ""

    @property
    def submitted(self) -> bool:
        """Whether a job reference was obtained in this pass."""
        return self.state is DispatchState.SUBMITTED and self.handle is not None


@dataclass
class ParallelSubmissionCoordinator:
    """Submit independent READY nodes up to a hard quota, and no further.

    `quota` is the effective per-project allowance — `parallel_quota` in
    `ravel.backends.slurm.targets` computes `min(T06 project quota, target
    max_jobs)`. Passing the already-decided number keeps this module free of a
    second budget interpretation and of an import cycle.

    The two submission states that consume a slot are `SUBMITTED` and
    `UNKNOWN`. A refusal does not: the scheduler said it did not take the job.
    A node that was never eligible does not. `resolve_uncertain` is the only
    way to give back a slot held for `UNKNOWN`, and it requires the caller to
    say what it observed — a job id, or a confirmed absence after reconciliation
    — so a lost response cannot quietly become free capacity.
    """

    backend: WorkBackend
    quota: int
    #: Node id to the handle a successful submission returned.
    handles: dict[str, JobHandle] = field(default_factory=dict)
    #: Node ids whose submission outcome is unobservable. They hold slots.
    uncertain: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.quota < 1:
            raise ValueError("a parallel submission quota must be at least one")

    @property
    def in_flight(self) -> int:
        """How many slots are held, including unobservable submissions."""
        return len(self.handles) + len(self.uncertain)

    @property
    def available(self) -> int:
        """How many submissions this pass may still make."""
        return max(0, self.quota - self.in_flight)

    def dispatch(
        self,
        candidates: Iterable[ReadyNode],
        *,
        completed: Iterable[str] = (),
    ) -> tuple[DispatchResult, ...]:
        """Offer nodes in order and submit those the quota allows.

        Order is the caller's — typically the DAG's topological order — and is
        preserved in the results. A node past the quota is `QUEUED`, not
        skipped: the caller can submit it in a later pass without recomputing
        eligibility. If a candidate's submit raises something other than a
        stated refusal or `UNKNOWN`, the exception propagates: a transport
        fault is not a scheduling decision, and the durable layer retries it.
        """
        finished = frozenset(completed)
        results: list[DispatchResult] = []
        for candidate in candidates:
            if candidate.request.node_id != candidate.node_id:
                raise ValueError(
                    f"ReadyNode {candidate.node_id!r} carries a request for "
                    f"{candidate.request.node_id!r}; a job submitted under the "
                    "wrong node id would break idempotency"
                )
            results.append(self._dispatch_one(candidate, finished))
        return tuple(results)

    def _dispatch_one(
        self, candidate: ReadyNode, completed: frozenset[str]
    ) -> DispatchResult:
        node_id = candidate.node_id
        if node_id in self.handles:
            return DispatchResult(
                node_id,
                DispatchState.SUBMITTED,
                self.handles[node_id],
                "already submitted in this worker; submit is idempotent",
            )
        if node_id in self.uncertain:
            # Calling submit again could be safe at the backend — it
            # reconciles first — but until that reconciliation has an observed
            # answer the scheduler-side capacity is unknown. The coordinator
            # says so instead of assuming.
            return DispatchResult(
                node_id,
                DispatchState.UNKNOWN,
                None,
                self.uncertain[node_id]
                or "the previous submission outcome was not observable",
            )
        if candidate.status is not NodeStatus.READY:
            return DispatchResult(
                node_id,
                DispatchState.NOT_READY,
                None,
                f"node status is {candidate.status.value}, not READY",
            )
        unmet = sorted(set(candidate.dependencies) - completed)
        if unmet:
            return DispatchResult(
                node_id,
                DispatchState.NOT_READY,
                None,
                "dependencies not complete: " + ", ".join(unmet),
            )
        if not candidate.review_allowed:
            return DispatchResult(
                node_id,
                DispatchState.NOT_READY,
                None,
                "Review has not allowed this node to proceed",
            )
        if not candidate.budget_allowed:
            return DispatchResult(
                node_id,
                DispatchState.NOT_READY,
                None,
                "the project's compute budget does not allow another action",
            )
        if self.available < 1:
            return DispatchResult(
                node_id,
                DispatchState.QUEUED,
                None,
                f"quota {self.quota} is fully in flight",
            )
        try:
            handle = self.backend.submit(candidate.request)
        except Exception as error:
            state = getattr(error, "state", "")
            if state == "UNKNOWN":
                detail = str(error)
                self.uncertain[node_id] = detail
                return DispatchResult(node_id, DispatchState.UNKNOWN, None, detail)
            if state in {
                "REJECTED",
                "PARTITION_REJECTED",
                "ACCOUNT_REJECTED",
                "SOFTWARE_NOT_ALLOWED",
            }:
                return DispatchResult(
                    node_id,
                    DispatchState.REFUSED,
                    None,
                    f"{state}: {error}",
                )
            raise
        self.handles[node_id] = handle
        return DispatchResult(node_id, DispatchState.SUBMITTED, handle)

    def release(self, node_id: str) -> JobHandle | None:
        """Free the slot for a job known to have ended.

        Only a node with a known handle can be released here. An `UNKNOWN`
        node goes through `resolve_uncertain`, because freeing its slot without
        an observation is exactly the assumption AC1 forbids.
        """
        return self.handles.pop(node_id, None)

    def resolve_uncertain(self, node_id: str, *, job_id: str | None = None) -> None:
        """Record what reconciliation observed for an `UNKNOWN` submission.

        `job_id` is the scheduler's id when the job turned out to exist; it
        replaces the held slot with a real handle. `None` means both scheduler
        probes confirmed the submission never happened after the grace period,
        so the slot may be freed. Both outcomes are observations; neither is
        a guess by this object.
        """
        detail = self.uncertain.pop(node_id, None)
        if job_id:
            self.handles[node_id] = JobHandle(
                backend_job_ref=job_id,
                state=JobState.SUBMITTED,
                backend_state="RECONCILED",
            )
        elif detail is None:
            raise KeyError(f"{node_id!r} is not an uncertain submission")


class JoinTimeoutError(RuntimeError):
    """The required dependencies did not end before the join's deadline."""

    state = "JOIN_TIMEOUT"

    def __init__(self, waiting: Iterable[str], *, polls: int = 0) -> None:
        self.waiting = tuple(waiting)
        self.polls = polls
        super().__init__(
            "join did not finish; still waiting for: " + ", ".join(self.waiting)
        )


@dataclass(frozen=True)
class JoinOutcome:
    """The statuses a join waited for, once every required node ended."""

    required: tuple[str, ...]
    statuses: Mapping[str, JobStatus]
    polls: int = 0

    @property
    def ended(self) -> bool:
        """Whether every required job reached a terminal state."""
        return all(status.state.is_terminal for status in self.statuses.values())

    @property
    def completed(self) -> bool:
        """Whether every required job completed successfully."""
        return all(
            status.state is JobState.COMPLETED for status in self.statuses.values()
        )


def join_required(
    backend: WorkBackend,
    handles: Mapping[str, JobHandle | str],
    required: Iterable[str] | None = None,
    *,
    poll_interval: float = 5.0,
    deadline_seconds: float = 3600.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> JoinOutcome:
    """Wait for the required nodes, and only the required nodes, to end.

    A join node names what it needs; unrelated work may still be running when
    it proceeds. Asking the backend about those unrelated jobs would make the
    join wait for work it was not asked about, which is the bug this function
    exists to prevent.

    Terminal is not success. The outcome reports the statuses so the DAG's
    `JoinPolicy` and `FailurePolicy` decide what a failed dependency means;
    `JoinOutcome.completed` is true only when every required job completed.
    A backend that cannot answer propagates its own explicit state — for
    Slurm, `SACCT_DELAY` or `UNKNOWN` — rather than this function inventing a
    status.

    Raises:
        KeyError: A required node has no handle in `handles`.
        JoinTimeoutError: The deadline passed with a required job not terminal.
    """
    names = tuple(required) if required is not None else tuple(handles)
    missing = [name for name in names if name not in handles]
    if missing:
        raise KeyError(
            "no job handle for required node(s): " + ", ".join(sorted(missing))
        )
    started = clock()
    polls = 0
    while True:
        polls += 1
        statuses = {
            name: backend.status(_job_ref(handles[name])) for name in names
        }
        if all(status.state.is_terminal for status in statuses.values()):
            return JoinOutcome(required=names, statuses=statuses, polls=polls)
        if clock() - started >= deadline_seconds:
            waiting = [
                name
                for name, status in statuses.items()
                if not status.state.is_terminal
            ]
            raise JoinTimeoutError(waiting, polls=polls)
        sleep(poll_interval)


def _job_ref(handle: JobHandle | str) -> str:
    """The reference string for a handle, accepting a bare ref for convenience."""
    return handle.backend_job_ref if isinstance(handle, JobHandle) else handle
