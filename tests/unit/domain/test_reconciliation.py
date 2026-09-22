"""What RAVEL is allowed to conclude from a run that is gone.

The vocabulary and one function, tested exhaustively because the function is
where the phase's central rule lives: an infrastructure failure is not a
scientific failure, and the reconciler must be incapable of recording one as
the other. Every combination of liveness and job ending is enumerated here
rather than sampled, so a member added to either vocabulary fails this file
instead of slipping through it.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ravel.domain.clock import utcnow
from ravel.domain.enums import FailureClass, JobState, NodeStatus
from ravel.domain.execution import BackendJob
from ravel.domain.reconciliation import (
    RunFailureClass,
    RunReconciliation,
    WorkflowLiveness,
    classify_run_failure,
)

#: Every liveness the probe can report, in the order `WorkflowLiveness` declares
#: them. Spelled out rather than taken from the enum so that adding a member is
#: a deliberate act here too.
ALL_LIVENESS = (
    WorkflowLiveness.RUNNING,
    WorkflowLiveness.COMPLETED,
    WorkflowLiveness.FAILED,
    WorkflowLiveness.CANCELLED,
    WorkflowLiveness.TERMINATED,
    WorkflowLiveness.TIMED_OUT,
    WorkflowLiveness.NOT_FOUND,
    WorkflowLiveness.UNKNOWN,
)

#: The job endings a lost run can be holding, including none at all. `None`
#: stands for a run that died before `start_job` recorded anything, which is a
#: real case rather than a convenience: the planning activity is the first one
#: that can fail.
JOB_ENDINGS: tuple[tuple[str, JobState | None], ...] = (
    ("no job", None),
    ("submitted", JobState.SUBMITTED),
    ("running", JobState.RUNNING),
    ("waiting", JobState.WAITING_EXTERNAL),
    ("completed", JobState.COMPLETED),
    ("failed retryable", JobState.FAILED),
    ("timed out", JobState.TIMED_OUT),
    ("cancelled", JobState.CANCELLED),
)


def _job(state: JobState | None, *, classified: FailureClass | None = None) -> BackendJob | None:
    """A job in one state, with nothing else about it decided by the caller."""
    if state is None:
        return None
    return BackendJob(
        project_id="p" * 32,
        node_id="n" * 32,
        attempt=1,
        execution_contract_ref="ctr-1",
        execution_contract_version=1,
        backend="mock-compute",
        state=state,
        failure_class=classified,
        ended_at=utcnow() if state.is_terminal else None,
    )


def _classified(state: JobState | None) -> BackendJob | None:
    """A job of one state, carrying the failure class that state allows."""
    if state in (JobState.FAILED, JobState.TIMED_OUT):
        return _job(state, classified=FailureClass.NON_RETRYABLE)
    return _job(state)


# ── The vocabulary ──────────────────────────────────────────────────────────


def test_only_a_running_workflow_is_alive() -> None:
    """The property that decides whether a node is touched at all.

    Narrow on purpose. Every mistake in the other direction — reading a
    finished, terminated or unreachable run as still alive — leaves a node
    stranded, and every mistake in *this* direction ends healthy work. Only one
    of those two is recoverable by a later scan, so the classifier is built to
    err towards asking again.
    """
    assert [item.value for item in ALL_LIVENESS if item.is_alive] == ["RUNNING"]


def test_unknown_is_not_a_finding() -> None:
    """A probe that established nothing is distinguishable from one that did."""
    assert not WorkflowLiveness.UNKNOWN.is_known
    assert [item.value for item in ALL_LIVENESS if not item.is_known] == ["UNKNOWN"]


def test_the_five_classes_the_plan_names_exist() -> None:
    """Phase 11 requires five to be distinguishable, by name."""
    assert {item.value for item in RunFailureClass} == {
        "INFRASTRUCTURE",
        "WORKFLOW_LOST",
        "BACKEND_FAILURE",
        "CANCELLED",
        "SCIENTIFIC",
    }


# ── Classification ──────────────────────────────────────────────────────────


def test_a_live_run_is_not_classified() -> None:
    """A run that is still executing has not failed, whatever a caller wants.

    Raises rather than returning `None`, because a caller that reached here
    with a live run has already decided wrongly somewhere above, and a silent
    answer would let it record that decision.
    """
    with pytest.raises(ValueError, match="has not failed"):
        classify_run_failure(WorkflowLiveness.RUNNING, job=None)


def test_an_unknown_observation_is_not_classified() -> None:
    """An unreachable frontend is not evidence about any particular run."""
    with pytest.raises(ValueError, match="established nothing"):
        classify_run_failure(WorkflowLiveness.UNKNOWN, job=None)


def test_no_classification_is_ever_scientific() -> None:
    """The whole of the phase's central rule, checked exhaustively.

    Every liveness the probe can report, against every job ending a lost run
    can be holding, including the endings a backend classified as
    non-retryable. `SCIENTIFIC` is never the answer, because a run that RAVEL
    lost delivered nothing for anyone to judge — and a reconciler that could
    reach that verdict would be reporting a broken queue as a result.

    The member exists so that the other four can be defined against it. This
    test is what keeps it a definition rather than a value.
    """
    produced = set()
    for observed in ALL_LIVENESS:
        for _name, state in JOB_ENDINGS:
            if observed.is_alive or not observed.is_known:
                with pytest.raises(ValueError):
                    classify_run_failure(observed, job=_classified(state))
                continue
            produced.add(classify_run_failure(observed, job=_classified(state)))
    assert RunFailureClass.SCIENTIFIC not in produced
    assert produced == {
        RunFailureClass.WORKFLOW_LOST,
        RunFailureClass.INFRASTRUCTURE,
        RunFailureClass.BACKEND_FAILURE,
        RunFailureClass.CANCELLED,
    }


def test_every_closed_liveness_has_some_class() -> None:
    """No closed observation falls through to a default.

    `UNKNOWN` and `RUNNING` raise by design; everything else must be named.
    Without this, a liveness Temporal might add would be classified by whatever
    the fallback happened to be.
    """
    for observed in ALL_LIVENESS:
        if observed.is_alive or not observed.is_known:
            continue
        assert classify_run_failure(observed, job=None) in RunFailureClass


def test_nothing_under_the_id_is_a_lost_workflow() -> None:
    """Temporal having no run is the fact that names this whole mechanism."""
    assert (
        classify_run_failure(WorkflowLiveness.NOT_FOUND, job=None)
        is RunFailureClass.WORKFLOW_LOST
    )


def test_a_failed_workflow_is_ravens_own_failure() -> None:
    """Retry exhaustion in an activity is the machinery, not the science.

    This is L-24's own case: `finish_node_run` fails five times and the
    workflow ends FAILED. Nothing was learned, and a class that said otherwise
    would send Master looking for a flaw in a result that was never produced.
    """
    assert (
        classify_run_failure(WorkflowLiveness.FAILED, job=_job(JobState.FAILED))
        is RunFailureClass.INFRASTRUCTURE
    )


def test_a_person_ending_the_run_outranks_the_job() -> None:
    """A terminated or cancelled workflow was stopped, and that is the ending.

    The job underneath it may already have failed, or may be perfectly healthy;
    neither is why the run ended, and letting the job's own failure class win
    here would report a job fault as the cause of somebody's decision.
    """
    for observed in (WorkflowLiveness.TERMINATED, WorkflowLiveness.CANCELLED):
        for _name, state in JOB_ENDINGS:
            assert (
                classify_run_failure(observed, job=_classified(state))
                is RunFailureClass.CANCELLED
            )


def test_a_backends_own_ending_is_quoted_rather_than_overwritten() -> None:
    """The job is the only party that observed the work.

    A job that ended carrying a failure class is the backend's report, and the
    run dying before RAVEL wrote the report down is downstream of it. The class
    says the backend failed and the job keeps its own reason, which is where a
    reader looks for whether it was retryable.
    """
    assert (
        classify_run_failure(
            WorkflowLiveness.FAILED,
            job=_job(JobState.FAILED, classified=FailureClass.NON_RETRYABLE),
        )
        is RunFailureClass.BACKEND_FAILURE
    )
    assert (
        classify_run_failure(
            WorkflowLiveness.NOT_FOUND,
            job=_job(JobState.TIMED_OUT, classified=FailureClass.INFRA_RETRYABLE),
        )
        is RunFailureClass.BACKEND_FAILURE
    )


def test_a_job_that_did_not_fail_does_not_claim_the_run() -> None:
    """Success, a cancellation and a wait are not failures to attribute.

    A job in one of those states is not the backend saying the work failed, so
    the run's own ending is what the class is taken from.
    """
    for state in (JobState.SUBMITTED, JobState.RUNNING, JobState.WAITING_EXTERNAL):
        assert (
            classify_run_failure(WorkflowLiveness.NOT_FOUND, job=_job(state))
            is RunFailureClass.WORKFLOW_LOST
        )
    assert (
        classify_run_failure(WorkflowLiveness.NOT_FOUND, job=_job(JobState.COMPLETED))
        is RunFailureClass.WORKFLOW_LOST
    )


def test_a_completed_workflow_beside_a_live_node_is_ravens_own_fault() -> None:
    """The contradiction, classified rather than skipped.

    A completed run writes its Execution Record and moves its node in one
    transaction, so finding the node still live is impossible — and "impossible"
    is not a reason to leave it stranded. It is RAVEL's machinery failing to
    record an ending it had already reached.
    """
    assert (
        classify_run_failure(WorkflowLiveness.COMPLETED, job=_job(JobState.COMPLETED))
        is RunFailureClass.INFRASTRUCTURE
    )


# ── The record ──────────────────────────────────────────────────────────────


def _record(**overrides: object) -> RunReconciliation:
    fields: dict[str, object] = {
        "project_id": "p" * 32,
        "node_id": "n" * 32,
        "execution_contract_version": 1,
        "workflow_id": f"node-run:{'n' * 32}:v1",
        "observed": WorkflowLiveness.FAILED,
        "failure_class": RunFailureClass.INFRASTRUCTURE,
        "node_status_before": NodeStatus.RUNNING,
        "node_status_after": NodeStatus.WAITING_DECISION,
    }
    fields.update(overrides)
    return RunReconciliation(**fields)  # type: ignore[arg-type]


def test_the_record_is_frozen_and_rejects_unknown_fields() -> None:
    """A reconciliation is evidence of what RAVEL saw, so it cannot be edited.

    The same two properties every record in this project has, asserted here
    because this one is written by a component with no role and no contract,
    and the record is the only thing that makes its act reviewable.
    """
    reconciliation = _record()
    with pytest.raises(ValidationError):
        reconciliation.observed = WorkflowLiveness.RUNNING  # type: ignore[misc]
    with pytest.raises(ValidationError):
        RunReconciliation(**{**_record().model_dump(), "conclusion": "the run failed"})


def test_the_record_names_the_run_by_its_terms() -> None:
    """Version and workflow id together, because that is what re-runs by."""
    reconciliation = _record(execution_contract_version=3)
    assert reconciliation.execution_contract_version == 3
    assert reconciliation.workflow_id.endswith(":v1")


def test_a_reconciliation_never_claims_a_scientific_judgement() -> None:
    """The property a reader can check without reading the classifier."""
    assert not _record().is_a_scientific_judgement
    assert _record(failure_class=RunFailureClass.WORKFLOW_LOST).is_a_scientific_judgement is False
