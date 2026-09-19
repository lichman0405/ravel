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
`request_missing_output` — are not here. They arrive in Phase 6 with the
scenarios that need them, because a port written ahead of its callers is a
port shaped by guesswork.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ravel.domain.enums import FailureClass, JobState, NodeType


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
    #: True when this attempt exists only because the previous one failed
    #: retryably. A backend that treats a retry as a first attempt would, for
    #: example, re-charge a lab for the same run.
    is_retry: bool = False


@dataclass(frozen=True)
class JobHandle:
    """What a backend returns when it accepts work."""

    backend_job_ref: str
    state: JobState
    backend_state: str = ""


@dataclass(frozen=True)
class JobStatus:
    """What a backend says about work when asked.

    `failure_class` is the backend classifying its own failure. A backend that
    cannot classify one reports `None`, and RAVEL reads that as non-retryable —
    the closed list in `acceptance/MOCK_SCENARIOS.yaml` has no "unknown", and
    inventing a third value for it would mean guessing on the backend's behalf.
    """

    state: JobState
    backend_state: str = ""
    failure_class: FailureClass | None = None
    detail: str = ""
    progress: dict[str, object] = field(default_factory=dict)


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
