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


class RunInput(BaseModel):
    """The order to run one attempt at one node."""

    project_id: str
    node_id: str
    attempt: int = Field(ge=1)
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


class RunOutcome(BaseModel):
    """How the run ended, and what the workflow is handing back.

    `retry_reason` is here rather than only in the log because it is the answer
    to "why did this run only once when the backend said the failure was
    retryable", and the person asking that is reading the record, not a
    timeline.
    """

    execution_id: str
    node_id: str
    termination_status: TerminationStatus
    completeness: CompletenessVerdict
    delivered_outputs: tuple[str, ...] = ()
    missing_outputs: tuple[str, ...] = ()
    attempts: tuple[AttemptSummary, ...] = ()
    output_refs: tuple[str, ...] = ()
    log_refs: tuple[str, ...] = ()
    retry_reason: str = ""
