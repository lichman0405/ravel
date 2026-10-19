"""T09-AC3: scoped cancellation and the explicit states of a broken scheduler.

No scheduler or SSH server is contacted. The target states are produced by the
scripted cluster and by a substituted paramiko module; `support.REAL_HPC` is
false, and the live check is BLOCKED_EXTERNAL.
"""

from __future__ import annotations

import sys

import pytest
from tests.premium.T09 import support
from tests.premium.T09.support import (
    OTHER_PROJECT,
    PROJECT,
    TARGET_A,
    TARGET_B,
    ScriptedTargetCluster,
)

from ravel.backends.slurm import (
    HostKeyVerificationError,
    ParamikoTransport,
    SlurmAccountRejected,
    SlurmConfigurationError,
    SlurmPartitionRejected,
    SlurmStateUnknown,
    SlurmSubmissionRejected,
    TransportError,
)
from ravel.domain.enums import JobState


# ── A paramiko stand-in, so host-key policy is testable without a network ────
class _Client:
    def __init__(self, module: _FakeParamiko) -> None:
        self.module = module
        self.policy: object | None = None
        self.loaded: object | None = None
        self.closed = False

    def load_host_keys(self, filename: str) -> None:
        if self.module.load_error is not None:
            raise self.module.load_error
        self.loaded = filename

    def load_system_host_keys(self) -> None:
        self.loaded = "SYSTEM"

    def set_missing_host_key_policy(self, policy: object) -> None:
        self.policy = policy

    def connect(self, **kwargs: object) -> None:
        self.module.connect_kwargs = kwargs
        if self.module.connect_error is not None:
            raise self.module.connect_error

    def open_sftp(self) -> object:
        return object()

    def close(self) -> None:
        self.closed = True


class _FakeParamiko:
    """The small surface `ParamikoTransport.connect` uses, and nothing else."""

    class SSHException(Exception):
        pass

    class BadHostKeyException(SSHException):
        pass

    class RejectPolicy:
        pass

    class AutoAddPolicy:
        pass

    def __init__(self) -> None:
        self.client = _Client(self)
        self.load_error: OSError | None = None
        self.connect_error: BaseException | None = None
        self.connect_kwargs: dict[str, object] = {}
        module = self

        class SSHClient:
            def __new__(cls) -> _Client:
                return module.client

        self.SSHClient = SSHClient


def _scancel_calls(cluster: ScriptedTargetCluster) -> int:
    return sum(1 for command in cluster.commands if command.startswith("scancel "))


def _submitted(cluster: ScriptedTargetCluster, tmp_path):
    backend = support.backend(cluster)
    handle = backend.submit(support.request(workspace=support.workspace(tmp_path)))
    return backend, handle


# ── Cancellation scope ──────────────────────────────────────────────────────
def test_a_same_project_job_is_cancelled(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend, handle = _submitted(cluster, tmp_path)

    outcome = backend.cancel_with_state(handle.backend_job_ref, project_id=PROJECT)

    assert outcome.state == "CANCELLED"
    assert outcome.cancelled
    assert outcome.job_id == handle.backend_job_ref.split(":")[2]
    assert _scancel_calls(cluster) == 1
    assert cluster.jobs[outcome.job_id]["state"] == "CANCELLED"
    assert backend.cancel(handle.backend_job_ref, project_id=PROJECT) is False


def test_a_job_from_another_project_is_not_cancelled(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend, handle = _submitted(cluster, tmp_path)
    job_id = handle.backend_job_ref.split(":")[2]

    outcome = backend.cancel_with_state(handle.backend_job_ref, project_id=OTHER_PROJECT)

    assert outcome.state == "REFUSED_SCOPE"
    assert not outcome.cancelled
    assert "does not match this cancellation's target/project" in outcome.detail
    assert _scancel_calls(cluster) == 0
    assert cluster.jobs[job_id]["state"] == "PENDING"
    assert backend.cancel(handle.backend_job_ref, project_id=OTHER_PROJECT) is False


def test_a_job_from_another_target_is_not_cancelled(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    submitted = support.backend(cluster)
    handle = submitted.submit(
        support.request(workspace=support.workspace(tmp_path))
    )
    job_id = handle.backend_job_ref.split(":")[2]
    digest = handle.backend_job_ref.split(":")[3]
    both_targets = support.backend(
        cluster,
        targets=[support.target(TARGET_A), support.target(TARGET_B)],
        default_target_id=TARGET_A,
    )
    other_target_ref = both_targets.make_ref(job_id, digest, TARGET_B)

    outcome = both_targets.cancel_with_state(other_target_ref, project_id=PROJECT)

    assert outcome.state == "REFUSED_SCOPE"
    assert not outcome.cancelled
    assert _scancel_calls(cluster) == 0
    assert cluster.jobs[job_id]["state"] == "PENDING"


def test_a_job_with_no_submission_record_is_unknown_not_cancelled(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    unknown_ref = backend.make_ref("8123", "0123456789abcdef", TARGET_A)

    outcome = backend.cancel_with_state(unknown_ref, project_id=PROJECT)

    assert outcome.state == "UNKNOWN_JOB"
    assert not outcome.cancelled
    assert "no submission record" in outcome.detail
    assert _scancel_calls(cluster) == 0
    assert backend.cancel(unknown_ref, project_id=PROJECT) is False


def test_a_terminal_job_is_reported_as_already_terminal(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend, handle = _submitted(cluster, tmp_path)
    job_id = handle.backend_job_ref.split(":")[2]
    cluster.jobs[job_id]["state"] = "COMPLETED"

    outcome = backend.cancel_with_state(handle.backend_job_ref, project_id=PROJECT)

    assert outcome.state == "ALREADY_TERMINAL"
    assert not outcome.cancelled
    assert _scancel_calls(cluster) == 0


def test_a_rejected_scancel_is_a_refusal_not_a_fault(tmp_path) -> None:
    cluster = ScriptedTargetCluster(scancel_fails=True)
    backend, handle = _submitted(cluster, tmp_path)

    outcome = backend.cancel_with_state(handle.backend_job_ref, project_id=PROJECT)

    assert outcome.state == "REFUSED"
    assert not outcome.cancelled
    assert "Socket timed out" in outcome.detail
    assert _scancel_calls(cluster) == 1


# ── Stated scheduler refusals ───────────────────────────────────────────────
def test_a_partition_rejection_is_recorded_and_not_asked_again(tmp_path) -> None:
    cluster = ScriptedTargetCluster(sbatch_rejection="partition")
    target = support.target(partition="gpu")
    backend = support.backend(cluster, targets=[target])
    request = support.request(workspace=support.workspace(tmp_path))

    with pytest.raises(SlurmPartitionRejected) as raised:
        backend.submit(request)

    assert raised.value.state == "PARTITION_REJECTED"
    assert "Invalid partition" in str(raised.value)
    assert cluster.sbatch_calls() == 1
    assert cluster.jobs == {}
    assert "--partition='gpu'" in next(
        command for command in cluster.commands if " sbatch " in command
    )
    record = support.read_record(cluster)
    assert record["status"] == "REJECTED"
    assert record["state"] == "PARTITION_REJECTED"

    with pytest.raises(SlurmPartitionRejected) as again:
        backend.submit(request)
    assert again.value.state == "PARTITION_REJECTED"
    assert cluster.sbatch_calls() == 1


def test_an_account_rejection_has_its_own_state(tmp_path) -> None:
    cluster = ScriptedTargetCluster(sbatch_rejection="account")
    target = support.target(account="chem")
    backend = support.backend(cluster, targets=[target])

    with pytest.raises(SlurmAccountRejected) as raised:
        backend.submit(support.request(workspace=support.workspace(tmp_path)))

    assert raised.value.state == "ACCOUNT_REJECTED"
    assert "Invalid account" in str(raised.value)
    assert cluster.jobs == {}
    sbatch = next(command for command in cluster.commands if " sbatch " in command)
    assert "--account='chem'" in sbatch
    record = support.read_record(cluster)
    assert record["status"] == "REJECTED"
    assert record["state"] == "ACCOUNT_REJECTED"


def test_an_unclassified_scheduler_refusal_is_generic(tmp_path) -> None:
    cluster = ScriptedTargetCluster(sbatch_rejection="generic")
    backend = support.backend(cluster)

    with pytest.raises(SlurmSubmissionRejected) as raised:
        backend.submit(support.request(workspace=support.workspace(tmp_path)))

    assert type(raised.value) is SlurmSubmissionRejected
    assert raised.value.state == "REJECTED"
    assert cluster.jobs == {}


def test_an_unconfigured_target_is_refused_before_contact(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    request = support.request(
        target_id="33333333-3333-4333-8333-333333333333",
        workspace=support.workspace(tmp_path),
    )

    with pytest.raises(SlurmConfigurationError, match="not one of this deployment"):
        backend.submit(request)

    assert cluster.jobs == {}
    assert cluster.sbatch_calls() == 0


# ── Accounting delay and absence ────────────────────────────────────────────
def test_a_sacct_delay_is_its_own_state_and_not_a_job_state(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend, handle = _submitted(cluster, tmp_path)
    cluster.jobs[handle.backend_job_ref.split(":")[2]]["state"] = "COMPLETED"
    cluster.sacct_status_fails = True

    with pytest.raises(SlurmStateUnknown) as raised:
        backend.status(handle.backend_job_ref)

    assert raised.value.state == "SACCT_DELAY"
    assert raised.value.job_id == handle.backend_job_ref.split(":")[2]
    assert "answered neither squeue nor sacct" in str(raised.value)


def test_a_job_absent_from_both_views_is_absent_not_success(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend, handle = _submitted(cluster, tmp_path)
    cluster.jobs[handle.backend_job_ref.split(":")[2]]["state"] = "COMPLETED"
    cluster.sacct_status_empty = True

    status = backend.status(handle.backend_job_ref)

    assert status.state is JobState.FAILED
    assert status.backend_state == "ABSENT"
    assert "neither the queue nor the accounting log" in status.detail


# ── Host-key verification ──────────────────────────────────────────────────
def test_an_unknown_host_is_refused_never_auto_accepted(monkeypatch) -> None:
    fake = _FakeParamiko()
    fake.connect_error = fake.SSHException(
        "Server 'hpc.example.edu' not found in known_hosts"
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake)

    with pytest.raises(HostKeyVerificationError) as raised:
        ParamikoTransport.connect(
            host="hpc.example.edu",
            username="scientist",
            password="not-printed",
            known_hosts_filename="/etc/ravel/known_hosts",
            strict_host_keys=True,
        )

    assert raised.value.state == "HOST_KEY_UNKNOWN"
    assert "verify its fingerprint out of band" in str(raised.value)
    assert fake.client.policy.__class__ is fake.RejectPolicy
    assert not isinstance(fake.client.policy, fake.AutoAddPolicy)
    assert "not-printed" not in str(raised.value)


def test_a_changed_host_key_is_a_mismatch_not_an_unknown_host(monkeypatch) -> None:
    fake = _FakeParamiko()
    fake.connect_error = fake.BadHostKeyException(
        "the presented key differs from the recorded one"
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake)

    with pytest.raises(HostKeyVerificationError) as raised:
        ParamikoTransport.connect(
            host="hpc.example.edu",
            username="scientist",
            known_hosts_filename="/etc/ravel/known_hosts",
            strict_host_keys=True,
        )

    assert raised.value.state == "HOST_KEY_MISMATCH"
    assert "does not match the one recorded" in str(raised.value)


def test_a_missing_known_hosts_file_refuses_the_connection(monkeypatch) -> None:
    fake = _FakeParamiko()
    fake.load_error = FileNotFoundError(
        "No such file or directory: '/etc/ravel/known_hosts'"
    )
    monkeypatch.setitem(sys.modules, "paramiko", fake)

    with pytest.raises(HostKeyVerificationError) as raised:
        ParamikoTransport.connect(
            host="hpc.example.edu",
            username="scientist",
            known_hosts_filename="/etc/ravel/known_hosts",
            strict_host_keys=True,
        )

    assert raised.value.state == "HOST_KEY_UNKNOWN"
    assert "could not be read" in str(raised.value)
    assert fake.connect_kwargs == {}


def test_strict_host_keys_overrides_trust_unknown_host(monkeypatch) -> None:
    fake = _FakeParamiko()
    monkeypatch.setitem(sys.modules, "paramiko", fake)

    ParamikoTransport.connect(
        host="hpc.example.edu",
        username="scientist",
        known_hosts_filename="/etc/ravel/known_hosts",
        trust_unknown_host=True,
        strict_host_keys=True,
    )

    assert fake.client.policy.__class__ is fake.RejectPolicy


def test_an_authentication_fault_is_a_transport_error(monkeypatch) -> None:
    fake = _FakeParamiko()
    fake.connect_error = fake.SSHException("Authentication failed")
    monkeypatch.setitem(sys.modules, "paramiko", fake)

    with pytest.raises(TransportError) as raised:
        ParamikoTransport.connect(
            host="hpc.example.edu",
            username="scientist",
            known_hosts_filename="/etc/ravel/known_hosts",
            strict_host_keys=True,
        )

    assert not isinstance(raised.value, HostKeyVerificationError)
    assert "could not connect to scientist@hpc.example.edu" in str(raised.value)
