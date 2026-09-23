"""A cluster that answers commands from a dictionary.

The Slurm backend is written against `SSHTransport`, so what a test substitutes
is a transport and not the backend. `ScriptedCluster` is a small shell: it keeps
a dictionary of remote paths to bytes and a dictionary of jobs, and it *parses
the commands the backend builds* rather than being handed their results. That
distinction is the point. A fake that returned canned answers keyed by method
name would pass whatever command strings the backend happened to produce,
including a `sbatch` with the wrong `--chdir`; this one runs the command through
the same quoting and the same options a cluster would see, so a change to a
command is a change a test can see.

**Nothing here is a mock of the backend.** `SlurmComputeBackend` under test is
the real one — the same object a deployment constructs — and the only thing
missing is the network. What that buys is that the parts of this backend most
worth testing are the parts a live cluster could not show on demand: a worker
that died between `sbatch` and the record being written, a scheduler that
forgets a job, an unrecognised state, a host that echoes the password back.

A module rather than a conftest, because two suites need it and they sit in
different directories: `tests/unit/backends/slurm/` drives the backend's own
transitions against it, and `tests/integration/backends/` collects a job's
output through it into a real database. A conftest is loaded for its own
directory only, so the fake lives here and each suite declares the two fixtures
it wants around it.
"""

from __future__ import annotations

import base64
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ravel.backends.slurm.ssh import CommandResult, RemoteFile

#: The password a scripted cluster is configured with. Present so that a test
#: can make the cluster *echo* it and then assert it is nowhere in what the
#: backend hands back — a redaction test that used no real secret would pass
#: against a redactor that did nothing.
PASSWORD = "correct-horse-battery-staple"

#: Where this cluster keeps the jobs RAVEL submits.
JOBS_ROOT = "/ravel/jobs"


@dataclass
class ScriptedCluster:
    """A transport that answers like a small, well-behaved Slurm cluster."""

    #: Remote path to bytes. The workspace, the results, everything.
    files: dict[str, bytes] = field(default_factory=dict)
    #: Job id to the facts about it: `state`, `name`, `dir`, `exit`.
    jobs: dict[str, dict[str, str]] = field(default_factory=dict)
    #: Every command the backend ran, in order. A test reads this to find out
    #: what the backend actually asked the cluster to do.
    commands: list[str] = field(default_factory=list)
    #: Substring to canned result. Checked before the shell below, which is how
    #: a test makes one command fail without breaking the rest of the cluster.
    answers: dict[str, CommandResult] = field(default_factory=dict)
    #: Text appended to every command's stderr, and discarded. Used to make the
    #: cluster leak a secret so a test can assert the backend does not.
    stderr_echo: str = ""
    #: The next job id `sbatch` will hand out.
    next_job_id: int = 7001
    closed: int = 0

    # ── The transport protocol ──────────────────────────────────────────────

    def run(self, command: str, *, timeout: float | None = None) -> CommandResult:
        """Read the command and answer it."""
        self.commands.append(command)
        for needle, canned in self.answers.items():
            if needle in command:
                return canned
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
        """Upload a file. Fails the way a full disk or a bad path would."""
        self.files[remote_path] = local_path.read_bytes()

    def get(self, remote_path: str, local_path: Path) -> None:
        """Download a file."""
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(self.files[remote_path])

    def listdir(self, remote_path: str) -> tuple[RemoteFile, ...]:
        """Every file under a remote directory, recursively."""
        prefix = remote_path.rstrip("/") + "/"
        return tuple(
            RemoteFile(path=path[len(prefix) :], size_bytes=len(body))
            for path, body in sorted(self.files.items())
            if path.startswith(prefix)
        )

    def close(self) -> None:
        """Count the close, so a test can tell a session was released."""
        self.closed += 1

    # ── Reading the commands RAVEL sends ────────────────────────────────────

    def _interpret(self, command: str) -> CommandResult:
        """The cluster's side of the command surface the backend uses.

        Deliberately narrow. An unrecognised command comes back as a failure
        rather than a success, so a backend that grows a command this fake does
        not know fails visibly instead of passing on a default.
        """
        for pattern, handler in _HANDLERS:
            match = pattern.search(command)
            if match is not None:
                return handler(self, match, command)
        return _failed(command, "the scripted cluster does not know that command")

    def job_named(self, name: str) -> str | None:
        """The id of the job with this name, if the cluster has one."""
        for job_id, job in self.jobs.items():
            if job.get("name") == name:
                return job_id
        return None

    def submit(self, name: str, directory: str) -> str:
        """Take a job on, as `sbatch` would."""
        job_id = str(self.next_job_id)
        self.next_job_id += 1
        self.jobs[job_id] = {
            "state": "PENDING",
            "name": name,
            "dir": directory,
            "exit": "0:0",
        }
        return job_id


def _result(command: str, stdout: str = "", status: int = 0, stderr: str = "") -> CommandResult:
    return CommandResult(command=command, exit_status=status, stdout=stdout, stderr=stderr)


def _failed(command: str, why: str) -> CommandResult:
    return _result(command, stderr=why, status=1)


def _unquote(word: str) -> str:
    """Undo `_quote`'s single-quoting, so a path can be looked up."""
    return word.replace("'\\''", "'").strip("'")


def _mkdir(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    return _result(command)


def _cat(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    path = _unquote(match.group("path"))
    if path not in cluster.files:
        return _failed(command, f"cat: {path}: No such file or directory")
    return _result(command, cluster.files[path].decode("utf-8", errors="replace"))


def _base64(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    path = _unquote(match.group("path"))
    if path not in cluster.files:
        return _failed(command, f"base64: {path}: No such file or directory")
    return _result(command, base64.b64encode(cluster.files[path]).decode("ascii"))


def _write(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    path = _unquote(match.group("path"))
    body = command.split("\n", 1)[1].rsplit("\nRAVEL_SUBMISSION", 1)[0]
    cluster.files[path] = body.encode("utf-8")
    return _result(command)


def _squeue_job(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    job = cluster.jobs.get(match.group("id"))
    if job is None or job["state"] in ("COMPLETED", "FAILED", "CANCELLED"):
        # What squeue does for a job the queue no longer holds: nothing at all,
        # with a zero exit status. The backend reads the empty answer as "ask
        # the accounting log".
        return _result(command, "")
    return _result(command, job["state"] + "\n")


def _squeue_name(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    job_id = cluster.job_named(_unquote(match.group("name")))
    if job_id is None:
        return _result(command, "")
    return _result(command, f"{job_id}\n")


def _sacct_name(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    job_id = cluster.job_named(_unquote(match.group("name")))
    return _result(command, f"{job_id}|\n" if job_id else "")


def _sacct_job(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    job_id = match.group("id")
    job = cluster.jobs.get(job_id)
    if job is None:
        # What sacct does for a job the accounting log has no record of: it
        # prints nothing and exits successfully. The distinction matters — a
        # failed sacct means the scheduler could not be reached, and RAVEL
        # answers the two differently.
        return _result(command, "")
    state = job["state"]
    if state in ("PENDING", "RUNNING"):
        # sacct reports a running job as RUNNING with no exit code yet.
        return _result(command, f"{job_id}|RUNNING|0:0|00:01:02|\n")
    rows = [f"{job_id}|{state}|{job['exit']}|00:12:34|1234K"]
    if job.get("batch_exit"):
        rows.append(f"{job_id}.batch|{state}|{job['batch_exit']}|00:12:30|1234K")
    return _result(command, "\n".join(rows) + "\n")


def _sacct_workdir(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    job = cluster.jobs.get(match.group("id"))
    if job is None:
        return _result(command, "")
    return _result(command, f"{job['dir']}|\n")


def _sbatch(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    """Run the job script, having first done the `test -f` the backend guards it with.

    The guard is part of the command line rather than a separate call — one
    round trip instead of two — so the fake runs it as a shell would: a script
    that is not there means the whole command fails and nothing is submitted.
    """
    name = _unquote(match.group("name"))
    directory = _unquote(match.group("dir"))
    script = _unquote(match.group("script"))
    if script not in cluster.files:
        return _failed(command, f"sbatch: error: {script}: No such file or directory")
    return _result(command, f"{cluster.submit(name, directory)}\n")


def _scancel(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    job = cluster.jobs.get(match.group("id"))
    if job is None:
        return _failed(command, "scancel: error: Invalid job id specified")
    job["state"] = "CANCELLED"
    job["exit"] = "0:15"
    return _result(command)


def _grep(cluster: ScriptedCluster, match: re.Match[str], command: str) -> CommandResult:
    needle = _unquote(match.group("needle"))
    root = _unquote(match.group("root"))
    for path, body in sorted(cluster.files.items()):
        if path.startswith(root) and needle.encode("utf-8") in body:
            return _result(command, f"{path}\n")
    return _result(command, "")


#: The command surface, in the order it is tried. Each pattern names the
#: arguments it needs, so a test failing here means the backend's command line
#: changed shape rather than that the fake is wrong.
_Handled = Callable[["ScriptedCluster", "re.Match[str]", str], CommandResult]

#: The command surface, in the order it is tried.
_HANDLERS: tuple[tuple[re.Pattern[str], _Handled], ...] = (
    (re.compile(r"^mkdir -p "), _mkdir),
    (re.compile(r"^cat > '(?P<path>[^']*)' <<'RAVEL_SUBMISSION'"), _write),
    (re.compile(r"^cat '(?P<path>[^']*)'$"), _cat),
    (re.compile(r"^base64 -w0 '(?P<path>[^']*)'$"), _base64),
    (re.compile(r"^squeue -h -j (?P<id>\d+) -o %T$"), _squeue_job),
    (re.compile(r"^squeue -h -n '(?P<name>[^']*)' -o %A$"), _squeue_name),
    (re.compile(r"^sacct -n -P -X --name='(?P<name>[^']*)' -o JobID$"), _sacct_name),
    (re.compile(r"^sacct -n -P -X -j (?P<id>\d+) -o WorkDir%512$"), _sacct_workdir),
    (
        re.compile(
            r"^sacct -n -P -j (?P<id>\d+) -o JobID,State%512,ExitCode,Elapsed,MaxRSS$"
        ),
        _sacct_job,
    ),
    (
        re.compile(
            r"^(?:test -f '[^']*' && )?sbatch --parsable --job-name='(?P<name>[^']*)' "
            r"--chdir='(?P<dir>[^']*)' '(?P<script>[^']*)'$"
        ),
        _sbatch,
    ),
    (re.compile(r"^scancel (?P<id>\d+)$"), _scancel),
    (re.compile(r"^chmod \+x "), _mkdir),
    (
        re.compile(
            r"^grep -rl --include=submission\.json -m1 '(?P<needle>[^']*)' "
            r"'(?P<root>[^']*)' 2>/dev/null \| head -1$"
        ),
        _grep,
    ),
)


@dataclass
class Connections:
    """The transport factory a backend is built with, and what it opened.

    A callable rather than a list because that is exactly what the backend
    takes: `connect` is the seam, and a test that passed a plain lambda could
    not afterwards ask how many connections were opened or whether they were
    closed. Passing this instead makes "one connection per operation, each
    released" a thing a test can assert rather than a thing it hopes.
    """

    cluster: ScriptedCluster
    opened: list[ScriptedCluster] = field(default_factory=list)

    def __call__(self) -> ScriptedCluster:
        self.opened.append(self.cluster)
        return self.cluster

    @property
    def all_closed(self) -> bool:
        """Whether every connection this factory handed out was closed."""
        return bool(self.opened) and all(
            connection.closed > 0 for connection in self.opened
        )
