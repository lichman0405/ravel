"""A scripted multi-target Slurm cluster for T09's premium tests.

**This is not real HPC, and these tests do not certify a real cluster.** The
live-cluster half of T09 is blocked on an endpoint, credentials and a
`known_hosts` file nobody has supplied; `real_hpc_check.py` in this directory
says so and refuses to pretend otherwise. What this module provides is the
other half: the real `SlurmComputeBackend` and the real target registry, with
the network replaced by a transport that parses the exact command strings the
backend builds.

The fake is deliberately a *shell*, not a dictionary of canned method answers.
It stores remote files and jobs, and it runs the commands the backend issues —
so a changed `sbatch` line, a dropped `--partition`, or a probe that is not
sent is a change these tests can see. An unknown command fails visibly rather
than defaulting to success.

The command surface is the target-mode one: the compound intent-record write
(`mkdir -p '<dir>' && cat > '<file>' <<'RAVEL_SUBMISSION'`), the six-field
`sacct` row that includes `AllocTRES`, and `sbatch` with the target's optional
`--partition`/`--account`. The legacy surface lives in `tests/support/slurm.py`
and is left untouched, because the legacy tests are how we know target mode
did not change it.
"""

from __future__ import annotations

import base64
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from ravel.backends.slurm import (
    CommandResult,
    RemoteFile,
    SlurmComputeBackend,
    SlurmTarget,
    TransportError,
)
from ravel.execution.backends import JobRequest

#: The one fact every test module in this directory is built around. Read it
#: and believe it: no test here touches a cluster, a scheduler, or a network.
REAL_HPC = False

#: A real secret, so that redaction tests fail against a redactor that does
#: nothing. The scripted cluster can echo it on every command's stderr.
PASSWORD = "premium-T09-horse-battery-staple"

#: Where the scripted cluster keeps run directories by default.
REMOTE_ROOT = "/scratch/ravel"

#: Two distinct operator-published targets. The UUIDs are fixed so that a
#: target-qualified reference is stable in a failure message.
TARGET_A = "11111111-1111-4111-8111-111111111111"
TARGET_B = "22222222-2222-4222-8222-222222222222"

PROJECT = "project-0001"
OTHER_PROJECT = "project-0002"
NODE = "node-0001"
OTHER_NODE = "node-0002"
CONTRACT_REF = "exec-contract-0001"
CONTRACT_VERSION = 1
ATTEMPT = 1

JOB_SCRIPT = "job.slurm"
MANIFEST_NAME = "calculation_manifest.json"


def request(
    node_id: str = NODE,
    *,
    attempt: int = ATTEMPT,
    project_id: str = PROJECT,
    target_id: str = TARGET_A,
    workspace: Path | None = None,
    entrypoint: str = JOB_SCRIPT,
    contract_version: int = CONTRACT_VERSION,
    software: str | None = None,
) -> JobRequest:
    """A `JobRequest` with the four facts the submission key is made of."""
    task_spec: dict[str, object] = {}
    if software is not None:
        task_spec["execution_requirements"] = {"software": software}
    if workspace is None:
        # A path is enough for the paths that refuse before touching a file,
        # and it is deliberately one that does not exist: a test that needed a
        # workspace passes one.
        workspace = Path("/nonexistent/ravel/T09") / node_id
    return JobRequest(
        project_id=project_id,
        node_id=node_id,
        attempt=attempt,
        execution_contract_ref=CONTRACT_REF,
        execution_contract_version=contract_version,
        objective=f"run {node_id}",
        task_spec=task_spec,
        required_outputs=("results.csv",),
        workspace_path=str(workspace),
        entrypoint=entrypoint,
        target_id=target_id,
    )


def workspace(
    root: Path,
    *,
    node_id: str = NODE,
    files: Mapping[str, str] | None = None,
) -> Path:
    """A prepared workspace: a job script, a manifest, and an output directory."""
    directory = root / f"workspace-{node_id}"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / JOB_SCRIPT).write_text(
        "#!/bin/bash\n#SBATCH --output=run.out\nset -eu\necho ran\n",
        encoding="utf-8",
    )
    (directory / MANIFEST_NAME).write_text(
        json.dumps({"required_outputs": ["results.csv"]}),
        encoding="utf-8",
    )
    for name, body in (files or {}).items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return directory


def target(
    target_id: str = TARGET_A,
    *,
    password: str | None = PASSWORD,
    remote_root: str = REMOTE_ROOT,
    partition: str | None = None,
    account: str | None = None,
    allowed_software: tuple[str, ...] = ("raspa", "lammps"),
    max_jobs: int | None = None,
    grace_seconds: float = 3600.0,
    known_hosts_filename: str = "/etc/ravel/known_hosts",
    host: str | None = None,
) -> SlurmTarget:
    """One runtime target, as `resolve_target` would hand it to the backend."""
    return SlurmTarget(
        host=host or f"cluster-{target_id[:4]}.example.org",
        username="ravel",
        port=22,
        password=password,
        known_hosts_filename=known_hosts_filename,
        target_id=target_id,
        partition=partition,
        account=account,
        remote_root=remote_root,
        allowed_software=allowed_software,
        max_jobs=max_jobs,
        reconcile_grace_seconds=grace_seconds,
    )


def backend(
    cluster: Any,
    *,
    targets: Iterable[SlurmTarget] | None = None,
    default_target_id: str = TARGET_A,
    clusters: Mapping[str, Any] | None = None,
    database: object | None = None,
    store: object | None = None,
) -> SlurmComputeBackend:
    """The real backend, with every connection answered by a scripted cluster.

    `clusters` lets two target ids share one fake (so a cross-target
    cancellation has the same remote filesystem) or use different ones.
    """
    resolved = {item.target_id: item for item in (targets or (target(),))}
    if clusters is None:
        clusters = {}
    default = resolved.get(default_target_id) or next(iter(resolved.values()))
    return SlurmComputeBackend(
        target=default,
        connect=lambda: clusters.get(default_target_id, cluster),
        targets=resolved,
        connect_for=lambda target_id: clusters.get(target_id, cluster),
        default_target_id=default_target_id,
        database=database,  # type: ignore[arg-type]
        store=store,  # type: ignore[arg-type]
        jobs_root=REMOTE_ROOT,
    )


def record_path(
    remote_root: str = REMOTE_ROOT,
    *,
    project_id: str = PROJECT,
    node_id: str = NODE,
    attempt: int = ATTEMPT,
) -> str:
    """Where the backend writes this attempt's intent record."""
    return f"{remote_root}/{project_id}/{node_id}/{attempt}/submission.json"


def write_record(
    cluster: ScriptedTargetCluster,
    record: Mapping[str, object],
    *,
    path: str | None = None,
    project_id: str = PROJECT,
    node_id: str = NODE,
    attempt: int = ATTEMPT,
) -> str:
    """Put a record on the cluster, as a dead process would have left it."""
    destination = path or record_path(
        cluster.remote_root, project_id=project_id, node_id=node_id, attempt=attempt
    )
    cluster.files[destination] = json.dumps(dict(record), indent=2, sort_keys=True).encode()
    return destination


def read_record(
    cluster: ScriptedTargetCluster,
    *,
    path: str | None = None,
    project_id: str = PROJECT,
    node_id: str = NODE,
    attempt: int = ATTEMPT,
) -> dict[str, Any]:
    """The JSON record the backend wrote, parsed."""
    source = path or record_path(
        cluster.remote_root, project_id=project_id, node_id=node_id, attempt=attempt
    )
    document = json.loads(cluster.files[source])
    assert isinstance(document, dict)
    return document


def intent_record(
    request_: JobRequest,
    target_: SlurmTarget,
    *,
    status: str = "SUBMITTING",
    submission_key: str | None = None,
    intent_at: datetime | None = None,
    **overrides: object,
) -> dict[str, object]:
    """A record shaped exactly as `_write_target_intent` shapes one."""
    key = submission_key or SlurmComputeBackend.submission_key_for(request_)
    written = intent_at or datetime.now(UTC)
    document: dict[str, object] = {
        "status": status,
        "submission_key": key,
        "project_id": request_.project_id,
        "node_id": request_.node_id,
        "attempt": request_.attempt,
        "execution_contract_ref": request_.execution_contract_ref,
        "execution_contract_version": request_.execution_contract_version,
        "target_id": target_.target_id,
        "target_host": target_.host,
        "job_id": None,
        "job_name": f"ravel-{SlurmComputeBackend.digest_for(request_)}",
        "remote_directory": (
            f"{(target_.remote_root or REMOTE_ROOT).rstrip('/')}/"
            f"{request_.project_id}/{request_.node_id}/{request_.attempt}"
        ),
        "software": None,
        "intent_at": written.isoformat(),
        "reconcile_grace_seconds": target_.reconcile_grace_seconds,
    }
    document.update(overrides)
    return document


def old_intent_time(seconds: float = 3600.0) -> datetime:
    """A timestamp old enough that a grace window has certainly passed."""
    return datetime.now(UTC) - timedelta(seconds=seconds)


#: Slurm states after which the queue (as opposed to accounting) forgets a job.
TERMINAL_STATES = frozenset(
    {
        "COMPLETED",
        "FAILED",
        "CANCELLED",
        "TIMEOUT",
        "OUT_OF_MEMORY",
        "NODE_FAIL",
        "PREEMPTED",
        "BOOT_FAIL",
        "DEADLINE",
        "REVOKED",
    }
)


@dataclass
class ScriptedTargetCluster:
    """A transport that answers like a small Slurm cluster with a target file.

    All state is behind one re-entrant lock: the concurrency tests drive the
    backend from two threads, and a fake that lost a job to a dict race would
    make an idempotency test prove nothing.
    """

    remote_root: str = REMOTE_ROOT
    #: Remote path to bytes: the uploaded workspace, results, the record.
    files: dict[str, bytes] = field(default_factory=dict)
    #: Job id to the facts about it. See `submit`.
    jobs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Every command the backend ran, in order.
    commands: list[str] = field(default_factory=list)
    #: One-shot sbatch outcomes, consumed in order. Values are
    #: `lost_but_created`, `lost_no_job`, `garbage`.
    failures: list[str] = field(default_factory=list)
    #: A persistent scheduler refusal: `partition`, `account`, or `generic`.
    sbatch_rejection: str | None = None
    #: Text appended to every command's stderr. Used to make the cluster leak
    #: the password so a test can assert the backend does not.
    stderr_echo: str = ""
    next_job_id: int = 7001
    closed: int = 0

    #: Probe and command switches. Named for what they break, so a failing test
    #: says which part of the scheduler went quiet.
    squeue_name_fails: bool = False
    sacct_name_fails: bool = False
    squeue_status_fails: bool = False
    sacct_status_fails: bool = False
    sacct_status_empty: bool = False
    record_read_fails: bool = False
    record_write_fails: bool = False
    workdir_fails: bool = False
    scancel_fails: bool = False
    #: When true, accounting reports the job but no elapsed/memory/TRES.
    suppress_metering: bool = False
    suppress_alloc_tres: bool = False

    _lock: threading.RLock = field(
        default_factory=threading.RLock, init=False, repr=False
    )

    # ── The transport protocol ──────────────────────────────────────────────

    def run(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Answer one command. A transport fault raises; a refusal does not."""
        with self._lock:
            self.commands.append(command)
            result = self._interpret(command)
            if self.stderr_echo:
                return CommandResult(
                    command=result.command,
                    exit_status=result.exit_status,
                    stdout=result.stdout,
                    stderr=result.stderr + self.stderr_echo,
                )
            return result

    def put(self, local_path: Path, remote_path: str) -> None:
        """Upload a file, as the real transport would have hashed and sent it."""
        with self._lock:
            self.files[remote_path] = local_path.read_bytes()

    def get(self, remote_path: str, local_path: Path) -> None:
        """Download a file."""
        with self._lock:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(self.files[remote_path])

    def listdir(self, remote_path: str) -> tuple[RemoteFile, ...]:
        """Every file under one remote directory, recursively."""
        prefix = remote_path.rstrip("/") + "/"
        with self._lock:
            return tuple(
                RemoteFile(path=path[len(prefix) :], size_bytes=len(body))
                for path, body in sorted(self.files.items())
                if path.startswith(prefix)
            )

    def close(self) -> None:
        """Count the close, so a test can tell a session was released."""
        with self._lock:
            self.closed += 1

    # ── Reading the commands RAVEL sends ────────────────────────────────────

    def _interpret(self, command: str) -> CommandResult:
        for pattern, handler in _HANDLERS:
            match = pattern.search(command)
            if match is not None:
                return handler(self, match, command)
        return _failed(command, "the scripted cluster does not know that command")

    # ── Helpers tests use to read and steer the cluster ──────────────────────

    def job_named(self, name: str, *, live_only: bool = False) -> str | None:
        """The id of the job with this name.

        `live_only` is what `squeue` sees: a terminal job has left the queue,
        and only accounting still knows it.
        """
        for job_id, job in self.jobs.items():
            if job["name"] != name:
                continue
            if live_only and job["state"] in TERMINAL_STATES:
                continue
            return job_id
        return None

    def submit(self, name: str, directory: str, script: str) -> str:
        """Take a job on, as `sbatch` would."""
        job_id = str(self.next_job_id)
        self.next_job_id += 1
        self.jobs[job_id] = {
            "state": "PENDING",
            "name": name,
            "dir": directory,
            "script": script,
            "exit": "0:0",
            "batch_exit": "0:0",
            "elapsed": "00:12:34",
            "max_rss": "1234K",
            "alloc_tres": "cpu=4,mem=8G",
        }
        return job_id

    def sbatch_calls(self) -> int:
        """How many times the backend asked for an allocation.

        The backend's call is `test -f '<script>' && sbatch ...`. Counting
        commands that merely contain the word would also count the record
        write whose detail quotes the failed command, so the shape is matched
        rather than the substring.
        """
        return sum(
            1
            for command in self.commands
            if command.startswith("test -f ") and " && sbatch " in command
        )

    def commands_since(self, index: int) -> list[str]:
        """The commands run after a mark, for asserting what a poll asked."""
        return list(self.commands[index:])


def result(
    command: str, stdout: str = "", status: int = 0, stderr: str = ""
) -> CommandResult:
    return CommandResult(
        command=command, exit_status=status, stdout=stdout, stderr=stderr
    )


def _failed(command: str, why: str) -> CommandResult:
    return result(command, stderr=why, status=1)


# ── Handlers for the target-mode command surface ────────────────────────────


def _record_write(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.record_write_fails:
        return _failed(command, "mkdir: cannot create directory: Permission denied")
    path = match.group("path")
    body = match.group("body")
    cluster.files[path] = body.encode("utf-8")
    return result(command)


def _record_read(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.record_read_fails:
        return _failed(command, "cat: the submission record could not be read")
    path = match.group("path")
    body = cluster.files.get(path)
    if body is None:
        return result(command, "")
    return result(command, body.decode("utf-8", errors="replace"))


def _mkdir(cluster: ScriptedTargetCluster, match: re.Match[str], command: str) -> CommandResult:
    return result(command)


def _cat(cluster: ScriptedTargetCluster, match: re.Match[str], command: str) -> CommandResult:
    path = match.group("path")
    if path not in cluster.files:
        return _failed(command, f"cat: {path}: No such file or directory")
    return result(command, cluster.files[path].decode("utf-8", errors="replace"))


def _base64(cluster: ScriptedTargetCluster, match: re.Match[str], command: str) -> CommandResult:
    path = match.group("path")
    if path not in cluster.files:
        return _failed(command, f"base64: {path}: No such file or directory")
    return result(command, base64.b64encode(cluster.files[path]).decode("ascii"))


def _squeue_job(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.squeue_status_fails:
        return _failed(command, "squeue: error: Unable to contact slurm controller")
    job = cluster.jobs.get(match.group("id"))
    if job is None or job["state"] in TERMINAL_STATES:
        # What squeue does for a job the queue no longer holds: nothing at all,
        # with a zero exit status.
        return result(command, "")
    return result(command, job["state"] + "\n")


def _squeue_name(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.squeue_name_fails:
        return _failed(command, "squeue: error: Unable to contact slurm controller")
    job_id = cluster.job_named(match.group("name"), live_only=True)
    return result(command, f"{job_id}\n" if job_id else "")


def _sacct_name(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.sacct_name_fails:
        return _failed(command, "sacct: error: slurmdbd is not responding")
    job_id = cluster.job_named(match.group("name"))
    return result(command, f"{job_id}|\n" if job_id else "")


def _sacct_job(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.sacct_status_fails:
        return _failed(command, "sacct: error: slurmdbd is not responding")
    if cluster.sacct_status_empty:
        return result(command, "")
    job_id = match.group("id")
    job = cluster.jobs.get(job_id)
    if job is None:
        return result(command, "")
    state = job["state"]
    if state not in TERMINAL_STATES:
        return result(command, f"{job_id}|{state}|{job['exit']}|00:01:02||\n")
    if cluster.suppress_metering:
        elapsed = max_rss = tres = ""
    else:
        elapsed = job["elapsed"]
        max_rss = job["max_rss"]
        tres = "" if cluster.suppress_alloc_tres else job["alloc_tres"]
    rows = [f"{job_id}|{state}|{job['exit']}|{elapsed}|{max_rss}|{tres}"]
    rows.append(
        f"{job_id}.batch|{state}|{job['batch_exit']}|{elapsed}|{max_rss}|{tres}"
    )
    return result(command, "\n".join(rows) + "\n")


def _sacct_workdir(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.workdir_fails:
        return _failed(command, "sacct: error: slurmdbd is not responding")
    job = cluster.jobs.get(match.group("id"))
    if job is None:
        return result(command, "")
    return result(command, f"{job['dir']}|\n")


def _sbatch(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    script = match.group("script")
    if script not in cluster.files:
        return _failed(command, f"sbatch: error: {script}: No such file or directory")
    if cluster.sbatch_rejection == "partition":
        return _failed(
            command,
            "sbatch: error: Batch job submission failed: Invalid partition name specified",
        )
    if cluster.sbatch_rejection == "account":
        return _failed(
            command,
            "sbatch: error: Batch job submission failed: Invalid account or "
            "account/partition combination specified",
        )
    if cluster.sbatch_rejection == "generic":
        return _failed(command, "sbatch: error: Batch job submission failed")
    mode = cluster.failures.pop(0) if cluster.failures else ""
    name = match.group("name")
    directory = match.group("dir")
    if mode == "lost_no_job":
        raise TransportError("the connection reset after sbatch was sent")
    if mode == "garbage":
        # An exit status of zero and an answer that is not a job id: the
        # scheduler said something, and it was not "job 123".
        return result(command, "sbatch: warning: cluster is busy\n")
    if mode == "lost_but_created":
        cluster.submit(name, directory, script)
        raise TransportError("the connection reset after the job was created")
    job_id = cluster.submit(name, directory, script)
    # `--parsable` answers a bare id, or an id and a cluster after a semicolon.
    return result(command, f"{job_id}\n")


def _scancel(
    cluster: ScriptedTargetCluster, match: re.Match[str], command: str
) -> CommandResult:
    if cluster.scancel_fails:
        return _failed(command, "scancel: error: slurm_receive_msg: Socket timed out")
    job = cluster.jobs.get(match.group("id"))
    if job is None:
        return _failed(command, "scancel: error: Invalid job id specified")
    job["state"] = "CANCELLED"
    job["exit"] = "0:15"
    job["batch_exit"] = "0:15"
    return result(command)


def _grep(cluster: ScriptedTargetCluster, match: re.Match[str], command: str) -> CommandResult:
    needle = match.group("needle")
    root = match.group("root")
    for path, body in sorted(cluster.files.items()):
        if path.startswith(root) and needle.encode("utf-8") in body:
            return result(command, f"{path}\n")
    return result(command, "")


_Handled = Callable[["ScriptedTargetCluster", "re.Match[str]", str], CommandResult]

#: The command surface, in the order it is tried. The compound record write
#: comes before the plain `mkdir` because it starts with one.
_HANDLERS: tuple[tuple[re.Pattern[str], _Handled], ...] = (
    (
        re.compile(
            r"^mkdir -p '(?P<dir>[^']*)' && cat > '(?P<path>[^']*)' "
            r"<<'RAVEL_SUBMISSION'\n(?P<body>.*)\nRAVEL_SUBMISSION$",
            re.DOTALL,
        ),
        _record_write,
    ),
    (
        re.compile(r"^if \[ -e '[^']*' \]; then cat '(?P<path>[^']*)'; fi$"),
        _record_read,
    ),
    (re.compile(r"^mkdir -p '[^']*'$"), _mkdir),
    (re.compile(r"^cat '(?P<path>[^']*)'$"), _cat),
    (re.compile(r"^base64 -w0 '(?P<path>[^']*)'$"), _base64),
    (re.compile(r"^squeue -h -j (?P<id>\d+) -o %T$"), _squeue_job),
    (re.compile(r"^squeue -h -n '(?P<name>[^']*)' -o %A$"), _squeue_name),
    (re.compile(r"^sacct -n -P -X --name='(?P<name>[^']*)' -o JobID$"), _sacct_name),
    (re.compile(r"^sacct -n -P -X -j (?P<id>\d+) -o WorkDir%512$"), _sacct_workdir),
    (
        re.compile(
            r"^sacct -n -P -j (?P<id>\d+) -o "
            r"JobID,State%512,ExitCode,Elapsed,MaxRSS,AllocTRES%512$"
        ),
        _sacct_job,
    ),
    (
        re.compile(
            r"^test -f '(?P<script_check>[^']*)' && sbatch --parsable "
            r"--job-name='(?P<name>[^']*)' --chdir='(?P<dir>[^']*)'"
            r"(?: --partition='[^']*')?(?: --account='[^']*')? "
            r"'(?P<script>[^']*)'$"
        ),
        _sbatch,
    ),
    (re.compile(r"^scancel (?P<id>\d+)$"), _scancel),
    (re.compile(r"^chmod \+x '[^']*'$"), _mkdir),
    (
        re.compile(
            r"^grep -rl --include=submission\.json -m1 '(?P<needle>[^']*)' "
            r"'(?P<root>[^']*)' 2>/dev/null \| head -1$"
        ),
        _grep,
    ),
)
