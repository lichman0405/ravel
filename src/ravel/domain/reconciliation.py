"""A run that died without reporting, and what RAVEL did about it.

Temporal owns one fact PostgreSQL cannot derive: **whether a run is still
alive**. RAVEL's own state says a node is RUNNING, which is true of a run in
flight and equally true of a run whose workflow failed an hour ago — L-24 is
that sentence, and this module is the other half of it.

Three things live here, and they are separate on purpose:

- `WorkflowLiveness` — what Temporal said, translated into one closed
  vocabulary. `UNKNOWN` is a member because a probe that could not reach the
  frontend must be distinguishable from a probe that was told the run is gone.
- `RunFailureClass` — *whose* failure ended the run. The plan requires five to
  be distinguishable, and the fifth, `SCIENTIFIC`, exists so that the other
  four can be told apart from it. `classify_run_failure` never returns it. A
  run that produced a result is Review's to judge, and a run that RAVEL lost
  produced no result at all; recording either as the other is the mistake this
  vocabulary is shaped to prevent.
- `RunReconciliation` — the immutable record that the recovery happened, so
  that "RAVEL found this run dead" is a fact in the table rather than an
  inference from a node that quietly changed status.

**Why this is not an Execution Record.** An Execution Record says what a Worker
did, and it is a Worker's act in every other path — `finish_node_run` writes
one, once, and a node has one per run. A run that never reported did no work
RAVEL can attest to: no backend was asked to collect anything, no outputs were
delivered anywhere. Writing an Execution Record for it would put a Worker's
name on a statement no Worker made. What is true is narrower and is what this
record says: RAVEL looked, the run was gone, and here is what it saw.

**Why this is not a Deviation Record either.** A deviation is a Worker asking
for something its contract did not permit, and Master answers it through
`resolve_deviation`, whose revision path requires the new terms to permit the
action that was requested. A lost run requested nothing, so there is no action
for a revised contract to permit and the resolution vocabulary does not fit.
Master's answer to a lost run is a decision about the plan, not the terms.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import JobState, NodeStatus
from ravel.domain.execution import BackendJob
from ravel.domain.ids import new_id


class WorkflowLiveness(StrEnum):
    """What Temporal says about a run, in words the reconciler can act on.

    `NOT_FOUND` is Temporal's answer for a workflow id it has never seen *and*
    for one whose closed history has passed its retention window. They are the
    same fact from RAVEL's side — there is no run under this id that can still
    move a node — and the vocabulary does not try to separate them, because
    nothing RAVEL can do differs between them.

    `CANCELLED` is spelled with two Ls to match Temporal's own
    `WorkflowExecutionStatus`, which uses one; the mapping between them lives
    in the probe rather than being smeared across this vocabulary.

    `UNKNOWN` is the member that keeps the reconciler honest. A frontend that
    could not be reached, a credential that expired, a namespace that was
    renamed: none of those is evidence that a run is dead, and a reconciler
    that treated them as evidence would end a healthy run's node the first time
    the network hiccuped. Nothing acts on `UNKNOWN`.
    """

    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    TERMINATED = "TERMINATED"
    TIMED_OUT = "TIMED_OUT"
    NOT_FOUND = "NOT_FOUND"
    UNKNOWN = "UNKNOWN"

    @property
    def is_alive(self) -> bool:
        """Whether a run under this id can still move its node on its own.

        Only `RUNNING`. A closed workflow cannot move anything, and RAVEL does
        not ask which ending closed it here: `COMPLETED` beside a node that is
        still in a live status is an anomaly the reconciler treats like any
        other strand, for the reason given on `_LIVENESS_CLASS`.

        This is the property that decides whether the reconciler touches a
        node at all, and it is deliberately the narrow one. Every mistake in
        the other direction — acting on a run that was merely unreachable, or
        slow, or one namespace away from being found — ends a healthy run.
        """
        return self is WorkflowLiveness.RUNNING

    @property
    def is_known(self) -> bool:
        """Whether the probe actually established anything."""
        return self is not WorkflowLiveness.UNKNOWN


class RunFailureClass(StrEnum):
    """Whose failure ended a run, from the closed list Phase 11 fixes.

    The distinction exists because the four are four different next steps, and
    because conflating the first three with the last is how RAVEL would report
    a broken queue as a scientific result.

    - `WORKFLOW_LOST` — Temporal has no run under the id. Nothing is executing
      and nothing will: the record of the work is gone, not the work.
    - `INFRASTRUCTURE` — the workflow itself ended badly: it exhausted the
      retries of an activity, or the wait it was given ran out. RAVEL's own
      machinery failed. Nothing was learned about the science.
    - `BACKEND_FAILURE` — the backend reported an ending for the job and the
      run died before RAVEL could write it down. The machine that ran the work
      is the party that observed the failure, and its answer is in the job's
      own `failure_class`, which is preserved.
    - `CANCELLED` — the run was terminated or cancelled from outside. Somebody
      stopped it, and that somebody is not the science.
    - `SCIENTIFIC` — **never assigned here.** It is the fourth of the four
      things a run's ending can mean, and it is in this vocabulary so that the
      other four can be defined against it rather than by omission. A run whose
      result is a scientific failure is a run that *delivered* a result, which
      Review judges against the criteria frozen before it ran; a run that
      RAVEL lost delivered nothing and is not evidence about anything.
      `test_no_reconciliation_is_ever_a_scientific_failure` asserts this.
    """

    WORKFLOW_LOST = "WORKFLOW_LOST"
    INFRASTRUCTURE = "INFRASTRUCTURE"
    BACKEND_FAILURE = "BACKEND_FAILURE"
    CANCELLED = "CANCELLED"
    SCIENTIFIC = "SCIENTIFIC"


#: What each closed liveness means for a run that nothing else has explained.
#: Present as a mapping rather than as branches so that a member added to
#: `WorkflowLiveness` without a class here is a `KeyError` at the first
#: reconciliation rather than a silent default, and so the two vocabularies can
#: be compared in one place.
#:
#: `COMPLETED` is in here, and it is the entry a reader is most likely to
#: question. A completed workflow should never be found beside a node still in
#: a live status: whichever activity wrote the ending wrote the Execution
#: Record and moved the node in the same transaction, so the pair is
#: contradictory. Leaving it out would make that contradiction unclassifiable,
#: and the reconciler's alternative to classifying it is to leave the node
#: stranded — which is the one outcome this phase exists to prevent. So it is
#: classified as what it is: RAVEL's own machinery failed to record an ending
#: it had already reached, and nothing was learned about the science.
_LIVENESS_CLASS: dict[WorkflowLiveness, RunFailureClass] = {
    WorkflowLiveness.NOT_FOUND: RunFailureClass.WORKFLOW_LOST,
    WorkflowLiveness.COMPLETED: RunFailureClass.INFRASTRUCTURE,
    WorkflowLiveness.FAILED: RunFailureClass.INFRASTRUCTURE,
    WorkflowLiveness.TIMED_OUT: RunFailureClass.INFRASTRUCTURE,
    WorkflowLiveness.TERMINATED: RunFailureClass.CANCELLED,
    WorkflowLiveness.CANCELLED: RunFailureClass.CANCELLED,
}


def classify_run_failure(
    observed: WorkflowLiveness, *, job: BackendJob | None
) -> RunFailureClass:
    """Why a run that is no longer alive cannot be left to finish.

    The order of the three tests is the order of the evidence, and it is the
    whole of the judgement this function makes:

    1. **A person ended it.** A terminated or cancelled workflow is a fact with
       an author, and no downstream statement outranks it — a job that had
       already failed before somebody killed the run did not "cause" the
       cancellation.
    2. **The backend reported an ending.** A job that ended carrying a failure
       class is the only party that observed the work itself. That the run then
       died before writing the ending down is downstream of the backend's
       report, and the report is quoted rather than overwritten.
    3. **Temporal has no run, or the run ended badly.** `WORKFLOW_LOST` when
       there is nothing under the id, `INFRASTRUCTURE` when the workflow
       failed or timed out. Both are RAVEL's failures, and RAVEL says so.

    Raises:
        ValueError: The run is alive, or the probe established nothing. Neither
            is a condition to classify: a live run has not failed, and an
            unreachable frontend is not evidence about this one.
    """
    if not observed.is_known:
        raise ValueError(
            f"a run cannot be classified from {observed.value}: the probe "
            "established nothing about it, and guessing here would end a "
            "healthy run's node the first time the frontend was unreachable"
        )
    if observed.is_alive:
        raise ValueError(
            f"a run observed as {observed.value} has not failed; classifying it "
            "would give a name to an ending that has not happened"
        )
    if observed in (WorkflowLiveness.TERMINATED, WorkflowLiveness.CANCELLED):
        return RunFailureClass.CANCELLED
    if job is not None and job.is_terminal and job.failure_class is not None:
        return RunFailureClass.BACKEND_FAILURE
    return _LIVENESS_CLASS[observed]


class RunReconciliation(Record):
    """One recovery: a run RAVEL found dead, and what it did about it.

    Immutable and append-only, like every other record of something that
    happened. It is written in the same transaction as the job's ending and the
    node's move, so a reader never sees a node waiting on Master with no
    explanation of why — and a `one_reconciliation_per_run` constraint keyed by
    `(project_id, node_id, execution_contract_version)` makes a second scan
    find the first record rather than write a second one.

    `node_status_before` and `node_status_after` are both stored rather than
    only the destination, because the pair is what makes the record readable
    without reconstructing the state machine: "RUNNING became
    WAITING_DECISION" says where the node was stranded and where Master was
    asked, and the version names which run's terms were lost.
    """

    reconciliation_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    #: Which version of the node's Execution Contract the lost run executed.
    #: Part of the identity of the run, not decoration: a node whose terms were
    #: revised runs again under a new version, and that run may be lost too.
    execution_contract_version: int = Field(ge=1)
    #: The id the lost run was started under, so a person can ask Temporal
    #: about it by name even though the workflow is no longer there.
    workflow_id: str = Field(min_length=1)
    observed: WorkflowLiveness
    failure_class: RunFailureClass
    node_status_before: NodeStatus
    node_status_after: NodeStatus
    #: The backend's job for the lost run, if it had got as far as one, and the
    #: states it moved between. `None` for both when the run died before
    #: `start_job` recorded anything.
    job_id: str | None = None
    job_state_before: JobState | None = None
    job_state_after: JobState | None = None
    #: What RAVEL saw, in the probe's own words where it had any.
    detail: str = ""
    detected_by: str = "execution-reconciler"
    created_at: datetime = Field(default_factory=utcnow)

    @property
    def is_a_scientific_judgement(self) -> bool:
        """Whether this record claims anything about the science.

        Always false, and here so that the claim is a property a test can read
        rather than a convention a reader has to trust.
        """
        return self.failure_class is RunFailureClass.SCIENTIFIC
