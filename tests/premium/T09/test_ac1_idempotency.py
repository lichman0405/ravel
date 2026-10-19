"""T09-AC1: one attempt creates at most one job, and an unobserved outcome is UNKNOWN.

Every test here drives the real `SlurmComputeBackend` against
`ScriptedTargetCluster`, which parses the commands the backend builds. No test
in this file contacts a scheduler: `REAL_HPC` is false, and the live check is
recorded as BLOCKED_EXTERNAL in `docs/premium/tasklogs/T09.md`.
"""

from __future__ import annotations

import json
import threading

import pytest
from tests.premium.T09 import support
from tests.premium.T09.support import (
    ATTEMPT,
    CONTRACT_VERSION,
    NODE,
    PASSWORD,
    PROJECT,
    TARGET_A,
    ScriptedTargetCluster,
)

from ravel.actions.idempotency import compute_submission_key
from ravel.backends.slurm import (
    SlurmComputeBackend,
    SlurmSubmissionError,
    SlurmSubmissionUncertain,
    TransportError,
)


def _submission_key() -> str:
    return compute_submission_key(
        project_id=PROJECT,
        node_id=NODE,
        contract_version=CONTRACT_VERSION,
        attempt=ATTEMPT,
    )


def test_the_submission_key_is_t06_s_attempt_identity() -> None:
    backend = support.backend(ScriptedTargetCluster())
    request = support.request()

    key = backend.submission_key_for(request)

    assert key == _submission_key()
    assert key.startswith("sha256:")
    assert backend.digest_for(request) == key.split(":", 1)[1][:16]


def test_two_concurrent_identical_submissions_create_one_job(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    first = support.backend(cluster)
    second = support.backend(cluster)
    request = support.request(workspace=support.workspace(tmp_path))
    barrier = threading.Barrier(3)
    handles: list[object] = []
    errors: list[BaseException] = []

    def submit(backend: SlurmComputeBackend) -> None:
        barrier.wait(timeout=10)
        try:
            handles.append(backend.submit(request))
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=submit, args=(first,)),
        threading.Thread(target=submit, args=(second,)),
    ]
    for thread in threads:
        thread.start()
    barrier.wait(timeout=10)
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert len(handles) == 2
    assert handles[0].backend_job_ref == handles[1].backend_job_ref
    assert len(cluster.jobs) == 1
    assert cluster.sbatch_calls() == 1


def test_a_restarted_worker_adopts_the_record_and_does_not_submit_again(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    request = support.request(workspace=support.workspace(tmp_path))

    first = support.backend(cluster).submit(request)
    # A different backend object, as a worker restart constructs: the only
    # memory between them is the record on the cluster.
    second = support.backend(cluster).submit(request)

    assert first == second
    assert len(cluster.jobs) == 1
    assert cluster.sbatch_calls() == 1
    record = support.read_record(cluster)
    assert record["status"] == "SUBMITTED"
    assert record["job_id"] == first.backend_job_ref.split(":")[2]


def test_a_lost_response_with_a_visible_job_is_adopted(tmp_path) -> None:
    cluster = ScriptedTargetCluster(failures=["lost_but_created"])
    backend = support.backend(cluster)
    request = support.request(workspace=support.workspace(tmp_path))

    handle = backend.submit(request)

    assert cluster.sbatch_calls() == 1
    assert len(cluster.jobs) == 1
    job_id = next(iter(cluster.jobs))
    expected = backend.make_ref(
        job_id, SlurmComputeBackend.digest_for(request), TARGET_A
    )
    assert handle.backend_job_ref == expected
    record = support.read_record(cluster)
    assert record["status"] == "SUBMITTED"
    assert record["job_id"] == job_id


def test_a_lost_response_without_a_visible_job_is_unknown_and_not_resubmitted(
    tmp_path,
) -> None:
    cluster = ScriptedTargetCluster(failures=["lost_no_job"])
    backend = support.backend(cluster)
    request = support.request(workspace=support.workspace(tmp_path))

    with pytest.raises(SlurmSubmissionUncertain) as raised:
        backend.submit(request)

    assert raised.value.state == "UNKNOWN"
    assert raised.value.submission_key == _submission_key()
    assert raised.value.target_id == TARGET_A
    assert cluster.sbatch_calls() == 1
    assert cluster.jobs == {}
    assert support.read_record(cluster)["status"] == "SUBMITTING"

    # The retry re-enters reconciliation. A 3600-second grace window has not
    # passed, so the job may still appear; nothing may be submitted.
    with pytest.raises(SlurmSubmissionUncertain):
        backend.submit(request)
    assert cluster.sbatch_calls() == 1
    assert cluster.jobs == {}


def test_a_restart_from_a_submitting_record_stays_unknown(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    target = support.target(grace_seconds=3600.0)
    request = support.request(workspace=support.workspace(tmp_path))
    support.write_record(cluster, support.intent_record(request, target))

    with pytest.raises(SlurmSubmissionUncertain) as raised:
        support.backend(cluster, targets=[target]).submit(request)

    assert raised.value.state == "UNKNOWN"
    assert cluster.sbatch_calls() == 0
    assert cluster.jobs == {}
    assert support.read_record(cluster)["status"] == "SUBMITTING"


def test_a_probe_failure_is_not_evidence_of_absence(tmp_path) -> None:
    cluster = ScriptedTargetCluster(squeue_name_fails=True, sacct_name_fails=True)
    target = support.target(grace_seconds=0.0)
    request = support.request(workspace=support.workspace(tmp_path))
    support.write_record(
        cluster,
        support.intent_record(request, target, intent_at=support.old_intent_time()),
    )

    with pytest.raises(SlurmSubmissionUncertain) as raised:
        support.backend(cluster, targets=[target]).submit(request)

    assert "did not answer both probes" in str(raised.value)
    assert cluster.sbatch_calls() == 0
    assert cluster.jobs == {}


def test_a_confirmed_absence_after_the_grace_window_submits_once(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    target = support.target(grace_seconds=30.0)
    request = support.request(workspace=support.workspace(tmp_path))
    support.write_record(
        cluster,
        support.intent_record(
            request, target, intent_at=support.old_intent_time(seconds=120.0)
        ),
    )
    backend = support.backend(cluster, targets=[target])

    handle = backend.submit(request)

    assert cluster.sbatch_calls() == 1
    assert len(cluster.jobs) == 1
    record = support.read_record(cluster)
    assert record["status"] == "SUBMITTED"
    assert record["job_id"] == handle.backend_job_ref.split(":")[2]


def test_a_record_for_different_work_is_refused_before_sbatch(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    target = support.target()
    request = support.request(workspace=support.workspace(tmp_path))
    support.write_record(
        cluster,
        support.intent_record(
            request,
            target,
            project_id=support.OTHER_PROJECT,
        ),
    )

    with pytest.raises(SlurmSubmissionError) as raised:
        support.backend(cluster, targets=[target]).submit(request)

    assert not isinstance(raised.value, SlurmSubmissionUncertain)
    assert "belongs to different work" in str(raised.value)
    assert cluster.sbatch_calls() == 0


def test_an_unreadable_record_is_a_transport_fault_not_absence(tmp_path) -> None:
    cluster = ScriptedTargetCluster(record_read_fails=True)

    with pytest.raises(TransportError) as raised:
        support.backend(cluster).submit(support.request(workspace=support.workspace(tmp_path)))

    assert not isinstance(raised.value, SlurmSubmissionUncertain)
    assert "could not be read" in str(raised.value)
    assert cluster.sbatch_calls() == 0


def test_a_malformed_record_is_unknown_not_absence(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    request = support.request(workspace=support.workspace(tmp_path))
    cluster.files[support.record_path()] = b"{not json"

    with pytest.raises(SlurmSubmissionUncertain) as raised:
        support.backend(cluster).submit(request)

    assert "not readable JSON" in str(raised.value)
    assert cluster.sbatch_calls() == 0


def test_an_unknown_record_status_is_unknown(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    target = support.target()
    request = support.request(workspace=support.workspace(tmp_path))
    support.write_record(cluster, support.intent_record(request, target, status="ALIEN"))

    with pytest.raises(SlurmSubmissionUncertain) as raised:
        support.backend(cluster, targets=[target]).submit(request)

    assert "does not understand" in str(raised.value)
    assert cluster.sbatch_calls() == 0


def test_a_scheduler_answer_that_is_not_a_job_id_is_unknown(tmp_path) -> None:
    cluster = ScriptedTargetCluster(failures=["garbage"])
    backend = support.backend(cluster)
    request = support.request(workspace=support.workspace(tmp_path))

    with pytest.raises(SlurmSubmissionUncertain) as raised:
        backend.submit(request)

    assert "instead of a job id" in str(raised.value)
    assert cluster.sbatch_calls() == 1
    assert cluster.jobs == {}

    with pytest.raises(SlurmSubmissionUncertain):
        backend.submit(request)
    assert cluster.sbatch_calls() == 1


def test_a_submitted_record_is_returned_without_asking_the_scheduler(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    target = support.target()
    request = support.request(workspace=support.workspace(tmp_path))
    digest = SlurmComputeBackend.digest_for(request)
    support.write_record(
        cluster,
        support.intent_record(
            request,
            target,
            status="SUBMITTED",
            job_id="4242",
            digest=digest,
            submitted_at="2026-01-01T00:00:00+00:00",
        ),
    )

    handle = support.backend(cluster, targets=[target]).submit(request)

    token = SlurmComputeBackend.target_token(TARGET_A)
    expected = f"slurm:{token}:4242:{digest}"
    assert handle.backend_job_ref == expected
    assert cluster.jobs == {}
    assert cluster.sbatch_calls() == 0


def test_the_record_carries_the_attempt_identity_and_no_secret(tmp_path) -> None:
    cluster = ScriptedTargetCluster(stderr_echo=f"login banner says {PASSWORD}")
    request = support.request(workspace=support.workspace(tmp_path))

    handle = support.backend(cluster).submit(request)

    record = support.read_record(cluster)
    assert record["status"] == "SUBMITTED"
    assert record["submission_key"] == _submission_key()
    assert record["project_id"] == PROJECT
    assert record["node_id"] == NODE
    assert record["attempt"] == ATTEMPT
    assert record["execution_contract_version"] == CONTRACT_VERSION
    assert record["target_id"] == TARGET_A
    assert record["job_id"] == handle.backend_job_ref.split(":")[2]
    assert record["remote_directory"] == f"{support.REMOTE_ROOT}/{PROJECT}/{NODE}/{ATTEMPT}"
    serialized = json.dumps(record)
    assert PASSWORD not in serialized
