"""The Slurm backend: real compute, on a cluster RAVEL does not own.

This is the first backend in RAVEL that runs work somewhere real. Everything
before it either simulated a result or waited for a person. What that changes is
not the shape of the port — `submit`, `status`, `deliver`, `collect`, `cancel`,
exactly as the contract says — but what each call has to get right, because a
mistake here spends somebody's allocation and produces a number a paper might
rest on.

**Idempotent submission is the whole design problem.** RAVEL calls `submit`,
writes the job row, and commits; a worker killed before that commit leaves a row
saying "submitted, no reference", and RAVEL's answer is to call `submit` again.
For a mock that is free. For a cluster it is a second allocation running the
same simulation. The reference is therefore derived from the work rather than
generated — `(project, node, attempt)` — and the backend checks three places
before it submits anything:

1. the local `submission.json` RAVEL itself uploaded into the run's remote
   directory, which is the durable record and survives everything;
2. the scheduler, by job name, for a job this attempt already has queued or
   running or in the accounting log;
3. only then, an upload and an `sbatch`.

Each check answers a case the one before it cannot: (1) is the truth after any
restart, (2) catches a job submitted by a *different* RAVEL process that did not
get to write (1), and (3) is the only path that spends anything.

**The workspace is made self-describing.** `collect` is handed a job reference
and nothing else — the port says so — so the remote directory is found by asking
the scheduler where the job ran (`sacct -o WorkDir`), and what the run was
supposed to produce is read out of the `calculation_manifest.json` preparation
left in it. Nothing is remembered between calls that PostgreSQL does not already
hold, which is what makes a worker restart uninteresting.

**The credential never leaves this process.** It is read from `Settings`, lives
only in the transport's memory, and every string this module returns passes
through `Redactor` first: a `detail` line is written to a PostgreSQL column and
a traceback goes to a log, and both outlive the run.

**A Slurm job never waits for a person.** `deliver` exists on the port because a
lab reaches it; a cluster does not, and this backend's `deliver` says so rather
than pretending to accept a delivery it has nowhere to put. Nothing in the
workflow reaches it, because nothing here ever reports `WAITING_EXTERNAL`.
"""

from __future__ import annotations

import base64
import hashlib
import json
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ravel.backends.slurm.ssh import (
    REDACTED,
    CommandResult,
    Redactor,
    RemoteFile,
    SSHTransport,
    TransportError,
)
from ravel.backends.slurm.states import read_exit_code, read_state
from ravel.domain.enums import JobState
from ravel.execution.backends import (
    ExternalDelivery,
    JobHandle,
    JobOutputs,
    JobRequest,
    JobStatus,
)
from ravel.state.database import Database
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import ArtifactStore, hash_chunks

#: How this backend is named in `ExecutionRecord.backend` and in every
#: `BACKEND_STATUS_CHANGED` event.
BACKEND_NAME = "slurm-compute"

#: The file RAVEL writes into a run's remote directory to say that this attempt
#: has been submitted. Named rather than derived so an operator looking at a
#: directory can see what it means.
SUBMISSION_FILE = "submission.json"

#: How much of the work's digest goes into a job name and a reference. The same
#: sixteen characters the mocks use, for the same reason: a reference is a `REF`
#: column in RAVEL and those hold sixty-four, and a Slurm job name is capped at
#: sixty-four by the scheduler, both of which a spelled-out path would exceed.
REF_DIGEST_CHARS = 16

#: Slurm job name prefix. Names a person running `squeue` on the cluster can
#: pick RAVEL's work out by, and short enough to leave room for the digest.
JOB_NAME_PREFIX = "ravel-"

#: Suffix that makes a file a job script: the one preparation writes, and the
#: only file in a workspace that has to be executable on the far side.
JOB_SCRIPT_SUFFIX = ".slurm"


class SlurmConfigurationError(RuntimeError):
    """The backend was asked to run work and is not configured to reach a cluster.

    Raised when a deployment points a node type at Slurm without a host, rather
    than at submission time: the failure belongs at start-up, where somebody is
    watching, and not in the middle of a project.
    """


class SlurmSubmissionError(RuntimeError):
    """Work could not be handed to the cluster.

    Distinct from a job that ran and failed. This is a fault in getting the work
    *to* the cluster: no workspace to upload, a scheduler that refused, a
    transfer that broke. It propagates, the activity fails, and Temporal retries
    it — which is safe precisely because `submit` is idempotent, so the retry
    either finds the job the first call created or creates the one it did not.
    """


@dataclass(frozen=True, slots=True)
class CollectedOutput:
    """One required output that came back off the cluster.

    The name the *contract* uses, the bytes, and where they were filed. All
    three are needed and none implies the others: `delivered_outputs` is
    compared against the contract's names rather than against filenames, the
    hash of the bytes is what makes a collected result checkable later, and the
    artifact id is what the execution record points at.
    """

    name: str
    body: bytes
    artifact_id: str
    version: int = 1
    #: The hash of `body`, computed where the bytes were hashed anyway. It is
    #: what `completion_metadata` records so a reader can check the artifact
    #: against the cluster, and what a second collection compares against to
    #: tell a retry from a changed file.
    content_hash: str = ""


@dataclass(frozen=True, slots=True)
class Collected:
    """Where a collected result is filed, gathered once per `collect`.

    A bundle rather than five arguments because these are one fact — this run
    belongs to this project and its bytes go in this store — and because
    threading them through the download loop individually is how one of them
    ends up passed in the wrong position. `project_id` and `node_id` come from
    the run's own submission record, which is the only description of the run
    that travelled with the run.
    """

    job_id: str
    project_id: str
    database: Database
    store: ArtifactStore
    node_id: str | None = None


@dataclass(frozen=True, slots=True)
class SlurmTarget:
    """Where the cluster is and how to reach it.

    A separate object from `Settings` so that the backend's construction is not
    coupled to the whole configuration, and so that a test can describe a
    cluster without building a settings object. The secret is here and nowhere
    else; it is not a field on the backend, not logged, and not returned.
    """

    host: str
    username: str
    port: int = 22
    password: str | None = None
    key_filename: str | None = None
    trust_unknown_host: bool = False
    connect_timeout_seconds: float = 15.0
    command_timeout_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise SlurmConfigurationError("a Slurm target needs a host")
        if not self.username.strip():
            raise SlurmConfigurationError("a Slurm target needs a username")


@dataclass
class SlurmComputeBackend:
    """A `WorkBackend` that runs computation on a Slurm cluster over SSH.

    Constructed with a *factory* rather than a connection. A connection held
    open across a poll would be held open across a durable timer — the workflow
    waits minutes between status checks, and a job may run for days — and a
    worker that keeps a socket to a cluster for that long is a worker that has
    to notice a socket that died quietly. One connection per operation costs a
    handshake and removes the question.
    """

    target: SlurmTarget
    connect: Callable[[], SSHTransport]
    #: Where collected output is filed. Optional so that a deployment which
    #: only ever submits and polls can be built without one — and required, at
    #: runtime, by `collect`: a backend with nowhere to put a result has lost
    #: it, and `SlurmConfigurationError` says so rather than returning bytes
    #: that exist only inside a completed activity.
    database: Database | None = None
    store: ArtifactStore | None = None
    jobs_root: str = "/ravel/jobs"
    name: str = BACKEND_NAME

    #: Every string this backend hands back passes through here. Built from the
    #: target's secret rather than passed in, so there is no way to construct a
    #: backend that forgets.
    redact: Redactor = field(init=False, default_factory=lambda: Redactor())

    def __post_init__(self) -> None:
        self.redact = Redactor(
            tuple(secret for secret in (self.target.password,) if secret)
        )

    # ── The port ────────────────────────────────────────────────────────────

    def submit(self, request: JobRequest) -> JobHandle:
        """Hand work to the cluster, or return the work already handed over.

        Raises:
            SlurmSubmissionError: The request names no workspace to upload, or
                the scheduler refused the job. Both are faults in getting the
                work to the cluster rather than in the work, and both are safe
                to retry — see the module docstring.
            TransportError: The connection failed. Propagated so the activity
                retries rather than recording a failed attempt that never ran.
        """
        if not request.workspace_path.strip():
            raise SlurmSubmissionError(
                "no workspace was prepared for this run, so there is nothing to "
                "upload and no job script to submit; a contract that names a "
                "Slurm environment has to be materialized before it can run"
            )
        digest = self.digest_for(request)
        remote_dir = self.remote_directory(request)
        job_name = f"{JOB_NAME_PREFIX}{digest}"
        with self._session() as ssh:
            existing = self._read_submission(ssh, remote_dir)
            if existing is not None:
                return self._handle(existing["job_id"], digest, "resubmitted")
            found = self._find_by_name(ssh, job_name)
            if found is not None:
                self._write_submission(ssh, remote_dir, found, digest, request)
                return self._handle(found, digest, "found on the cluster")
            self._upload(ssh, Path(request.workspace_path), remote_dir, request.entrypoint)
            job_id = self._sbatch(ssh, job_name, remote_dir, request.entrypoint)
            self._write_submission(ssh, remote_dir, job_id, digest, request)
        return self._handle(job_id, digest, "submitted")

    def status(self, backend_job_ref: str) -> JobStatus:
        """Ask the scheduler where the job is.

        `squeue` first because it is the live view and answers for a running
        job; `sacct` when the queue has nothing, which means the job has ended
        and only the accounting log still holds it. A job absent from both is
        reported as a failure RAVEL cannot classify rather than guessed at:
        something happened, and inventing which would be inventing a fact.

        Raises:
            TransportError: The connection failed.
        """
        job_id, _digest = self.parse_ref(backend_job_ref)
        with self._session() as ssh:
            return self._status_of(ssh, job_id)

    def deliver(self, backend_job_ref: str, delivery: ExternalDelivery) -> JobStatus:
        """Report that a Slurm job does not wait for anything outside itself.

        Not an error and not a silent no-op: the state is reported unchanged and
        the detail says why. A caller that reached here has a bug — this backend
        never reports `WAITING_EXTERNAL`, so nothing in RAVEL should have decided
        to deliver to it — and a detail line saying so is how that gets found.
        """
        status = self.status(backend_job_ref)
        return JobStatus(
            state=status.state,
            backend_state=status.backend_state,
            failure_class=status.failure_class,
            detail=self.redact(
                f"a Slurm job runs to its own end and waits for nothing outside "
                f"itself, so the delivery was recorded by RAVEL and not here; "
                f"{status.detail}"
            ),
            progress=status.progress,
        )

    def collect(self, backend_job_ref: str) -> JobOutputs:
        """Bring back what the run produced, and say what is still missing.

        The remote directory is found by asking the scheduler where the job ran,
        and what the run owed is read from the manifest preparation left in that
        directory — a manifest names `required_outputs` because it was written
        for exactly this reader. Nothing about the job is remembered between
        calls, so a worker that restarted an hour ago collects as well as one
        that never stopped.

        Only what the contract required is brought back, plus the scheduler's
        own log. Everything else stays where the run put it: a simulation's
        scratch files can be gigabytes, and a directory is traceable from the
        manifest while a silently truncated download is not.

        The bytes are stored as artifacts here, in this call, for the reason the
        mocks store theirs here: this is the one moment the files are in hand,
        and a backend that returned them as a dictionary would be handing RAVEL
        a copy of a result rather than the result. What comes back in
        `completion_metadata` is what a reader needs to check the artifacts
        against the cluster — the directory, the hashes, the scheduler's own
        account, and which required outputs did *not* arrive.

        Collecting the same job twice does not file the same result twice: an
        activity that was retried after its transaction died finds the version
        the first attempt stored and delivers that. See `_already_collected`.

        Raises:
            TransportError: The connection failed.
            SlurmConfigurationError: No object store is configured, so there is
                nowhere to put what was collected. Raised rather than returning
                the bytes in memory: a result that exists only in a completed
                activity's return value is a result RAVEL does not have.
        """
        database, store = self.database, self.store
        if database is None or store is None:
            raise SlurmConfigurationError(
                "this backend has no object store, so there is nowhere to put "
                "what it collects: a result that exists only in a completed "
                "activity's return value is a result RAVEL does not have"
            )
        job_id, digest = self.parse_ref(backend_job_ref)
        with self._session() as ssh:
            remote_dir = self._remote_directory_for(ssh, job_id, digest)
            if remote_dir is None:
                return JobOutputs(
                    completion_metadata=self.redact.mapping(
                        {
                            "backend": self.name,
                            "slurm_job_id": job_id,
                            "note": (
                                "the run's directory could not be located, so "
                                "nothing was collected; the scheduler no longer "
                                "reports where the job ran"
                            ),
                        }
                    )
                )
            submitted = self._read_submission(ssh, remote_dir)
            if submitted is None:
                return JobOutputs(
                    completion_metadata=self.redact.mapping(
                        {
                            "backend": self.name,
                            "slurm_job_id": job_id,
                            "remote_directory": remote_dir,
                            "note": (
                                "the run's directory holds no submission record, "
                                "so RAVEL cannot tell which project the result "
                                "belongs to and will not file it under a guess; "
                                "the files are on the cluster, in the directory "
                                "named here"
                            ),
                        }
                    )
                )
            listing = ssh.listdir(remote_dir)
            required = self._required_outputs(ssh, remote_dir, listing)
            fetched, missing = self._fetch(
                ssh,
                remote_dir,
                listing,
                required,
                Collected(
                    job_id=job_id,
                    project_id=submitted["project_id"],
                    node_id=submitted.get("node_id") or None,
                    database=database,
                    store=store,
                ),
            )
            logs = self._fetch_logs(ssh, remote_dir, listing)
            schedule = self._schedule_facts(ssh, job_id)
        return JobOutputs(
            artifacts=tuple(item.artifact_id for item in fetched),
            logs=tuple(logs),
            delivered_outputs=tuple(item.name for item in fetched),
            completion_metadata=self.redact.mapping(
                {
                    "backend": self.name,
                    "slurm_job_id": job_id,
                    "remote_directory": remote_dir,
                    "collected": {item.name: item.content_hash for item in fetched},
                    "required_outputs": list(required),
                    "missing_outputs": list(missing),
                    "log_files": list(logs),
                    **schedule,
                }
            ),
        )

    def cancel(self, backend_job_ref: str) -> bool:
        """Ask the scheduler to stop the job.

        Returns:
            Whether the scheduler accepted. A job that has already ended cannot
            be stopped, and that is not an error — the port says so — so it is
            reported as a refusal rather than raised.

        Raises:
            TransportError: The connection failed.
        """
        job_id, _digest = self.parse_ref(backend_job_ref)
        with self._session() as ssh:
            current = self._status_of(ssh, job_id)
            if current.state.is_terminal:
                return False
            result = ssh.run(
                self._scancel_command(job_id), timeout=self.target.command_timeout_seconds
            )
            return result.ok

    # ── Naming the work ─────────────────────────────────────────────────────

    @staticmethod
    def digest_for(request: JobRequest) -> str:
        """The digest that identifies one attempt of one node of one project.

        Derived from the work rather than from a counter, so the same attempt
        submitted twice — by a retried activity, by a restarted worker, by a
        second RAVEL process — is recognisably the same work. This is the same
        construction the mocks use, and it is deliberately the same: the
        idempotency key is a property of RAVEL's model of a run, not of which
        backend happens to be running it.
        """
        work = f"{request.project_id}/{request.node_id}/{request.attempt}"
        return hashlib.sha256(work.encode("utf-8")).hexdigest()[:REF_DIGEST_CHARS]

    def remote_directory(self, request: JobRequest) -> str:
        """Where this attempt's workspace lives on the cluster.

        One directory per project, then per node, then per attempt. Spelled out
        rather than hashed because an operator has to be able to find a run's
        files from the identifiers RAVEL shows them, and separate per attempt
        because a retry must not read the attempt before it.
        """
        return (
            f"{self.jobs_root.rstrip('/')}/{request.project_id}/"
            f"{request.node_id}/{request.attempt}"
        )

    def reference_for(self, request: JobRequest) -> str:
        """The digest a reference is built from, for a caller that has no job id."""
        return self.digest_for(request)

    def make_ref(self, job_id: str, digest: str) -> str:
        """The `backend_job_ref` one job is known by.

        Carries the scheduler's job id because `status` and `cancel` need
        nothing else, and the digest because `collect` has to find the
        directory again and the digest is the only part of the three-part key
        that fits. Both are needed and neither implies the other.
        """
        return f"slurm:{job_id}:{digest}"

    def parse_ref(self, backend_job_ref: str) -> tuple[str, str]:
        """Split a reference back into the job id and the digest.

        Raises:
            SlurmSubmissionError: The reference is not one this backend made.
                Refused rather than guessed at: a reference that cannot be read
                would otherwise become a command run against the wrong job.
        """
        parts = backend_job_ref.split(":")
        if len(parts) != 3 or parts[0] != "slurm" or not parts[1].isdigit():
            raise SlurmSubmissionError(
                f"{backend_job_ref!r} is not a reference this backend made; it "
                f"makes references shaped slurm:<job id>:<{REF_DIGEST_CHARS} hex digits>"
            )
        return parts[1], parts[2]

    # ── Talking to the cluster ──────────────────────────────────────────────

    def _session(self) -> _Session:
        """A connection for the length of one operation."""
        return _Session(self.connect)

    def _status_of(self, ssh: SSHTransport, job_id: str) -> JobStatus:
        """Read one job's state, from the queue or from the accounting log."""
        queued = ssh.run(self._squeue_command(job_id), timeout=self.target.command_timeout_seconds)
        if queued.ok and queued.output:
            reading = read_state(queued.output)
            return JobStatus(
                state=reading.state,
                backend_state=reading.backend_state,
                failure_class=reading.failure_class,
                detail=self.redact(f"{reading.detail} (squeue: {queued.output})"),
                progress={"slurm_job_id": job_id, "source": "squeue"},
            )
        accounting = ssh.run(
            self._sacct_command(job_id), timeout=self.target.command_timeout_seconds
        )
        if not accounting.ok:
            # Neither command answered. That is a failure to *observe* the job,
            # not a finding about it, and the difference decides who acts: a
            # status returned here would go through `decide_retry` and could
            # start a second attempt, while an exception makes Temporal retry
            # the poll. The attempt has not changed; only RAVEL's view of it
            # has, and a scheduler that is down is asked again rather than
            # answered for.
            raise TransportError(
                self.redact(
                    "the scheduler answered neither squeue nor sacct for job "
                    f"{job_id}, so nothing can be said about it: "
                    f"{queued.describe()}; {accounting.describe()}"
                )
            )
        state_text, exit_allocation, exit_batch, elapsed, max_rss = _sacct_fields(accounting)
        if not state_text:
            return JobStatus(
                state=JobState.FAILED,
                backend_state="ABSENT",
                failure_class=None,
                detail=self.redact(
                    f"job {job_id} is in neither the queue nor the accounting "
                    "log; the scheduler has no record of it, and RAVEL does not "
                    "retry a failure it cannot account for"
                ),
                progress={"slurm_job_id": job_id, "source": "none"},
            )
        reading = read_state(state_text)
        exit_code, exit_note = read_exit_code(exit_allocation, exit_batch)
        progress: dict[str, object] = {
            "slurm_job_id": job_id,
            "source": "sacct",
            "elapsed": elapsed,
            "max_rss": max_rss,
        }
        if exit_code is not None:
            progress["exit_code"] = exit_code
        detail = f"{reading.detail} (sacct: {state_text}"
        detail += f", exit {exit_code})" if exit_code is not None else f", {exit_note})"
        return JobStatus(
            state=reading.state,
            backend_state=reading.backend_state,
            failure_class=reading.failure_class,
            detail=self.redact(detail),
            progress=progress,
        )

    def _read_submission(self, ssh: SSHTransport, remote_dir: str) -> dict[str, str] | None:
        """This attempt's submission record, if the cluster already has one.

        Read rather than remembered, which is the point: it is the one check
        that survives a worker restart, a redeployment, and a second RAVEL
        process that never saw the first submission.

        A record that cannot be parsed is treated as absent, which costs a
        duplicate check against the scheduler rather than a duplicate job.
        """
        path = f"{remote_dir}/{SUBMISSION_FILE}"
        result = ssh.run(f"cat {_quote(path)}", timeout=self.target.command_timeout_seconds)
        if not result.ok or not result.output:
            return None
        try:
            document = json.loads(result.output)
        except ValueError:
            return None
        if not isinstance(document, dict):
            return None
        job_id = document.get("job_id")
        return document if isinstance(job_id, str) and job_id.isdigit() else None

    def _write_submission(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        job_id: str,
        digest: str,
        request: JobRequest,
    ) -> None:
        """Record what was submitted, in the run's own directory.

        Written after the job exists and before `submit` returns, so the window
        in which a crash produces a second job is the window of a single
        `sbatch` — and the scheduler's own name lookup covers even that.
        """
        document = json.dumps(
            {
                "project_id": request.project_id,
                "node_id": request.node_id,
                "attempt": request.attempt,
                "digest": digest,
                "job_id": job_id,
                "job_name": f"{JOB_NAME_PREFIX}{digest}",
                "execution_contract_ref": request.execution_contract_ref,
                "execution_contract_version": request.execution_contract_version,
                "remote_directory": remote_dir,
                "submitted_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
            sort_keys=True,
        )
        ssh.run(
            f"cat > {_quote(f'{remote_dir}/{SUBMISSION_FILE}')} <<'RAVEL_SUBMISSION'\n"
            f"{document}\nRAVEL_SUBMISSION",
            timeout=self.target.command_timeout_seconds,
        )

    def _find_by_name(self, ssh: SSHTransport, job_name: str) -> str | None:
        """Look for a job this attempt already has, by its deterministic name.

        Two lookups, because one does not cover both halves of a job's life:
        `squeue` sees it while it is queued or running, and `sacct` sees it
        after it has ended and the queue has forgotten it. This is the check
        that catches a submission made by a process that died before writing
        `submission.json`.
        """
        for command in (
            f"squeue -h -n {_quote(job_name)} -o %A",
            f"sacct -n -P -X --name={_quote(job_name)} -o JobID",
        ):
            result = ssh.run(command, timeout=self.target.command_timeout_seconds)
            if not result.ok:
                continue
            for line in result.output.splitlines():
                candidate = line.strip().split("|")[0].strip()
                if candidate.isdigit():
                    return candidate
        return None

    def _upload(
        self, ssh: SSHTransport, workspace: Path, remote_dir: str, entrypoint: str
    ) -> None:
        """Put the prepared workspace on the cluster.

        Every regular file, at the same relative path, so that what runs is what
        preparation built and hashed. A symlink is refused rather than followed:
        `is_file` is true of a link to a file, so a walk that only asked that
        would `scp` whatever the link points at — bytes from outside the
        workspace, under a name the manifest hashed as something else. Nothing
        legitimate puts one there (`Workspace._target` refuses to write through
        one, and refuses a name that is not a plain file name), so a link found
        here means the directory is not the one preparation built, and copying
        part of it to a cluster is not a thing to do quietly.

        Raises:
            SlurmSubmissionError: The workspace is not there, holds nothing, or
                holds a symlink.
        """
        if not workspace.is_dir():
            raise SlurmSubmissionError(
                f"the prepared workspace {workspace} is not on this machine; a "
                "run is prepared and submitted by the same worker, so a missing "
                "directory means the work was prepared somewhere else"
            )
        everything = sorted(workspace.rglob("*"))
        links = [path for path in everything if path.is_symlink()]
        if links:
            raise SlurmSubmissionError(
                f"the prepared workspace {workspace} holds symlinks "
                f"({', '.join(str(path.relative_to(workspace)) for path in links)}), "
                "and uploading one would send its target's bytes to the cluster "
                "under the name the manifest hashed"
            )
        files = [path for path in everything if path.is_file()]
        if not files:
            raise SlurmSubmissionError(f"the prepared workspace {workspace} is empty")
        ssh.run(f"mkdir -p {_quote(remote_dir)}", timeout=self.target.command_timeout_seconds)
        for path in files:
            relative = path.relative_to(workspace).as_posix()
            ssh.put(path, f"{remote_dir}/{relative}")
        if entrypoint.endswith(JOB_SCRIPT_SUFFIX):
            ssh.run(
                f"chmod +x {_quote(f'{remote_dir}/{entrypoint}')}",
                timeout=self.target.command_timeout_seconds,
            )

    def _sbatch(
        self, ssh: SSHTransport, job_name: str, remote_dir: str, entrypoint: str
    ) -> str:
        """Submit the job script and read back its id.

        `--parsable` makes sbatch print one machine-readable line, which is the
        only reason this can parse an id out of it at all; `--chdir` puts the
        job in its own directory, so the script's own `cd` and the scheduler
        agree about where it ran.

        Raises:
            SlurmSubmissionError: The script is not in the workspace, or the
                scheduler refused it.
        """
        if not entrypoint:
            raise SlurmSubmissionError(
                "the contract named no entrypoint, so there is no job script to submit"
            )
        script = f"{remote_dir}/{entrypoint}"
        result = ssh.run(
            f"test -f {_quote(script)} && sbatch --parsable --job-name={_quote(job_name)} "
            f"--chdir={_quote(remote_dir)} {_quote(script)}",
            timeout=self.target.command_timeout_seconds,
        )
        if not result.ok:
            raise SlurmSubmissionError(
                f"the scheduler refused the job: {self.redact(result.describe())}"
            )
        # `--parsable` answers `<job id>` or `<job id>;<cluster>`; the id is the
        # first field either way.
        job_id = result.output.split(";")[0].strip()
        if not job_id.isdigit():
            raise SlurmSubmissionError(
                f"the scheduler answered {result.output!r} instead of a job id; "
                "RAVEL will not guess which job it started"
            )
        return job_id

    # ── Getting the results back ────────────────────────────────────────────

    def _remote_directory_for(
        self, ssh: SSHTransport, job_id: str, digest: str
    ) -> str | None:
        """Find where a job ran, having only its id and the work's digest.

        The scheduler is asked first, because it recorded the answer when it
        started the job and it is the only party that knows. The fallback
        searches RAVEL's own jobs root for the submission record carrying this
        digest — a directory RAVEL made, bounded by RAVEL's layout, and the
        only option when accounting retention has purged a job that finished
        long ago.
        """
        result = ssh.run(
            f"sacct -n -P -X -j {job_id} -o WorkDir%512",
            timeout=self.target.command_timeout_seconds,
        )
        if result.ok:
            for line in result.output.splitlines():
                candidate = line.strip().split("|")[0].strip()
                if candidate.startswith("/"):
                    return candidate
        found = ssh.run(
            f"grep -rl --include={SUBMISSION_FILE} -m1 {_quote(digest)} "
            f"{_quote(self.jobs_root.rstrip('/'))} 2>/dev/null | head -1",
            timeout=self.target.command_timeout_seconds,
        )
        if found.ok and found.output:
            record = found.output.strip().splitlines()[0]
            return record.rsplit("/", 1)[0] or None
        return None

    def _required_outputs(
        self, ssh: SSHTransport, remote_dir: str, listing: tuple[RemoteFile, ...]
    ) -> tuple[str, ...]:
        """What the contract required, read from the manifest in the workspace.

        The manifest is the document preparation wrote to say what the run was
        for, and it names `required_outputs` for exactly this reader. Any
        `*_manifest.json` in the directory is read and the first one that names
        outputs wins, which keeps this backend from having to know whether it is
        looking at a computation's manifest or a laboratory's.
        """
        for entry in listing:
            if not entry.path.endswith("manifest.json"):
                continue
            result = ssh.run(
                f"cat {_quote(f'{remote_dir}/{entry.path}')}",
                timeout=self.target.command_timeout_seconds,
            )
            if not result.ok:
                continue
            try:
                document = json.loads(result.stdout)
            except ValueError:
                continue
            if not isinstance(document, dict):
                continue
            outputs = document.get("required_outputs")
            if isinstance(outputs, list) and outputs:
                return tuple(str(name) for name in outputs)
        return ()

    def _fetch(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        listing: tuple[RemoteFile, ...],
        required: Iterable[str],
        where: Collected,
    ) -> tuple[list[CollectedOutput], list[str]]:
        """Download each required output the run produced and store it.

        Returns what arrived, as the artifacts it was stored as, and the names
        of what did not. A missing required output is *reported*, not raised:
        the run ended and produced what it produced, and RAVEL's completeness
        check is the layer that decides what that means. A backend that raised
        here would turn an incomplete result into an infrastructure fault and
        retry the whole simulation.

        A required output may name a path (`output/conductivity.csv`) or a bare
        filename; both are matched, and by *path* first, so a run that produced
        two files of one name in different directories resolves the one the
        manifest named rather than whichever the listing happened to reach
        first.
        """
        by_name = {entry.path: entry for entry in listing}
        by_basename: dict[str, str] = {}
        for entry in listing:
            by_basename.setdefault(entry.path.rsplit("/", 1)[-1], entry.path)
        stored: list[CollectedOutput] = []
        missing: list[str] = []
        for name in required:
            relative = name if name in by_name else by_basename.get(name.rsplit("/", 1)[-1])
            if relative is None:
                missing.append(name)
                continue
            body = self._read(ssh, f"{remote_dir}/{relative}")
            if body is None:
                missing.append(name)
                continue
            stored.append(self._store(name, relative, body, where))
        return stored, missing

    def _store(
        self, name: str, relative: str, body: bytes, where: Collected
    ) -> CollectedOutput:
        """File one collected output as an artifact, without filing it twice.

        The project it belongs to is read from the run's own submission record
        rather than carried here, because there is nowhere to carry it: the port
        hands `collect` a reference and nothing else. That record is the one
        RAVEL wrote when it submitted the job, in the directory the job ran in,
        which makes it the only description of the run that is true of the run
        rather than of this process.

        Carries no `SIMULATED_KIND`: this is the one backend in V0 whose output
        came off a real machine, and marking it simulated would be the same lie
        in the other direction. It is not marked as *evidence* either — that is
        Review's judgement against the acceptance criteria, and a backend that
        asserted its own result was admissible would be deciding its own case.
        """
        # Hashed through the store's own function rather than with `hashlib`
        # here: what a content hash looks like in RAVEL is a property of the
        # store that records them, and a second spelling of it in a backend is a
        # backend that stops matching the rows it is comparing against.
        content_hash, _size = hash_chunks([body])
        with where.database.transaction() as session:
            artifacts = ArtifactRepository(session, where.project_id, where.store)
            settled = self._already_collected(
                artifacts, where, name, body, content_hash
            )
            if settled is None:
                artifact, version = artifacts.register(
                    name=name,
                    chunks=[body],
                    created_by=self.name,
                    filename=Path(relative).name,
                    media_type=_media_type(relative),
                    provenance=f"slurm:{where.job_id}",
                    node_id=where.node_id,
                    note=(
                        f"collected from Slurm job {where.job_id} in the run's remote "
                        "workspace; the scheduler's account of the job is in the "
                        "execution record"
                    ),
                )
                artifact_id, number = artifact.artifact_id, version.version
            else:
                artifact_id, number = settled
        return CollectedOutput(
            name=name,
            body=body,
            artifact_id=artifact_id,
            version=number,
            content_hash=content_hash,
        )

    def _already_collected(
        self,
        artifacts: ArtifactRepository,
        where: Collected,
        name: str,
        body: bytes,
        content_hash: str,
    ) -> tuple[str, int] | None:
        """What this project already holds for this output of this job.

        `collect` is an activity, and activities are retried — a dropped
        connection between the download and the insert is exactly the failure
        Temporal exists to absorb. Filing on every attempt would turn one run
        into as many artifacts as it took to get the bytes home, which is why
        this asks first. The question is narrow on purpose: this *name*, for
        this *node*, out of this *job*. Another job's output of the same name is
        a different result and is filed separately; a collection with no node to
        anchor it — a submission record written before node identity existed —
        cannot be matched at all and is filed as it always was.

        Two answers, and the difference between them is what the bytes say. The
        same bytes mean the retry is a retry and the version already stored is
        the version this collection delivers. Different bytes mean the run's
        directory changed under RAVEL between the two reads, and both readings
        are kept: a version is immutable, so the honest record of "this is what
        it held, and then this is what it held" is a second version rather than
        a second artifact or an overwrite.
        """
        if where.node_id is None:
            return None
        found = artifacts.all(
            name=name, node_id=where.node_id, provenance=f"slurm:{where.job_id}"
        )
        if not found:
            return None
        versions = artifacts.versions(found[-1].artifact_id)
        if not versions:
            return None
        newest = versions[-1]
        if newest.content_hash == content_hash:
            return newest.artifact_id, newest.version
        added = artifacts.add_version(
            newest.artifact_id,
            chunks=[body],
            created_by=self.name,
            filename=newest.filename,
            media_type=newest.media_type,
            node_id=where.node_id,
            note=(
                f"the same run's output read again from Slurm job {where.job_id} and "
                f"found to have changed since version {newest.version} was collected"
            ),
        )
        return added.artifact_id, added.version

    def _fetch_logs(
        self, ssh: SSHTransport, remote_dir: str, listing: tuple[RemoteFile, ...]
    ) -> dict[str, str]:
        """Download the scheduler's stdout file for the job.

        One file, because the job script sends stderr to it too — `#SBATCH
        --output` with no `--error` is how preparation writes it, and the two
        streams arriving together is what keeps a stack trace next to the line
        that caused it.
        """
        logs: dict[str, str] = {}
        for entry in listing:
            if not entry.path.endswith(".out"):
                continue
            result = ssh.run(
                f"cat {_quote(f'{remote_dir}/{entry.path}')}",
                timeout=self.target.command_timeout_seconds,
            )
            if result.ok:
                logs[entry.path] = result.stdout
        return logs

    def _read(self, ssh: SSHTransport, remote_path: str) -> bytes | None:
        """Read a remote file's bytes, or `None` if it is not readable.

        Through `base64` rather than a plain `cat` so that a result the
        software wrote as binaries — an `.xyz` trajectory, a compressed
        archive — survives the trip. A text-only read would silently replace
        every non-UTF-8 byte and hand RAVEL a file that hashes differently from
        the one on the cluster.
        """
        result = ssh.run(
            f"base64 -w0 {_quote(remote_path)}",
            timeout=self.target.command_timeout_seconds,
        )
        if not result.ok:
            return None
        try:
            return base64.b64decode(result.output, validate=True)
        except ValueError:
            return None

    def _schedule_facts(self, ssh: SSHTransport, job_id: str) -> dict[str, object]:
        """The scheduler's account of how the job went, for the record."""
        result = ssh.run(
            self._sacct_command(job_id), timeout=self.target.command_timeout_seconds
        )
        if not result.ok:
            return {}
        state_text, exit_allocation, exit_batch, elapsed, max_rss = _sacct_fields(result)
        if not state_text:
            return {}
        exit_code, note = read_exit_code(exit_allocation, exit_batch)
        facts: dict[str, object] = {"slurm_state": state_text}
        if elapsed:
            facts["elapsed"] = elapsed
        if max_rss:
            facts["max_rss"] = max_rss
        if exit_code is not None:
            facts["exit_code"] = exit_code
        elif note:
            facts["exit_code_note"] = note
        return facts

    # ── Command construction ────────────────────────────────────────────────

    def _squeue_command(self, job_id: str) -> str:
        """The job's state, or nothing when the queue no longer holds it."""
        return f"squeue -h -j {job_id} -o %T"

    def _sacct_command(self, job_id: str) -> str:
        """The job's accounting row: state, exit codes, time, peak memory.

        `-P` is parsable, pipe-separated output and `%512` widens the state
        field, because Slurm truncates it to fourteen characters by default and
        `CANCELLED by 1000` is longer than that.
        """
        return (
            f"sacct -n -P -j {job_id} "
            "-o JobID,State%512,ExitCode,Elapsed,MaxRSS"
        )

    def _scancel_command(self, job_id: str) -> str:
        """Stop the job."""
        return f"scancel {job_id}"

    # ── Plumbing ────────────────────────────────────────────────────────────

    def _handle(self, job_id: str, digest: str, how: str) -> JobHandle:
        """The handle for a job, with the state the scheduler reports for it."""
        return JobHandle(
            backend_job_ref=self.make_ref(job_id, digest),
            # A job just submitted is queued; RAVEL's first poll replaces this
            # with whatever the scheduler says. Reporting SUBMITTED here rather
            # than RUNNING is the honest reading of an `sbatch` that returned.
            state=JobState.SUBMITTED,
            backend_state=how,
        )


class _Session:
    """A connection used for the length of one operation and then closed.

    A class rather than `contextlib.contextmanager` so that a transport whose
    `close` raises cannot mask the error that was already on its way out.
    """

    def __init__(self, connect: Callable[[], SSHTransport]) -> None:
        self._connect = connect
        self._transport: SSHTransport | None = None

    def __enter__(self) -> SSHTransport:
        try:
            self._transport = self._connect()
        except TransportError:
            raise
        return self._transport

    def __exit__(self, *_: object) -> None:
        if self._transport is not None:
            # A close that fails has nothing left to fail at, and raising here
            # would replace whatever error is already on its way out.
            with suppress(Exception):
                self._transport.close()
        self._transport = None


def _sacct_fields(result: CommandResult) -> tuple[str, str, str, str, str]:
    """Read one job's accounting row: state, both exit codes, time, memory.

    `sacct` answers with a line per job *and per step*. The allocation's row is
    the one whose JobID has no dot — `<id>` rather than `<id>.batch` — and the
    batch step's exit code is the script's own, which is the one that says how
    the software ended.
    """
    state = allocation_exit = batch_exit = elapsed = max_rss = ""
    for line in result.output.splitlines():
        fields = line.split("|")
        if len(fields) < 5:
            continue
        job_id, job_state, exit_code, job_elapsed, job_rss = (field.strip() for field in fields[:5])
        if not job_id:
            continue
        if "." not in job_id:
            state = job_state
            allocation_exit = exit_code
            elapsed = job_elapsed
            max_rss = job_rss
        elif job_id.endswith(".batch"):
            batch_exit = exit_code
    return state, allocation_exit, batch_exit, elapsed, max_rss


def _media_type(filename: str) -> str:
    """A media type from the extension, or an honest absence.

    The set is small on purpose: a simulation writes text it configured itself
    and the two tabular formats a person reads, and everything else is
    `application/octet-stream` rather than a guess from a filename. A media type
    that is wrong is worse than one that says "some bytes" — a reader who trusts
    the first will try to parse them.
    """
    lowered = filename.lower()
    if lowered.endswith((".csv", ".tsv")):
        return "text/csv"
    if lowered.endswith(".json"):
        return "application/json"
    if lowered.endswith((".txt", ".log", ".out", ".input", ".data", ".def")):
        return "text/plain"
    return "application/octet-stream"


def _quote(value: str) -> str:
    """A shell word that means exactly the string it was given.

    Every path here is built from identifiers RAVEL minted — hex uuids, an
    attempt number, a contract reference — and single-quoting them is not a
    defence against a hostile value so much as a guarantee that a value which
    is *not* what it should be cannot become a second command. The remote
    command line is a string; this is the boundary where a string becomes
    something the shell will read.
    """
    return "'" + value.replace("'", "'\\''") + "'"


__all__ = [
    "BACKEND_NAME",
    "JOB_NAME_PREFIX",
    "JOB_SCRIPT_SUFFIX",
    "REDACTED",
    "REF_DIGEST_CHARS",
    "SUBMISSION_FILE",
    "SlurmComputeBackend",
    "SlurmConfigurationError",
    "SlurmSubmissionError",
    "SlurmTarget",
]
