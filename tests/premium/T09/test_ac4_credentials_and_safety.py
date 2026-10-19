"""T09-AC4: the credential stays in the execution process, and the remote side
is treated as hostile until the workspace has been checked.

No scheduler or SSH server is contacted. `support.REAL_HPC` is false; the
scripted cluster answers the commands the real backend builds, and
`real_hpc_check.py` records the live half as BLOCKED_EXTERNAL.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, cast

import pytest
import run_temporal_worker as worker
from tests.premium.T09 import support
from tests.premium.T09.support import (
    PASSWORD,
    PROJECT,
    TARGET_A,
    TARGET_B,
    ScriptedTargetCluster,
)

from ravel.backends.slurm import (
    REDACTED,
    CredentialResolutionError,
    DisabledHpcTargetError,
    ResolvedCredential,
    SlurmConfigurationError,
    SlurmPartitionRejected,
    SlurmSoftwareNotAllowed,
    SlurmStateUnknown,
    SlurmSubmissionError,
    TargetConfigurationError,
    TargetRegistry,
    UnknownHpcTargetError,
    resolve_credential,
    resolve_known_hosts,
    resolve_target,
    target_from_document,
)
from ravel.backends.slurm.backend import Collected
from ravel.config import Settings
from ravel.execution.backends import JobHandle, JobOutputs

ENV_NAME = "RAVEL_T09_PASSWORD"


def _known_hosts(tmp_path: Path) -> Path:
    path = tmp_path / "known_hosts"
    path.write_text("hpc.example.edu ssh-ed25519 AAAAC3Nza-t09\n", encoding="utf-8")
    return path


def _document(
    *,
    known_hosts: Path | None = None,
    **overrides: object,
) -> dict[str, object]:
    """One schema-shaped target, holding a reference and never a secret."""
    document: dict[str, object] = {
        "target_id": TARGET_A,
        "kind": "SLURM_SSH",
        "host": "hpc.example.edu",
        "port": 22,
        "username": "scientist",
        "known_hosts_ref": "file:/etc/ravel/known_hosts",
        "credential_ref": f"env:{ENV_NAME}",
        "remote_root": support.REMOTE_ROOT,
        "allowed_software": ["raspa", "lammps"],
        "max_jobs": 2,
        "enabled": True,
    }
    if known_hosts is not None:
        document["known_hosts_ref"] = f"file:{known_hosts}"
    document.update(overrides)
    return document


def _write_targets(tmp_path: Path, *documents: dict[str, object]) -> Path:
    path = tmp_path / "targets.json"
    path.write_text(json.dumps(list(documents)), encoding="utf-8")
    return path


def _worker_args(
    targets_file: Path,
    *,
    default: str | None = None,
    jobs_root: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        compute_backend="slurm",
        compute_scenario="slurm",
        slurm_jobs_root=jobs_root,
        slurm_targets_file=str(targets_file),
        slurm_default_target=default,
    )


def _server(
    tmp_path: Path, *, stderr_echo: str = ""
) -> tuple[ScriptedTargetCluster, JobHandle]:
    cluster = ScriptedTargetCluster(stderr_echo=stderr_echo)
    backend = support.backend(cluster)
    handle = backend.submit(support.request(workspace=support.workspace(tmp_path)))
    return cluster, handle


def _job_id(handle: JobHandle) -> str:
    return handle.backend_job_ref.split(":")[2]


def test_no_test_in_this_directory_claims_a_real_cluster() -> None:
    assert support.REAL_HPC is False


# ── Representation and resolution ───────────────────────────────────────────
def test_a_target_and_a_credential_are_redacted_in_their_representations() -> None:
    configured = target_from_document(_document())
    credential = resolve_credential(
        f"env:{ENV_NAME}", environ={ENV_NAME: PASSWORD}
    )

    assert PASSWORD not in repr(configured)
    assert PASSWORD not in repr(credential)
    assert "«redacted»" in repr(credential)
    assert "«redacted»" in repr(ResolvedCredential(password=PASSWORD))
    assert ResolvedCredential(key_filename="/keys/id").password is None


def test_a_target_document_carries_a_reference_and_not_the_secret() -> None:
    document = target_from_document(_document()).document()

    assert document["credential_ref"] == f"env:{ENV_NAME}"
    assert document["known_hosts_ref"] == "file:/etc/ravel/known_hosts"
    serialized = json.dumps(document)
    assert PASSWORD not in serialized
    assert f"env:{ENV_NAME}" in serialized


def test_resolution_happens_in_the_calling_process(tmp_path) -> None:
    hosts = _known_hosts(tmp_path)
    configured = target_from_document(_document(known_hosts=hosts))

    with pytest.raises(CredentialResolutionError, match=ENV_NAME):
        resolve_target(configured, environ={})

    resolved = resolve_target(configured, environ={ENV_NAME: PASSWORD})

    assert resolved.password == PASSWORD
    assert resolved.key_filename is None
    assert resolved.host == "hpc.example.edu"
    assert resolved.username == "scientist"
    assert resolved.known_hosts_filename == str(hosts)
    assert resolved.target_id == TARGET_A
    assert PASSWORD not in repr(resolved)


def test_an_unset_or_empty_credential_names_the_variable_not_a_value() -> None:
    with pytest.raises(CredentialResolutionError) as unset:
        resolve_credential(f"env:{ENV_NAME}", environ={})
    assert f"env:{ENV_NAME} is not set" in str(unset.value)
    assert PASSWORD not in str(unset.value)

    with pytest.raises(CredentialResolutionError) as empty:
        resolve_credential(f"env:{ENV_NAME}", environ={ENV_NAME: ""})
    assert f"env:{ENV_NAME} is not set" in str(empty.value)

    with pytest.raises(CredentialResolutionError, match="empty environment variable"):
        resolve_credential("env:", environ={ENV_NAME: PASSWORD})


def test_credential_files_and_keys_are_read_by_reference(tmp_path) -> None:
    password_file = tmp_path / "password"
    password_file.write_text(PASSWORD + "\nnot part of it\n", encoding="utf-8")
    empty_file = tmp_path / "empty"
    empty_file.write_text("", encoding="utf-8")
    key_file = tmp_path / "id_ed25519"
    key_file.write_text("not really a key, but a file\n", encoding="utf-8")

    from_file = resolve_credential(f"file:{password_file}", environ={})
    assert from_file.password == PASSWORD
    assert PASSWORD not in repr(from_file)

    with pytest.raises(CredentialResolutionError, match="is empty"):
        resolve_credential(f"file:{empty_file}", environ={})

    with pytest.raises(CredentialResolutionError, match="could not be read"):
        resolve_credential(f"file:{tmp_path / 'missing'}", environ={})

    from_key = resolve_credential(f"key:{key_file}", environ={})
    assert from_key.password is None
    assert from_key.key_filename == str(key_file)
    assert resolve_credential(f"keyfile:{key_file}", environ={}).key_filename == str(
        key_file
    )

    with pytest.raises(CredentialResolutionError, match="readable key file"):
        resolve_credential(f"key:{tmp_path / 'missing-key'}", environ={})

    with pytest.raises(CredentialResolutionError, match="not one of"):
        resolve_credential("vault:secret/t09", environ={})


def test_known_hosts_must_be_a_readable_file(tmp_path) -> None:
    hosts = _known_hosts(tmp_path)

    assert resolve_known_hosts(f"file:{hosts}") == str(hosts)

    with pytest.raises(CredentialResolutionError, match="must be file:/path"):
        resolve_known_hosts("env:KNOWN_HOSTS")

    with pytest.raises(CredentialResolutionError, match="readable file"):
        resolve_known_hosts(f"file:{tmp_path / 'missing-known-hosts'}")


# ── The fixed allowlist ─────────────────────────────────────────────────────
def test_the_registry_answers_only_for_ids_it_was_given(tmp_path) -> None:
    hosts = _known_hosts(tmp_path)
    registry = TargetRegistry.from_documents([_document(known_hosts=hosts)])

    assert registry.ids == (TARGET_A,)
    assert registry.target(TARGET_A).host == "hpc.example.edu"
    assert TARGET_A in registry
    assert TARGET_B not in registry

    with pytest.raises(UnknownHpcTargetError, match="not in this deployment"):
        registry.target(TARGET_B)

    disabled = TargetRegistry.from_documents(
        [_document(known_hosts=hosts, enabled=False)]
    )
    with pytest.raises(DisabledHpcTargetError, match="configured but disabled"):
        disabled.target(TARGET_A)
    # A disabled target is not resolved, so a missing credential is not read.
    assert disabled.resolve_enabled(environ={}) == {}


def test_a_duplicate_target_id_is_refused(tmp_path) -> None:
    documents = [_document(), _document()]

    with pytest.raises(TargetConfigurationError, match="more than once"):
        TargetRegistry.from_documents(documents)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"max_job": 2}, "fields the schema does not allow"),
        ({"target_id": "not-a-uuid"}, "must be a UUID"),
        ({"remote_root": "scratch/ravel"}, "absolute POSIX path"),
        ({"remote_root": "/scratch/../escape"}, "contains '..'"),
        ({"partition": "gpu\nscancel 1"}, "single scheduler token"),
        ({"enabled": "yes"}, "must be true or false"),
        ({"max_jobs": 0}, "at least 1"),
        ({"credential_ref": "vault:secret"}, "must start with one of"),
        ({"known_hosts_ref": "env:KNOWN_HOSTS"}, "must start with one of"),
    ],
)
def test_a_document_outside_the_schema_is_refused(overrides, message) -> None:
    with pytest.raises(TargetConfigurationError, match=message):
        target_from_document(_document(**overrides))


def test_a_document_missing_a_required_field_is_refused() -> None:
    document = _document()
    del document["remote_root"]

    with pytest.raises(TargetConfigurationError, match="missing required fields"):
        target_from_document(document)


def test_a_target_file_is_a_list_or_an_object_with_targets(tmp_path) -> None:
    listed = _write_targets(tmp_path, _document())
    assert TargetRegistry.from_path(listed).ids == (TARGET_A,)

    wrapped = tmp_path / "wrapped.json"
    wrapped.write_text(
        json.dumps({"targets": [_document(target_id=TARGET_B)]}), encoding="utf-8"
    )
    assert TargetRegistry.from_path(wrapped).ids == (TARGET_B,)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(TargetConfigurationError, match="not JSON"):
        TargetRegistry.from_path(broken)

    wrong_shape = tmp_path / "wrong.json"
    wrong_shape.write_text(json.dumps({"targets": "one"}), encoding="utf-8")
    with pytest.raises(TargetConfigurationError, match="must be a list"):
        TargetRegistry.from_path(wrong_shape)


# ── What leaves the process ─────────────────────────────────────────────────
def test_a_scheduler_refusal_does_not_echo_the_credential(tmp_path) -> None:
    cluster = ScriptedTargetCluster(
        sbatch_rejection="partition", stderr_echo=f"login banner: {PASSWORD}"
    )
    backend = support.backend(cluster)

    with pytest.raises(SlurmPartitionRejected) as raised:
        backend.submit(support.request(workspace=support.workspace(tmp_path)))

    assert PASSWORD not in str(raised.value)
    assert REDACTED in str(raised.value)
    assert PASSWORD not in repr(backend)
    record = support.read_record(cluster)
    assert PASSWORD not in json.dumps(record)
    assert REDACTED in str(record["detail"])


def test_a_sacct_delay_does_not_echo_the_credential(tmp_path) -> None:
    cluster = ScriptedTargetCluster(stderr_echo=f"login banner: {PASSWORD}")
    backend = support.backend(cluster)
    handle = backend.submit(support.request(workspace=support.workspace(tmp_path)))
    cluster.jobs[_job_id(handle)]["state"] = "COMPLETED"
    cluster.sacct_status_fails = True

    with pytest.raises(SlurmStateUnknown) as raised:
        backend.status(handle.backend_job_ref)

    assert raised.value.state == "SACCT_DELAY"
    assert PASSWORD not in str(raised.value)
    assert REDACTED in str(raised.value)


def test_the_worker_banner_names_the_target_and_never_the_credential(
    tmp_path, monkeypatch
) -> None:
    hosts = _known_hosts(tmp_path)
    targets_file = _write_targets(tmp_path, _document(known_hosts=hosts))
    monkeypatch.setenv(ENV_NAME, PASSWORD)

    banner = worker._describe_compute(Settings(), _worker_args(targets_file))

    assert TARGET_A in banner
    assert "scientist@hpc.example.edu:22" in banner
    assert "default: request must name one" in banner
    assert PASSWORD not in banner
    assert f"env:{ENV_NAME}" not in banner


def test_the_worker_resolves_credentials_only_when_building_the_backend(
    tmp_path, monkeypatch
) -> None:
    hosts = _known_hosts(tmp_path)
    targets_file = _write_targets(tmp_path, _document(known_hosts=hosts))
    monkeypatch.setenv(ENV_NAME, PASSWORD)

    backend = worker._target_mode_backend(
        Settings(), cast(Any, object()), cast(Any, object()), _worker_args(targets_file)
    )

    assert set(backend.targets or {}) == {TARGET_A}
    assert backend.target.password == PASSWORD
    assert backend.target.known_hosts_filename == str(hosts)
    assert PASSWORD not in repr(backend)


def test_the_worker_refuses_a_target_file_with_no_enabled_target(
    tmp_path, monkeypatch
) -> None:
    hosts = _known_hosts(tmp_path)
    targets_file = _write_targets(
        tmp_path, _document(known_hosts=hosts, enabled=False)
    )
    monkeypatch.setenv(ENV_NAME, PASSWORD)

    with pytest.raises(TargetConfigurationError, match="no enabled target"):
        worker._target_mode_backend(
            Settings(), cast(Any, object()), cast(Any, object()), _worker_args(targets_file)
        )


def test_the_worker_refuses_a_missing_credential_and_names_only_the_reference(
    tmp_path, monkeypatch
) -> None:
    hosts = _known_hosts(tmp_path)
    targets_file = _write_targets(tmp_path, _document(known_hosts=hosts))
    monkeypatch.delenv(ENV_NAME, raising=False)

    with pytest.raises(CredentialResolutionError) as raised:
        worker._target_mode_backend(
            Settings(), cast(Any, object()), cast(Any, object()), _worker_args(targets_file)
        )

    assert ENV_NAME in str(raised.value)
    assert PASSWORD not in str(raised.value)


def test_the_worker_refuses_a_default_target_that_is_not_enabled(
    tmp_path, monkeypatch
) -> None:
    hosts = _known_hosts(tmp_path)
    targets_file = _write_targets(tmp_path, _document(known_hosts=hosts))
    monkeypatch.setenv(ENV_NAME, PASSWORD)

    with pytest.raises(TargetConfigurationError, match="slurm-default-target"):
        worker._target_mode_backend(
            Settings(),
            cast(Any, object()),
            cast(Any, object()),
            _worker_args(targets_file, default=TARGET_B),
        )


# ── The remote side is not trusted ──────────────────────────────────────────
@pytest.mark.parametrize(
    ("entrypoint", "message"),
    [
        ("/etc/passwd", "relative POSIX path"),
        ("scripts\\job.slurm", "relative POSIX path"),
        ("sub/../../job.slurm", "empty, '.' or '..' segment"),
        ("", "named no entrypoint"),
    ],
)
def test_an_entrypoint_that_is_not_one_file_in_the_workspace_is_refused(
    tmp_path, entrypoint, message
) -> None:
    cluster = ScriptedTargetCluster()

    with pytest.raises(SlurmSubmissionError, match=message):
        support.backend(cluster).submit(
            support.request(entrypoint=entrypoint, workspace=support.workspace(tmp_path))
        )

    assert cluster.commands == []


def test_an_entrypoint_that_is_a_symlink_is_refused(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    workspace = support.workspace(tmp_path)
    (workspace / "linked.slurm").symlink_to(workspace / "job.slurm")

    with pytest.raises(SlurmSubmissionError, match="passes through a symlink"):
        support.backend(cluster).submit(
            support.request(entrypoint="linked.slurm", workspace=workspace)
        )

    assert cluster.commands == []


def test_a_workspace_holding_a_symlink_is_not_uploaded(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    workspace = support.workspace(tmp_path)
    (workspace / "outside").symlink_to(Path("/etc/hostname"))

    with pytest.raises(SlurmSubmissionError, match="holds symlinks"):
        support.backend(cluster).submit(support.request(workspace=workspace))

    assert cluster.sbatch_calls() == 0
    assert support.read_record(cluster)["status"] == "PREPARING"


@pytest.mark.parametrize(
    ("field", "value"),
    [("project_id", "../escape"), ("node_id", "../escape")],
)
def test_an_identifier_that_is_not_one_path_component_is_refused(
    tmp_path, field, value
) -> None:
    cluster = ScriptedTargetCluster()
    request = support.request(
        workspace=support.workspace(tmp_path), **{field: value}
    )

    with pytest.raises(SlurmSubmissionError, match="not one path component"):
        support.backend(cluster).submit(request)

    assert cluster.commands == []


def test_software_outside_the_target_catalog_is_refused_before_contact(tmp_path) -> None:
    cluster = ScriptedTargetCluster()

    with pytest.raises(SlurmSoftwareNotAllowed) as raised:
        support.backend(cluster).submit(
            support.request(workspace=support.workspace(tmp_path), software="gromacs")
        )

    assert raised.value.state == "SOFTWARE_NOT_ALLOWED"
    assert "raspa, lammps" in str(raised.value)
    assert cluster.commands == []


def test_software_in_the_catalog_is_submitted(tmp_path) -> None:
    cluster = ScriptedTargetCluster()

    support.backend(cluster).submit(
        support.request(workspace=support.workspace(tmp_path), software="raspa")
    )

    assert cluster.sbatch_calls() == 1
    assert support.read_record(cluster)["software"] == "raspa"


# ── Collection facts ────────────────────────────────────────────────────────
def test_missing_metering_is_unknown_and_never_zero(tmp_path) -> None:
    cluster, handle = _server(tmp_path)
    backend = support.backend(cluster)
    job_id = _job_id(handle)
    cluster.jobs[job_id]["state"] = "COMPLETED"
    cluster.suppress_metering = True
    cluster.suppress_alloc_tres = True

    status = backend.status(handle.backend_job_ref)
    facts = backend._schedule_facts(cluster, job_id, target=support.target())

    for key in ("elapsed", "max_rss", "alloc_tres"):
        assert status.progress[key] == "UNKNOWN"
        assert status.progress[key] not in (0, "0")
        assert facts[key] == "UNKNOWN"
        assert facts[key] not in (0, "0")
    assert status.progress["metering"] == "UNKNOWN"
    assert facts["metering"] == "UNKNOWN"
    assert facts["accounting"] == "REPORTED"
    assert status.progress["exit_code"] == 0


def test_reported_metering_and_the_exit_code_are_carried(tmp_path) -> None:
    cluster, handle = _server(tmp_path)
    backend = support.backend(cluster)
    job_id = _job_id(handle)
    cluster.jobs[job_id]["state"] = "COMPLETED"

    status = backend.status(handle.backend_job_ref)
    facts = backend._schedule_facts(cluster, job_id, target=support.target())

    assert status.progress["elapsed"] == "00:12:34"
    assert status.progress["max_rss"] == "1234K"
    assert status.progress["alloc_tres"] == "cpu=4,mem=8G"
    assert status.progress["metering"] == "REPORTED"
    assert status.progress["exit_code"] == 0
    assert facts["slurm_state"] == "COMPLETED"
    assert facts["target_id"] == TARGET_A
    assert facts["elapsed"] == "00:12:34"
    assert facts["max_rss"] == "1234K"
    assert facts["alloc_tres"] == "cpu=4,mem=8G"
    assert facts["metering"] == "REPORTED"
    assert facts["exit_code"] == 0


def test_the_manifest_and_scheduler_log_are_read_from_the_run_directory(tmp_path) -> None:
    cluster, _handle = _server(tmp_path)
    backend = support.backend(cluster)
    remote_dir = support.record_path().rsplit("/", 1)[0]
    cluster.files[f"{remote_dir}/run.out"] = b"iteration 1\n"
    listing = cluster.listdir(remote_dir)

    assert backend._required_outputs(cluster, remote_dir, listing) == ("results.csv",)
    assert backend._fetch_logs(cluster, remote_dir, listing) == {
        "run.out": "iteration 1\n"
    }

    body = b"\x00\xffbinary-result"
    cluster.files[f"{remote_dir}/results.csv"] = body
    assert backend._read(cluster, f"{remote_dir}/results.csv") == body


def test_a_missing_required_output_is_reported_not_invented(tmp_path) -> None:
    cluster, handle = _server(tmp_path)
    backend = support.backend(cluster)
    remote_dir = support.record_path().rsplit("/", 1)[0]
    listing = cluster.listdir(remote_dir)
    where = Collected(
        job_id=_job_id(handle),
        project_id=PROJECT,
        database=cast(Any, object()),
        store=cast(Any, object()),
    )

    fetched, missing = backend._fetch(
        cluster, remote_dir, listing, ("results.csv",), where
    )

    assert fetched == []
    assert missing == ["results.csv"]


def test_collect_refuses_without_an_object_store(tmp_path) -> None:
    cluster, handle = _server(tmp_path)

    with pytest.raises(SlurmConfigurationError, match="no object store"):
        support.backend(cluster).collect(handle.backend_job_ref)


def test_collect_will_not_read_outside_the_targets_root(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster, database=object(), store=object())
    handle = backend.submit(support.request(workspace=support.workspace(tmp_path)))
    cluster.jobs[_job_id(handle)]["dir"] = "/tmp/somebody-elses-run"

    result = backend.collect(handle.backend_job_ref)

    assert isinstance(result, JobOutputs)
    assert "outside this target's jobs root" in str(
        result.completion_metadata["note"]
    )
    assert result.artifacts == ()


def test_collect_reports_an_unlocatable_run_without_guessing(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster, database=object(), store=object())
    handle = backend.submit(support.request(workspace=support.workspace(tmp_path)))
    cluster.jobs.clear()
    cluster.files.pop(support.record_path(), None)

    result = backend.collect(handle.backend_job_ref)

    assert "could not be located" in str(result.completion_metadata["note"])
    assert result.artifacts == ()


def test_collect_reports_a_directory_with_no_submission_record(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster, database=object(), store=object())
    handle = backend.submit(support.request(workspace=support.workspace(tmp_path)))
    cluster.files.pop(support.record_path())

    result = backend.collect(handle.backend_job_ref)

    assert "holds no submission record" in str(result.completion_metadata["note"])
    assert result.artifacts == ()
