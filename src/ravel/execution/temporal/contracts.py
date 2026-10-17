"""What crosses the workflow boundary.

Everything here is serialized into Temporal history and stays readable there
for as long as the workflow can be replayed, which is why none of it is a
domain model. An `ExecutionContract` passed across the boundary would put the
frozen contract in history — fine in itself, but it would also mean that adding
a field to the domain model changes the wire format of workflows that are still
running, and a run that cannot be replayed cannot be recovered.

So the boundary carries the *decisions already made*: this is the plan, this is
what the backend said, this is how the run ended. Each is small enough to read
in a Temporal UI, which is the other reason to keep the domain out of it.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from ravel.domain.enums import (
    CompletenessVerdict,
    FailureClass,
    JobState,
    TerminationStatus,
)
from ravel.domain.preparation import PreparationOutcome, PreparationRefusal


class RunInput(BaseModel):
    """The order to run one attempt at one node."""

    project_id: str
    node_id: str
    attempt: int = Field(ge=1)
    #: Which version of the node's execution contract this run executes, and
    #: therefore which terms its Execution Record is written against. Carried
    #: in the order rather than read when the run begins: `begin_node_run`
    #: checks it against the newest version, so a run whose terms changed
    #: between the order and the start is refused rather than quietly executed
    #: under terms nobody asked for.
    execution_contract_version: int = Field(ge=1)
    #: The role whose contract this run executes, recorded as the actor on
    #: every status report the workflow makes.
    actor_id: str


class RunPlan(BaseModel):
    """What the node is, once the run has begun.

    Produced by `begin_node_run`, which is also what moves the node into
    RUNNING. Everything the workflow needs for the rest of the run is in here,
    so the workflow never re-reads the project: a node whose contract changed
    mid-run would otherwise be able to change what the run is measured against.
    """

    project_id: str
    node_id: str
    display_id: str
    attempt: int
    actor_id: str
    backend: str
    execution_contract_ref: str
    execution_contract_version: int
    objective: str
    required_outputs: tuple[str, ...] = ()
    allowed_retries: int = 0
    task_spec: dict[str, object] = Field(default_factory=dict)
    #: Whether this run's contract names an environment that has to be built
    #: before any work is handed to a backend. It travels in the plan rather
    #: than being re-read because the workflow decides from it whether to ask
    #: for preparation at all, and a workflow that re-read the database to
    #: decide would be reading it again on every replay.
    #:
    #: False is the ordinary case, and it is what keeps a contract that names
    #: no environment running exactly as it did before preparation existed.
    requires_preparation: bool = False
    #: The run limits, carried rather than read. A workflow that read
    #: `Settings` would be reading a file, and a file can change between a run
    #: and its replay — after which the two would disagree about how long to
    #: sleep, and Temporal would refuse the history as non-deterministic.
    #: Travelling in the plan means the numbers a run was governed by are the
    #: numbers in its history.
    poll_interval_seconds: float = 5.0
    deadline_seconds: float = 3600.0
    external_wait_seconds: float = 900.0
    #: How long any single activity call in this run may take before Temporal
    #: considers it lost. It is what bounds recovery after a worker dies: the
    #: server cannot tell a dead worker from a slow one, so a killed activity is
    #: not retried until this expires.
    activity_timeout_seconds: float = 120.0


class JobSnapshot(BaseModel):
    """Where the backend says the job is, after a poll.

    `state` is RAVEL's word and `backend_state` is the backend's own, kept side
    by side so that a disagreement is visible in history rather than being
    resolved silently at the point of reading.
    """

    job_id: str
    backend_job_ref: str | None = None
    state: JobState
    backend_state: str = ""
    failure_class: FailureClass | None = None
    detail: str = ""
    progress: dict[str, object] = Field(default_factory=dict)
    #: Set when this poll found the job reporting something its contract did
    #: not permit, and the deviation has been recorded. The run stops on it:
    #: the work has already been stopped, and what happens to the node next is
    #: Master's decision rather than the run's.
    deviation_id: str | None = None


class ExternalResult(BaseModel):
    """A signal from outside: the thing the job was waiting for has happened.

    Carried into the workflow by signal and then written by an activity, so the
    fact survives the worker that received it. A signal is not durable — it is
    delivered to a running workflow's memory — so a signal whose effect is only
    in memory is a signal lost by the next restart.
    """

    summary: str
    detail: str = ""
    delivered_outputs: tuple[str, ...] = ()
    payload: dict[str, object] = Field(default_factory=dict)


class AttemptSummary(BaseModel):
    """One attempt, as the Execution Record will hold it."""

    attempt: int = Field(ge=1)
    started_at: datetime
    ended_at: datetime
    backend_job_ref: str | None = None
    outcome: TerminationStatus
    note: str = ""


class PreparationReport(BaseModel):
    """What preparing this run's environment produced, or why it could not.

    The activity writes the `PreparationRecord` and, on a refusal, parks the
    node — so this is a report of something already recorded rather than an
    instruction to record it. The workflow reads one field of it: whether the
    run may go ahead.

    The workspace and the entry point are here as well as in the record so
    that a reader of the history can see what was built without opening a
    table, and so that the refusal a run ended on is in the history beside the
    ending rather than only in PostgreSQL.
    """

    preparation_id: str
    outcome: PreparationOutcome
    workspace_path: str = ""
    materializer: str = ""
    materializer_version: str = ""
    required_outputs: tuple[str, ...] = ()
    execution_metadata: dict[str, str] = Field(default_factory=dict)
    refusal: PreparationRefusal | None = None
    reason: str = ""


class RunOutcome(BaseModel):
    """How the run ended, and what the workflow is handing back.

    `retry_reason` is here rather than only in the log because it is the answer
    to "why did this run only once when the backend said the failure was
    retryable", and the person asking that is reading the record, not a
    timeline.

    **A run that ended before it began has no execution to report on.** A
    contract that could not be materialized never handed work to a backend, so
    there is no Execution Record, no termination status, and no completeness
    verdict — `termination_status` and `completeness` are `None` and
    `execution_id` is empty. What it has instead is `preparation_id`, which
    names the refusal Master has to read, and `refusal`, which says which of
    Master's tools the situation calls for. The alternative was to give the
    refusal a termination status, and every member of that vocabulary
    describes an execution that happened.
    """

    node_id: str
    execution_id: str = ""
    termination_status: TerminationStatus | None = None
    completeness: CompletenessVerdict | None = None
    delivered_outputs: tuple[str, ...] = ()
    missing_outputs: tuple[str, ...] = ()
    attempts: tuple[AttemptSummary, ...] = ()
    output_refs: tuple[str, ...] = ()
    log_refs: tuple[str, ...] = ()
    retry_reason: str = ""
    #: The deviation that ended this run, when one did. Present so that whoever
    #: started the run can find what stopped it without reading the record —
    #: and so a caller that sees `DEVIATION` knows which record to open.
    deviation_id: str | None = None
    #: The preparation that refused this run, when one did. Set only on the
    #: ending the fields above cannot describe: nothing executed.
    preparation_id: str | None = None
    refusal: PreparationRefusal | None = None
