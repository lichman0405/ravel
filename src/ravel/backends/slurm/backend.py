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
import json
import re
import threading
from collections.abc import Callable, Iterable, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from ravel.actions.idempotency import compute_submission_key
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

#: One lock per (target, submission key). Two threads submitting the same
#: attempt in one worker serialise here, so the second reads the record the
#: first wrote instead of racing it to `sbatch`. This is the in-process half of
#: duplicate prevention; the remote record and the scheduler's name lookup are
#: the half that survives a restart or a second process.
_SUBMISSION_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_SUBMISSION_LOCKS_GUARD = threading.Lock()

#: A target token is the first twelve hex digits of the target's UUID.
_TARGET_TOKEN = re.compile(r"^[0-9a-f]{12}$")

#: One path component RAVEL will interpolate into a remote path. Identifiers
#: are UUIDs, node ids and attempt numbers; anything else is a value that could
#: make the remote directory name mean something other than one run.
_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


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

    #: A machine-readable state for the cases where "it failed" is not enough.
    #: Legacy single-target submission raises the base class; target-mode
    #: submission distinguishes a rejection the scheduler stated from an
    #: outcome RAVEL could not observe.
    state = "REFUSED"


class SlurmSubmissionUncertain(SlurmSubmissionError):
    """`sbatch` was attempted and its outcome could not be established.

    This is `UNKNOWN`, not a failure. The scheduler may be running the job, may
    have rejected it, or may never have received the command. The one thing
    RAVEL may not do is submit again on the assumption that nothing happened:
    that is how one lost response becomes two allocations.

    The submission intent is on the cluster before `sbatch` is run, so a retry
    of this exception re-enters reconciliation rather than submission. Until a
    retry can *observe* that the job does not exist — both probes answering,
    and the reconciliation grace having passed — it raises this again.
    """

    state = "UNKNOWN"

    def __init__(
        self,
        detail: str,
        *,
        submission_key: str = "",
        target_id: str = "",
        job_id: str | None = None,
    ) -> None:
        super().__init__(detail)
        self.submission_key = submission_key
        self.target_id = target_id
        self.job_id = job_id


class SlurmSubmissionRejected(SlurmSubmissionError):
    """The scheduler stated a reason for refusing the job.

    A stated refusal is the opposite of an uncertain one: nothing was queued,
    and repeating the same command asks the same question. The state is kept so
    that a caller — and a test — can tell `PARTITION_REJECTED` from
    `ACCOUNT_REJECTED` from a generic refusal.
    """

    state = "REJECTED"

    def __init__(self, detail: str, *, state: str | None = None) -> None:
        super().__init__(detail)
        # A subclass names its state as a class attribute. Assigning the
        # base default unconditionally would overwrite it with "REJECTED";
        # only an explicit state is a caller's decision.
        if state is not None:
            self.state = state


class SlurmPartitionRejected(SlurmSubmissionRejected):
    """The target's partition is not one this cluster offers."""

    state = "PARTITION_REJECTED"


class SlurmAccountRejected(SlurmSubmissionRejected):
    """The target's account is not one this cluster accepts."""

    state = "ACCOUNT_REJECTED"


class SlurmSoftwareNotAllowed(SlurmSubmissionError):
    """The contract names software this target's catalog does not permit."""

    state = "SOFTWARE_NOT_ALLOWED"


class SlurmStateUnknown(TransportError):
    """The scheduler could not be observed, so no state can be reported.

    A subclass of `TransportError` because that is what the legacy path has
    always raised when neither `squeue` nor `sacct` answered; keeping the
    inheritance means an existing retry policy still retries the poll. What it
    adds is a state a caller can inspect: `SACCT_DELAY` when the queue had
    nothing and the accounting log could not be read, `UNKNOWN` when nothing
    answered at all.
    """

    def __init__(self, detail: str, *, state: str = "UNKNOWN", job_id: str = "") -> None:
        super().__init__(detail)
        self.state = state
        self.job_id = job_id


@dataclass(frozen=True, slots=True)
class CancellationOutcome:
    """What a scoped cancellation actually did.

    The port's `cancel` returns a bool because most callers only need "was it
    stopped". A target-mode caller that is deciding whether to record a
    cancellation, retry it, or refuse it needs the reason, and a bool cannot
    carry one. This can.
    """

    state: str
    cancelled: bool
    job_id: str = ""
    detail: str = ""

    @property
    def explicit(self) -> bool:
        """Whether the outcome is one of the named states rather than a fault."""
        return self.state in {
            "CANCELLED",
            "ALREADY_TERMINAL",
            "REFUSED_SCOPE",
            "UNKNOWN_JOB",
            "REFUSED",
        }


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
    #: Kept for the legacy single-target configuration. Target-mode targets
    #: never set it: `__post_init__` refuses a target that would auto-accept an
    #: unknown host key, because a host key accepted on first sight is not a
    #: host key check.
    trust_unknown_host: bool = False
    connect_timeout_seconds: float = 15.0
    command_timeout_seconds: float = 60.0
    #: The operator's id for this target. Empty on a legacy single-target
    #: deployment; non-empty on every target resolved from the allowlist.
    target_id: str = ""
    #: The file of trusted host keys. Required in target mode: strict
    #: verification against an *empty* key set refuses every host, which looks
    #: like a mismatch rather than the configuration mistake it is.
    known_hosts_filename: str | None = None
    partition: str | None = None
    account: str | None = None
    #: Where this target keeps run directories. `None` leaves the backend's
    #: `jobs_root` in charge, which is the legacy configuration.
    remote_root: str | None = None
    #: What this target may run. Empty means no catalog was declared; a name
    #: the contract does declare is checked against it when it is non-empty.
    allowed_software: tuple[str, ...] = ()
    #: The target's own concurrency ceiling. `None` means the T06 project quota
    #: is the only limit.
    max_jobs: int | None = None
    #: How long a submission intent may remain unconfirmed before an absent
    #: job is treated as a submission that never happened. Before it elapses,
    #: an absent job is `UNKNOWN`: the scheduler may still be about to show it.
    reconcile_grace_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise SlurmConfigurationError("a Slurm target needs a host")
        if not self.username.strip():
            raise SlurmConfigurationError("a Slurm target needs a username")
        if self.trust_unknown_host:
            raise SlurmConfigurationError(
                "auto-accepting an unknown host key is not permitted: a target "
                "must name a known_hosts file so the host is checked against a "
                "key somebody decided to trust"
            )
        if self.target_id:
            if not self.known_hosts_filename:
                raise SlurmConfigurationError(
                    f"target {self.target_id} names no known_hosts file, so its "
                    "host cannot be verified"
                )
            if self.remote_root is not None:
                self._validate_remote_root()
        if self.max_jobs is not None and self.max_jobs < 1:
            raise SlurmConfigurationError("a target's max_jobs must be at least one")
        if self.reconcile_grace_seconds < 0:
            raise SlurmConfigurationError(
                "a target's reconciliation grace cannot be negative"
            )

    def _validate_remote_root(self) -> None:
        root = self.remote_root or ""
        if not root.startswith("/"):
            raise SlurmConfigurationError(
                f"target {self.target_id} has a remote_root that is not absolute"
            )
        if ".." in root.split("/") or "\\" in root:
            raise SlurmConfigurationError(
                f"target {self.target_id} has a remote_root that could escape itself"
            )

    def __repr__(self) -> str:
        """A target whose secret is not in its own representation.

        A dataclass's generated `repr` prints every field, which would put the
        password into a traceback, a pytest failure, or a log line written by
        code that never meant to handle a credential. Presence is reported;
        the value never is.
        """
        password = "«redacted»" if self.password else None
        return (
            f"SlurmTarget(host={self.host!r}, username={self.username!r}, "
            f"port={self.port!r}, target_id={self.target_id!r}, "
            f"password={password!r}, key_filename={self.key_filename!r}, "
            f"known_hosts_filename={self.known_hosts_filename!r}, "
            f"partition={self.partition!r}, account={self.account!r})"
        )


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
    #: The operator's allowlist, keyed by `target_id`. `None` is the legacy
    #: single-target deployment: every request reaches `target`, and the
    #: command surface is exactly what it was before targets existed. A mapping
    #: turns on target mode, where a request names a target and the features
    #: that only make sense with an allowlist — scoped cancellation, extended
    #: accounting, intent records, parallel-safe quota — are in force.
    targets: Mapping[str, SlurmTarget] | None = None
    #: How to reach a named target. Required in target mode when there is more
    #: than one; `connect` remains the default target's connector.
    connect_for: Callable[[str], SSHTransport] | None = None
    #: The target used when a request does not name one. Empty means "the only
    #: enabled target", and is an error when there is more than one.
    default_target_id: str = ""
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
        secrets = {self.target.password}
        for target_id, target in (self.targets or {}).items():
            if target_id != target.target_id:
                raise SlurmConfigurationError(
                    f"the backend's target map keys {target_id!r} but the target "
                    f"calls itself {target.target_id!r}"
                )
            secrets.add(target.password)
        self.redact = Redactor(tuple(secret for secret in secrets if secret))
        if self.targets is not None and not self.targets:
            raise SlurmConfigurationError(
                "a target-mode backend needs at least one enabled target"
            )

    # ── The port ────────────────────────────────────────────────────────────

    def submit(self, request: JobRequest) -> JobHandle:
        """Hand work to the cluster, or return the work already handed over.

        In legacy single-target mode this is the three-check sequence the
        module docstring describes. In target mode it is the same sequence with
        two additions: the attempt's submission intent is written before
        `sbatch`, and an outcome that cannot be observed becomes an explicit
        `UNKNOWN` instead of a resubmission.

        Raises:
            SlurmSubmissionError: The request names no workspace to upload, or
                the scheduler refused the job. Both are faults in getting the
                work to the cluster rather than in the work, and both are safe
                to retry — see the module docstring.
            SlurmSubmissionUncertain: `sbatch` may or may not have reached the
                scheduler. Nothing is resubmitted; a later call reconciles
                against the record and the two scheduler probes first.
            TransportError: The connection failed. Propagated so the activity
                retries rather than recording a failed attempt that never ran.
        """
        if not request.workspace_path.strip():
            raise SlurmSubmissionError(
                "no workspace was prepared for this run, so there is nothing to "
                "upload and no job script to submit; a contract that names a "
                "Slurm environment has to be materialized before it can run"
            )
        if self.targets is None:
            return self._submit_legacy(request)
        return self._submit_target(request, self._target_for_request(request))

    def _submit_legacy(self, request: JobRequest) -> JobHandle:
        """The original single-target submission, with its exact command surface."""
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

    def _submit_target(self, request: JobRequest, target: SlurmTarget) -> JobHandle:
        """Submission against one allowlisted target, with recovery.

        The order is deliberate and each step is what makes the next one safe:

        1. The attempt's directory record is read. A record with a job id is
           the work already done; a `PREPARING` record means no `sbatch` was
           attempted; a `SUBMITTING` record means one was, and the question is
           no longer "should I submit" but "did it land".
        2. The scheduler is asked by deterministic name. This catches a record
           that never reached the cluster and a submission made by a process
           that died before writing one.
        3. Only then is the workspace uploaded and a `PREPARING` intent written.
        4. A `SUBMITTING` intent is written immediately before `sbatch`, so a
           process killed during the command leaves a fact rather than a gap.
        5. A response that cannot be read is reconciled against (1) and (2). It
           never causes a second `sbatch` in the same call.
        """
        self._validate_target_request(request, target)
        digest = self.digest_for(request)
        submission_key = self.submission_key_for(request)
        remote_dir = self._remote_directory_for_target(target, request)
        job_name = f"{JOB_NAME_PREFIX}{digest}"
        workspace = Path(request.workspace_path)
        entrypoint = self._validated_entrypoint(workspace, request.entrypoint)
        with self._target_lock(target, submission_key), self._session_for(target) as ssh:
            claimed = self._claim_remote_submission(ssh, remote_dir, target=target)
            record = self._read_record(ssh, remote_dir, target=target)
            if record is None and not claimed:
                # Another process created the claim after our local read and
                # before its record was visible. It may be mid-submit; probe
                # for the named job and otherwise refuse to create a second
                # submission under the same key.
                found = self._find_by_name(ssh, job_name, target=target)
                if found is not None:
                    self._write_target_submission(
                        ssh, remote_dir, found, digest, request, target, submission_key
                    )
                    return self._handle(
                        found, digest, "found on the cluster", target_id=target.target_id
                    )
                raise SlurmSubmissionUncertain(
                    self.redact(
                        f"submission {submission_key} is claimed by another process "
                        "and no scheduler job is visible yet; RAVEL will not submit "
                        "it again"
                    ),
                    submission_key=submission_key,
                    target_id=target.target_id,
                )
            if record is not None:
                self._check_record(record, request, target, submission_key)
                status = str(record.get("status") or "")
                job_id = record.get("job_id")
                if isinstance(job_id, str) and job_id.isdigit():
                    if status == "SUBMITTING":
                        # The record carries an id because a previous call
                        # adopted the job and was killed while rewriting it.
                        self._write_target_submission(
                            ssh, remote_dir, job_id, digest, request, target, submission_key
                        )
                    return self._handle(
                        job_id, digest, "submitted", target_id=target.target_id
                    )
                if status == "REJECTED":
                    recorded_state = str(record.get("state") or "REJECTED")
                    detail = str(
                        record.get("detail") or "the scheduler refused the job"
                    )
                    # A retry must see the same refusal as the call that got
                    # it, not a generic one: callers branch on the state.
                    rejection = {
                        "PARTITION_REJECTED": SlurmPartitionRejected,
                        "ACCOUNT_REJECTED": SlurmAccountRejected,
                    }.get(recorded_state, SlurmSubmissionRejected)
                    raise rejection(detail, state=recorded_state)
                if not claimed:
                    # The claim belongs to another process. It may be between
                    # its PREPARING write and its sbatch, or it may be dead;
                    # either way this call must not rewrite the record, upload
                    # or submit under the same key. Only a job the scheduler
                    # already reports is enough to adopt; otherwise the
                    # outcome stays UNKNOWN.
                    found = self._find_by_name(ssh, job_name, target=target)
                    if found is not None:
                        self._write_target_submission(
                            ssh, remote_dir, found, digest, request, target, submission_key
                        )
                        return self._handle(
                            found,
                            digest,
                            "found on the cluster",
                            target_id=target.target_id,
                        )
                    raise SlurmSubmissionUncertain(
                        self.redact(
                            f"submission {submission_key} is claimed by another "
                            "process and no scheduler job is visible yet; RAVEL "
                            "will not submit it again"
                        ),
                        submission_key=submission_key,
                        target_id=target.target_id,
                    )
                if status == "SUBMITTING":
                    # A previous call wrote the intent and then did not return.
                    # Do not submit. Reconcile, and only after both probes and
                    # the grace window agree the job is absent may this call
                    # proceed to a fresh submission.
                    found = self._find_by_name(ssh, job_name, target=target)
                    if found is not None:
                        self._write_target_submission(
                            ssh, remote_dir, found, digest, request, target, submission_key
                        )
                        return self._handle(
                            found, digest, "found on the cluster", target_id=target.target_id
                        )
                    healthy, detail = self._absence_confirmed(
                        ssh, job_name, target=target
                    )
                    if not healthy:
                        raise SlurmSubmissionUncertain(
                            self.redact(
                                f"submission {submission_key} was attempted and its "
                                f"outcome cannot be observed; the scheduler did not "
                                f"answer both probes ({detail}), so RAVEL will not "
                                "submit it again"
                            ),
                            submission_key=submission_key,
                            target_id=target.target_id,
                        )
                    if not self._grace_elapsed(record):
                        raise SlurmSubmissionUncertain(
                            self.redact(
                                f"submission {submission_key} was attempted and the "
                                "scheduler currently reports no such job; the "
                                "reconciliation grace has not passed, so the job may "
                                "still appear and RAVEL will not submit it again"
                            ),
                            submission_key=submission_key,
                            target_id=target.target_id,
                        )
                    # Both probes answered and the grace window has passed
                    # with no job in sight. The intent is spent: this call may
                    # prepare and submit again, and the PREPARING write below
                    # records that it did. Resetting the status is what turns
                    # the reconciliation above into a decision rather than a
                    # permanent UNKNOWN; without it a confirmed-absent
                    # submission could never be retried.
                    status = ""
                if status not in ("", "PREPARING"):
                    # A status this backend did not write. It says the
                    # directory belongs to a run whose state cannot be read,
                    # and submitting over it is how two jobs become one
                    # directory.
                    raise SlurmSubmissionUncertain(
                        self.redact(
                            f"the submission record in {remote_dir} has status "
                            f"{status!r}, which this backend does not understand, "
                            "so RAVEL cannot tell whether the job was submitted"
                        ),
                        submission_key=submission_key,
                        target_id=target.target_id,
                    )
                # PREPARING means no sbatch was attempted; the upload is
                # repeated rather than assumed, since the intent is written
                # before the transfer starts.
                if status in ("", "PREPARING"):
                    self._write_target_intent(
                        ssh, remote_dir, request, target, submission_key, "PREPARING"
                    )
                    self._upload(ssh, workspace, remote_dir, entrypoint, target=target)
            else:
                found = self._find_by_name(ssh, job_name, target=target)
                if found is not None:
                    self._write_target_submission(
                        ssh, remote_dir, found, digest, request, target, submission_key
                    )
                    return self._handle(
                        found, digest, "found on the cluster", target_id=target.target_id
                    )
                self._write_target_intent(
                    ssh, remote_dir, request, target, submission_key, "PREPARING"
                )
                self._upload(
                    ssh,
                    workspace,
                    remote_dir,
                    entrypoint,
                    target=target,
                )
            # A PREPARING record proves only that the workspace upload was
            # starting; rewrite it now, immediately before the one command that
            # can spend an allocation.
            self._write_target_intent(
                ssh, remote_dir, request, target, submission_key, "SUBMITTING"
            )
            try:
                job_id = self._sbatch(
                    ssh, job_name, remote_dir, entrypoint, target=target
                )
            except SlurmSubmissionRejected as rejected:
                self._write_target_rejection(
                    ssh, remote_dir, request, target, submission_key, rejected
                )
                raise
            except TransportError as error:
                return self._after_uncertain_submission(
                    ssh,
                    remote_dir,
                    request,
                    target,
                    submission_key,
                    job_name,
                    digest,
                    f"the connection failed while submitting: {error}",
                )
            if not job_id.isdigit():
                return self._after_uncertain_submission(
                    ssh,
                    remote_dir,
                    request,
                    target,
                    submission_key,
                    job_name,
                    digest,
                    f"the scheduler answered {job_id!r} instead of a job id",
                )
            self._write_target_submission(
                ssh, remote_dir, job_id, digest, request, target, submission_key
            )
        return self._handle(job_id, digest, "submitted", target_id=target.target_id)

    def _after_uncertain_submission(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        request: JobRequest,
        target: SlurmTarget,
        submission_key: str,
        job_name: str,
        digest: str,
        why: str,
    ) -> JobHandle:
        """Make one attempt to find the job, then say `UNKNOWN` rather than guess.

        The name lookup is allowed because it observes; a second `sbatch` is
        not, because it spends. If the job is there it is adopted. If it is not,
        the intent remains `SUBMITTING` and a later call reconciles again.
        """
        found = self._find_by_name(ssh, job_name, target=target)
        if found is not None:
            self._write_target_submission(
                ssh, remote_dir, found, digest, request, target, submission_key
            )
            return self._handle(found, digest, "found on the cluster", target_id=target.target_id)
        raise SlurmSubmissionUncertain(
            self.redact(
                f"{why}; submission {submission_key} is UNKNOWN, and RAVEL will "
                "not submit it again until a later call can confirm the job is absent"
            ),
            submission_key=submission_key,
            target_id=target.target_id,
        )

    # ── Targets, records and locks ──────────────────────────────────────────

    @staticmethod
    def submission_key_for(request: JobRequest) -> str:
        """The stable key of this attempt, in T06's spelling.

        `project_id`, `node_id`, `execution_contract_version`, `attempt` — the
        four facts that make two submissions the same work. The contract ref is
        deliberately not part of it: the version already identifies which
        contract ran, and a ref that changed on every rewrite would turn one
        attempt into two.
        """
        return compute_submission_key(
            project_id=request.project_id,
            node_id=request.node_id,
            contract_version=request.execution_contract_version,
            attempt=request.attempt,
        )

    @staticmethod
    def target_token(target_id: str) -> str:
        """The short, stable spelling of a target id that fits in a REF column.

        A reference holds sixty-four characters; a full UUID plus a job id plus
        a digest does not fit. Twelve hex digits of the UUID is enough to
        distinguish the handful of targets one deployment configures, and the
        backend refuses an ambiguous ref rather than choosing a cluster.
        """
        return target_id.replace("-", "")[:12]

    def _target_for_request(self, request: JobRequest) -> SlurmTarget:
        """The target a request named, or this deployment's default."""
        targets = self.targets or {}
        wanted = request.target_id or self.default_target_id
        if not wanted:
            if len(targets) == 1:
                return next(iter(targets.values()))
            raise SlurmConfigurationError(
                "this request names no target and the deployment has "
                f"{len(targets)} allowed targets, so RAVEL cannot choose one: "
                f"configured: {', '.join(sorted(targets)) or 'none'}"
            )
        if wanted not in targets:
            raise SlurmConfigurationError(
                f"target_id {wanted!r} is not one of this deployment's allowed "
                f"targets; configured: {', '.join(sorted(targets)) or 'none'}"
            )
        return targets[wanted]

    def _target_for_ref(self, backend_job_ref: str) -> tuple[SlurmTarget, str, str]:
        """The target and the two identifying parts of one of this backend's refs."""
        target_id, job_id, digest = self._split_ref(backend_job_ref)
        if target_id is None:
            if self.targets is not None and len(self.targets) > 1:
                raise SlurmSubmissionError(
                    "a target-mode backend needs a target-qualified reference; "
                    f"{backend_job_ref!r} does not say which target the job is on"
                )
            return self.target, job_id, digest
        matches = [
            target
            for target in (self.targets or {}).values()
            if self.target_token(target.target_id) == target_id
        ]
        if len(matches) != 1:
            raise SlurmSubmissionError(
                f"reference {backend_job_ref!r} names target token {target_id!r}, "
                "which this backend cannot resolve to exactly one target"
            )
        return matches[0], job_id, digest

    def _split_ref(self, backend_job_ref: str) -> tuple[str | None, str, str]:
        """Split a legacy or target-qualified reference.

        Target-qualified references are
        `slurm:<12 hex of target id>:<job id>:<digest>`; legacy ones are the
        original `slurm:<job id>:<digest>`. Both are accepted so jobs submitted
        before targets existed remain readable.
        """
        parts = backend_job_ref.split(":")
        if len(parts) == 3 and parts[0] == "slurm" and parts[1].isdigit():
            return None, parts[1], parts[2]
        if (
            len(parts) == 4
            and parts[0] == "slurm"
            and _is_target_token(parts[1])
            and parts[2].isdigit()
        ):
            return parts[1], parts[2], parts[3]
        raise SlurmSubmissionError(
            f"{backend_job_ref!r} is not a reference this backend made; it "
            f"makes references shaped slurm:<job id>:<{REF_DIGEST_CHARS} hex digits> "
            "or slurm:<target token>:<job id>:<digest>"
        )

    def _target_lock(self, target: SlurmTarget, submission_key: str):
        """One in-process lock per target and attempt.

        This is the cheap half of duplicate prevention: two threads submitting
        the same work in one process serialise here, so the second one reads
        the record the first wrote instead of racing it to `sbatch`. The other
        half is on the cluster, and is what covers a second process.
        """
        key = (target.target_id, submission_key)
        with _SUBMISSION_LOCKS_GUARD:
            lock = _SUBMISSION_LOCKS.setdefault(key, threading.Lock())
        return lock

    @contextmanager
    def _session_for(self, target: SlurmTarget):
        """A connection to one target, released at the end of the operation."""
        with _Session(lambda: self._connect_to(target)) as ssh:
            yield ssh

    def _connect_to(self, target: SlurmTarget) -> SSHTransport:
        if target is self.target or target.target_id == self.target.target_id:
            return self.connect()
        if self.connect_for is None:
            raise SlurmConfigurationError(
                f"target {target.target_id} has no connector: this backend was "
                "built with one default connection and cannot reach a second host"
            )
        return self.connect_for(target.target_id)

    def _remote_directory_for_target(
        self, target: SlurmTarget, request: JobRequest
    ) -> str:
        root = (target.remote_root or self.jobs_root).rstrip("/")
        project = _safe_component(request.project_id, "project_id")
        node = _safe_component(request.node_id, "node_id")
        attempt = _safe_component(str(request.attempt), "attempt")
        return f"{root}/{project}/{node}/{attempt}"

    def _validate_target_request(self, request: JobRequest, target: SlurmTarget) -> None:
        """Checks that must pass before an upload or an `sbatch`."""
        _safe_component(request.project_id, "project_id")
        _safe_component(request.node_id, "node_id")
        self._validate_software(request, target)
        self._validated_entrypoint(Path(request.workspace_path), request.entrypoint)

    def _validate_software(self, request: JobRequest, target: SlurmTarget) -> None:
        """Refuse software the target's catalog does not list.

        A deployment that declared a catalog has said what its allocation may
        be spent on. A contract naming anything else is refused before
        `sbatch`, not after the allocation is gone. A contract naming nothing
        is not guessed at: older RunPlans do not carry execution requirements,
        and inventing a software name would be worse than recording that it was
        absent.
        """
        software = _requested_software(request)
        if software and target.allowed_software and software not in target.allowed_software:
            raise SlurmSoftwareNotAllowed(
                f"target {target.target_id} does not allow software {software!r}; "
                f"its catalog names {', '.join(target.allowed_software) or 'nothing'}"
            )

    @staticmethod
    def _validated_entrypoint(workspace: Path, entrypoint: str) -> str:
        """A relative, non-escaping entrypoint that names a file in the workspace.

        The entrypoint is interpolated into a remote command and used to decide
        what is executable on the far side. An absolute path, a `..` segment, a
        backslash, or a symlink would each mean the job script is not the file
        preparation built.
        """
        if not entrypoint:
            raise SlurmSubmissionError(
                "the contract named no entrypoint, so there is no job script to submit"
            )
        candidate = PurePosixPath(entrypoint)
        if candidate.is_absolute() or "\\" in entrypoint:
            raise SlurmSubmissionError(
                f"entrypoint {entrypoint!r} must be a relative POSIX path inside "
                "the prepared workspace"
            )
        if any(part in ("", ".", "..") for part in candidate.parts):
            raise SlurmSubmissionError(
                f"entrypoint {entrypoint!r} has an empty, '.' or '..' segment, "
                "so it does not name one file in the workspace"
            )
        if workspace.is_dir():
            lexical = workspace / candidate
            target = lexical.resolve()
            root = workspace.resolve()
            if not target.is_relative_to(root):
                raise SlurmSubmissionError(
                    f"entrypoint {entrypoint!r} resolves outside the prepared "
                    f"workspace {workspace}, so it is not part of what was built"
                )
            # Check the names before `resolve` erased them. A link in the
            # entrypoint's own path is refused even when it points at a file
            # inside the workspace: the manifest hashed the name preparation
            # wrote, and a link would run the target's bytes under it.
            through_link = lexical.is_symlink()
            for parent in lexical.parents:
                if parent == workspace:
                    break
                through_link = through_link or parent.is_symlink()
            if through_link:
                raise SlurmSubmissionError(
                    f"entrypoint {entrypoint!r} passes through a symlink, and a "
                    "job script that is a link is not a file preparation built"
                )
        return candidate.as_posix()

    def _read_record(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        *,
        target: SlurmTarget | None = None,
    ) -> dict[str, object] | None:
        """The run's record whether or not it has a job id yet.

        Unlike `_read_submission`, an intent record with no job id is data: it
        says a submission was attempted and its outcome is not yet known.

        Absence is checked separately from readability. `cat` alone cannot tell
        "there is no record" from "the command failed", and the difference is
        the whole of AC1: treating a failed read as absence would let this call
        proceed to `sbatch` with a record it never saw. A missing file is
        therefore confirmed with `test -e`, a failed command is a transport
        fault, and a record that exists but cannot be parsed is `UNKNOWN`.
        """
        target = target or self.target
        path = f"{remote_dir}/{SUBMISSION_FILE}"
        result = ssh.run(
            f"if [ -e {_quote(path)} ]; then cat {_quote(path)}; fi",
            timeout=target.command_timeout_seconds,
        )
        if not result.ok:
            raise TransportError(
                self.redact(
                    f"the submission record at {path} could not be read: "
                    f"{result.describe()}"
                )
            )
        if not result.stdout.strip():
            return None
        try:
            document = json.loads(result.stdout)
        except ValueError:
            raise SlurmSubmissionUncertain(
                self.redact(
                    f"the submission record at {path} exists but is not readable "
                    "JSON, so RAVEL cannot tell whether this attempt was already "
                    "submitted and will not submit it again"
                ),
            ) from None
        if not isinstance(document, dict):
            raise SlurmSubmissionUncertain(
                self.redact(
                    f"the submission record at {path} is not an object, so RAVEL "
                    "cannot tell whether this attempt was already submitted"
                ),
            )
        return document

    def _check_record(
        self,
        record: Mapping[str, object],
        request: JobRequest,
        target: SlurmTarget,
        submission_key: str,
    ) -> None:
        """Refuse a record that describes different work or a different target.

        The directory is derived from the work, so a mismatch means the key
        collided or the directory is not RAVEL's. Adopting it would attach this
        project's attempt to another project's job.
        """
        expected = {
            "project_id": request.project_id,
            "node_id": request.node_id,
            "attempt": request.attempt,
            "execution_contract_version": request.execution_contract_version,
            "submission_key": submission_key,
            "target_id": target.target_id,
        }
        for field_name, value in expected.items():
            recorded = record.get(field_name)
            if recorded is None and field_name in ("submission_key", "target_id"):
                # A record from before intents existed; the directory and job
                # name still bind it to this work.
                continue
            if str(recorded) != str(value):
                raise SlurmSubmissionError(
                    self.redact(
                        f"the record in {request.project_id}/{request.node_id}/"
                        f"{request.attempt} says {field_name}={recorded!r}, but this "
                        f"request is {value!r}; RAVEL will not adopt a job that "
                        "belongs to different work"
                    )
                )

    def _absence_confirmed(
        self,
        ssh: SSHTransport,
        job_name: str,
        *,
        target: SlurmTarget | None = None,
    ) -> tuple[bool, str]:
        """Ask both scheduler probes whether the job is absent.

        Returns `(healthy, detail)`, where healthy means both commands answered
        *and* neither found a job. A failed probe is not evidence of absence:
        that is the distinction between "the scheduler says no" and "RAVEL
        could not ask", and only the first permits a resubmission.
        """
        target = target or self.target
        timeout = target.command_timeout_seconds
        squeue = ssh.run(
            f"squeue -h -n {_quote(job_name)} -o %A",
            timeout=timeout,
        )
        sacct = ssh.run(
            f"sacct -n -P -X --name={_quote(job_name)} -o JobID",
            timeout=timeout,
        )
        if not squeue.ok or not sacct.ok:
            return False, f"{squeue.describe()}; {sacct.describe()}"
        for result in (squeue, sacct):
            for line in result.output.splitlines():
                if line.strip().split("|")[0].strip().isdigit():
                    return False, "the scheduler still reports the job"
        return True, "both probes answered and neither found the job"

    @staticmethod
    def _grace_elapsed(record: Mapping[str, object]) -> bool:
        """Whether a submission intent is old enough to treat absence as final."""
        raw = record.get("intent_at")
        if not isinstance(raw, str):
            return False
        try:
            written = datetime.fromisoformat(raw)
        except ValueError:
            return False
        if written.tzinfo is None:
            written = written.replace(tzinfo=UTC)
        age = (datetime.now(UTC) - written).total_seconds()
        raw_grace = record.get("reconcile_grace_seconds")
        grace = float(raw_grace) if isinstance(raw_grace, (int, float)) else 0.0
        return age >= grace

    def _write_target_intent(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        request: JobRequest,
        target: SlurmTarget,
        submission_key: str,
        status: str,
    ) -> None:
        """Write the fact that this attempt is about to be submitted.

        Two statuses matter. `PREPARING` is written before the upload and says
        no `sbatch` has run. `SUBMITTING` is written immediately before it and
        is what makes a lost response recoverable: a later process knows to
        reconcile rather than assume nothing happened.
        """
        self._write_target_record(
            ssh,
            remote_dir,
            {
                "status": status,
                "submission_key": submission_key,
                "project_id": request.project_id,
                "node_id": request.node_id,
                "attempt": request.attempt,
                "execution_contract_ref": request.execution_contract_ref,
                "execution_contract_version": request.execution_contract_version,
                "target_id": target.target_id,
                "target_host": target.host,
                "job_id": None,
                "job_name": f"{JOB_NAME_PREFIX}{self.digest_for(request)}",
                "remote_directory": remote_dir,
                "software": _requested_software(request) or None,
                "intent_at": datetime.now(UTC).isoformat(),
                "reconcile_grace_seconds": target.reconcile_grace_seconds,
            },
            target=target,
        )

    def _write_target_submission(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        job_id: str,
        digest: str,
        request: JobRequest,
        target: SlurmTarget,
        submission_key: str,
    ) -> None:
        """Record a confirmed job id, after the scheduler has shown it exists."""
        self._write_target_record(
            ssh,
            remote_dir,
            {
                "status": "SUBMITTED",
                "submission_key": submission_key,
                "project_id": request.project_id,
                "node_id": request.node_id,
                "attempt": request.attempt,
                "execution_contract_ref": request.execution_contract_ref,
                "execution_contract_version": request.execution_contract_version,
                "target_id": target.target_id,
                "target_host": target.host,
                "job_id": job_id,
                "job_name": f"{JOB_NAME_PREFIX}{digest}",
                "digest": digest,
                "remote_directory": remote_dir,
                "software": _requested_software(request) or None,
                "submitted_at": datetime.now(UTC).isoformat(),
                "reconcile_grace_seconds": target.reconcile_grace_seconds,
            },
            target=target,
        )

    def _write_target_rejection(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        request: JobRequest,
        target: SlurmTarget,
        submission_key: str,
        rejected: SlurmSubmissionRejected,
    ) -> None:
        """Record a stated refusal so a retry does not re-ask the scheduler."""
        self._write_target_record(
            ssh,
            remote_dir,
            {
                "status": "REJECTED",
                "state": rejected.state,
                "detail": self.redact(str(rejected)),
                "submission_key": submission_key,
                "project_id": request.project_id,
                "node_id": request.node_id,
                "attempt": request.attempt,
                "execution_contract_version": request.execution_contract_version,
                "target_id": target.target_id,
                "job_id": None,
                "remote_directory": remote_dir,
                "software": _requested_software(request) or None,
            },
            target=target,
        )

    def _claim_remote_submission(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        *,
        target: SlurmTarget | None = None,
    ) -> bool:
        """Create one atomic remote claim for this submission key.

        ``mkdir`` without ``-p`` is the cross-process compare-and-set the local
        lock cannot provide: the first process creates the claim directory and
        the second gets ``File exists``. The claim is separate from the record
        because the record is written after the claim; a process killed between
        the two leaves a claim with no record, which the caller treats as
        UNKNOWN rather than submitting a second time.
        """
        target = target or self.target
        claim = f"{remote_dir}.claim"
        parent = claim.rsplit("/", 1)[0]
        result = ssh.run(
            f"mkdir -p {_quote(parent)} && mkdir {_quote(claim)}",
            timeout=target.command_timeout_seconds,
        )
        if result.ok:
            return True
        detail = result.describe()
        if "exists" in detail.lower():
            return False
        raise SlurmSubmissionError(
            self.redact(
                "the remote submission claim could not be created, so RAVEL "
                f"will not submit work it cannot make unique: {detail}"
            )
        )

    def _write_target_record(
        self,
        ssh: SSHTransport,
        remote_dir: str,
        document: Mapping[str, object],
        *,
        target: SlurmTarget | None = None,
    ) -> None:
        """Write the run's record, refusing to continue if it could not be written.

        The record is what makes a retry safe. Proceeding to `sbatch` after the
        `SUBMITTING` intent failed to write would spend an allocation no later
        process could see, so a failed write is a retryable submission fault,
        never a warning.
        """
        target = target or self.target
        body = json.dumps(dict(document), indent=2, sort_keys=True)
        # The directory first, in the same shell command: the intent is written
        # before the workspace upload, so on the first submission of an attempt
        # the directory does not exist yet. `&&` makes a failure to create it a
        # failure to write the record, which is the case that must stop the
        # submission rather than proceed without a recoverable fact.
        result = ssh.run(
            f"mkdir -p {_quote(remote_dir)} && "
            f"cat > {_quote(f'{remote_dir}/{SUBMISSION_FILE}')} <<'RAVEL_SUBMISSION'\n"
            f"{body}\nRAVEL_SUBMISSION",
            timeout=target.command_timeout_seconds,
        )
        if not result.ok:
            raise SlurmSubmissionError(
                self.redact(
                    "the submission record could not be written, so RAVEL will "
                    f"not submit work it could not make recoverable: {result.describe()}"
                )
            )

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
        target, job_id, _digest = self._target_for_ref(backend_job_ref)
        with self._session_for(target) as ssh:
            return self._status_of(ssh, job_id, target=target)

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
        target, job_id, digest = self._target_for_ref(backend_job_ref)
        with self._session_for(target) as ssh:
            remote_dir = self._remote_directory_for(ssh, job_id, digest, target=target)
            if remote_dir is not None and self.targets is not None:
                root = (target.remote_root or self.jobs_root).rstrip("/")
                if not remote_dir.startswith(root + "/"):
                    # The scheduler's WorkDir is not authoritative about which
                    # directory RAVEL may read. A result inside another project's
                    # workspace is not this job's to collect.
                    return JobOutputs(
                        completion_metadata=self.redact.mapping(
                            {
                                "backend": self.name,
                                "slurm_job_id": job_id,
                                "target_id": target.target_id,
                                "note": (
                                    f"the scheduler reported {remote_dir}, which is "
                                    f"outside this target's jobs root {root}; RAVEL "
                                    "will not collect a directory it does not own"
                                ),
                            }
                        )
                    )
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
            schedule = self._schedule_facts(ssh, job_id, target=target)
        metadata: dict[str, object] = {
            "backend": self.name,
            "slurm_job_id": job_id,
            "remote_directory": remote_dir,
            "collected": {item.name: item.content_hash for item in fetched},
            "required_outputs": list(required),
            "missing_outputs": list(missing),
            "log_files": list(logs),
            **schedule,
        }
        if self.targets is not None:
            metadata.update(
                {
                    "target_id": target.target_id,
                    "submission_key": submitted.get("submission_key") or "UNKNOWN",
                    "manifest_files": [
                        entry.path
                        for entry in listing
                        if entry.path.endswith("manifest.json")
                    ],
                }
            )
        return JobOutputs(
            artifacts=tuple(item.artifact_id for item in fetched),
            logs=tuple(logs),
            delivered_outputs=tuple(item.name for item in fetched),
            completion_metadata=self.redact.mapping(metadata),
        )

    def cancel(self, backend_job_ref: str, *, project_id: str | None = None) -> bool:
        """Ask the scheduler to stop the job.

        In legacy mode the check is exactly the original one. In target mode it
        is scoped: the reference must name a target this backend knows, the
        run's own record must agree on the target and (when the caller says who
        it is) on the project, and only then is `scancel` run for that one job
        id. A cancellation that cannot establish scope is refused, not widened.

        Returns:
            Whether the scheduler accepted. A job that has already ended cannot
            be stopped, and that is not an error — the port says so — so it is
            reported as a refusal rather than raised.

        Raises:
            TransportError: The connection failed.
        """
        if self.targets is not None:
            return self.cancel_with_state(
                backend_job_ref, project_id=project_id
            ).cancelled
        job_id, _digest = self.parse_ref(backend_job_ref)
        with self._session() as ssh:
            current = self._status_of(ssh, job_id)
            if current.state.is_terminal:
                return False
            result = ssh.run(
                self._scancel_command(job_id), timeout=self.target.command_timeout_seconds
            )
            return result.ok

    def cancel_with_state(
        self, backend_job_ref: str, *, project_id: str | None = None
    ) -> CancellationOutcome:
        """Cancel one target-mode job, reporting the reason as an explicit state.

        The states are the answer to "why did this not stop my job?":
        `ALREADY_TERMINAL`, `UNKNOWN_JOB`, `REFUSED_SCOPE`, `REFUSED`, and
        `CANCELLED`. None of them is a fault in the connection; a connection
        fault still raises, because it says nothing about the job.
        """
        if self.targets is None:
            cancelled = self.cancel(backend_job_ref, project_id=project_id)
            return CancellationOutcome(
                state="CANCELLED" if cancelled else "REFUSED", cancelled=cancelled
            )
        target, job_id, digest = self._target_for_ref(backend_job_ref)
        with self._session_for(target) as ssh:
            remote_dir = self._remote_directory_for(
                ssh, job_id, digest, target=target
            )
            record = self._read_record(ssh, remote_dir) if remote_dir else None
            if record is None:
                return CancellationOutcome(
                    state="UNKNOWN_JOB",
                    cancelled=False,
                    job_id=job_id,
                    detail=self.redact(
                        "no submission record for this job could be found, so "
                        "RAVEL cannot establish which project and target it "
                        "belongs to and will not cancel it"
                    ),
                )
            recorded_target = str(record.get("target_id") or "")
            recorded_project = str(record.get("project_id") or "")
            recorded_job = str(record.get("job_id") or "")
            if (
                recorded_target != target.target_id
                or recorded_job != job_id
                or (project_id is not None and recorded_project != project_id)
            ):
                return CancellationOutcome(
                    state="REFUSED_SCOPE",
                    cancelled=False,
                    job_id=job_id,
                    detail=self.redact(
                        "the job's own record does not match this cancellation's "
                        f"target/project (recorded target {recorded_target or 'none'}, "
                        f"project {recorded_project or 'none'}, job "
                        f"{recorded_job or 'none'}); RAVEL will not cancel a job "
                        "outside the scope that asked for it"
                    ),
                )
            current = self._status_of(ssh, job_id, target=target)
            if current.state.is_terminal:
                return CancellationOutcome(
                    state="ALREADY_TERMINAL",
                    cancelled=False,
                    job_id=job_id,
                    detail=current.detail,
                )
            result = ssh.run(
                self._scancel_command(job_id),
                timeout=target.command_timeout_seconds,
            )
            return CancellationOutcome(
                state="CANCELLED" if result.ok else "REFUSED",
                cancelled=result.ok,
                job_id=job_id,
                detail=self.redact(
                    result.describe()
                    if not result.ok
                    else f"the scheduler accepted scancel for job {job_id}"
                ),
            )

    # ── Naming the work ─────────────────────────────────────────────────────

    @staticmethod
    def digest_for(request: JobRequest) -> str:
        """The digest that identifies one attempt of one node of one project.

        Derived from the work rather than from a counter, so the same attempt
        submitted twice — by a retried activity, by a restarted worker, by a
        second RAVEL process — is recognisably the same work. It is T06's
        submission key truncated to the sixteen characters a `REF` column and
        a Slurm job name can hold, which is what makes the backend's job name
        and the Action's external key the same statement about the work rather
        than two spellings of it.
        """
        key = SlurmComputeBackend.submission_key_for(request)
        return key.split(":", 1)[1][:REF_DIGEST_CHARS]

    def remote_directory(self, request: JobRequest) -> str:
        """Where this attempt's workspace lives on the cluster.

        One directory per project, then per node, then per attempt. Spelled out
        rather than hashed because an operator has to be able to find a run's
        files from the identifiers RAVEL shows them, and separate per attempt
        because a retry must not read the attempt before it.
        """
        return self._remote_directory_for_target(self.target, request)

    def reference_for(self, request: JobRequest) -> str:
        """The digest a reference is built from, for a caller that has no job id."""
        return self.digest_for(request)

    def make_ref(self, job_id: str, digest: str, target_id: str = "") -> str:
        """The `backend_job_ref` one job is known by.

        Carries the scheduler's job id because `status` and `cancel` need
        nothing else, and the digest because `collect` has to find the
        directory again and the digest is the only part of the three-part key
        that fits. In target mode it also carries a short token for the target:
        job ids are only unique within one cluster, and a reference that did
        not say which cluster would be a command waiting to hit the wrong one.
        """
        if target_id:
            return f"slurm:{self.target_token(target_id)}:{job_id}:{digest}"
        return f"slurm:{job_id}:{digest}"

    def parse_ref(self, backend_job_ref: str) -> tuple[str, str]:
        """Split a reference back into the job id and the digest.

        Raises:
            SlurmSubmissionError: The reference is not one this backend made.
                Refused rather than guessed at: a reference that cannot be read
                would otherwise become a command run against the wrong job.
        """
        _target_id, job_id, digest = self._split_ref(backend_job_ref)
        return job_id, digest

    # ── Talking to the cluster ──────────────────────────────────────────────

    def _session(self) -> _Session:
        """A connection for the length of one operation."""
        return _Session(self.connect)

    def _status_of(
        self, ssh: SSHTransport, job_id: str, *, target: SlurmTarget | None = None
    ) -> JobStatus:
        """Read one job's state, from the queue or from the accounting log."""
        target = target or self.target
        timeout = target.command_timeout_seconds
        queued = ssh.run(self._squeue_command(job_id), timeout=timeout)
        if queued.ok and queued.output:
            reading = read_state(queued.output)
            progress: dict[str, object] = {
                "slurm_job_id": job_id,
                "source": "squeue",
            }
            if target.target_id:
                progress["target_id"] = target.target_id
            return JobStatus(
                state=reading.state,
                backend_state=reading.backend_state,
                failure_class=reading.failure_class,
                detail=self.redact(f"{reading.detail} (squeue: {queued.output})"),
                progress=progress,
            )
        accounting = ssh.run(
            self._sacct_command(job_id, target=target), timeout=timeout
        )
        if not accounting.ok:
            # Neither command answered. That is a failure to *observe* the job,
            # not a finding about it, and the difference decides who acts: a
            # status returned here would go through `decide_retry` and could
            # start a second attempt, while an exception makes Temporal retry
            # the poll. The attempt has not changed; only RAVEL's view of it
            # has, and a scheduler that is down is asked again rather than
            # answered for. `SACCT_DELAY` names the specific shape where the
            # queue had nothing and accounting could not be read.
            raise SlurmStateUnknown(
                self.redact(
                    "the scheduler answered neither squeue nor sacct for job "
                    f"{job_id}, so nothing can be said about it: "
                    f"{queued.describe()}; {accounting.describe()}"
                ),
                state="SACCT_DELAY",
                job_id=job_id,
            )
        facts = _sacct_fields(accounting, extended=bool(target.target_id))
        if not facts.state:
            progress = {"slurm_job_id": job_id, "source": "none"}
            if target.target_id:
                progress["target_id"] = target.target_id
            return JobStatus(
                state=JobState.FAILED,
                backend_state="ABSENT",
                failure_class=None,
                detail=self.redact(
                    f"job {job_id} is in neither the queue nor the accounting "
                    "log; the scheduler has no record of it, and RAVEL does not "
                    "retry a failure it cannot account for"
                ),
                progress=progress,
            )
        reading = read_state(facts.state)
        exit_code, exit_note = read_exit_code(facts.allocation_exit, facts.batch_exit)
        progress = {
            "slurm_job_id": job_id,
            "source": "sacct",
            "elapsed": facts.elapsed or ("UNKNOWN" if target.target_id else ""),
            "max_rss": facts.max_rss or ("UNKNOWN" if target.target_id else ""),
        }
        if target.target_id:
            progress["target_id"] = target.target_id
            # A scheduler that did not report an allocation did not report
            # zero: an absent metering value is UNKNOWN, and writing 0 here
            # would put a number RAVEL invented into a paper's methods.
            progress["alloc_tres"] = facts.alloc_tres or "UNKNOWN"
            progress["metering"] = (
                "REPORTED"
                if facts.elapsed or facts.max_rss or facts.alloc_tres
                else "UNKNOWN"
            )
        if exit_code is not None:
            progress["exit_code"] = exit_code
        detail = f"{reading.detail} (sacct: {facts.state}"
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

    def _find_by_name(
        self,
        ssh: SSHTransport,
        job_name: str,
        *,
        target: SlurmTarget | None = None,
    ) -> str | None:
        """Look for a job this attempt already has, by its deterministic name.

        Two lookups, because one does not cover both halves of a job's life:
        `squeue` sees it while it is queued or running, and `sacct` sees it
        after it has ended and the queue has forgotten it. This is the check
        that catches a submission made by a process that died before writing
        `submission.json`.
        """
        timeout = (target or self.target).command_timeout_seconds
        for command in (
            f"squeue -h -n {_quote(job_name)} -o %A",
            f"sacct -n -P -X --name={_quote(job_name)} -o JobID",
        ):
            result = ssh.run(command, timeout=timeout)
            if not result.ok:
                continue
            for line in result.output.splitlines():
                candidate = line.strip().split("|")[0].strip()
                if candidate.isdigit():
                    return candidate
        return None

    def _upload(
        self,
        ssh: SSHTransport,
        workspace: Path,
        remote_dir: str,
        entrypoint: str,
        *,
        target: SlurmTarget | None = None,
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
        special = [
            path
            for path in everything
            if not path.is_file() and not path.is_dir()
        ]
        if special:
            raise SlurmSubmissionError(
                f"the prepared workspace {workspace} holds entries that are not "
                f"regular files or directories "
                f"({', '.join(str(path.relative_to(workspace)) for path in special)}); "
                "RAVEL uploads files it hashed, not devices or sockets"
            )
        if not files:
            raise SlurmSubmissionError(f"the prepared workspace {workspace} is empty")
        relative_entry = self._validated_entrypoint(workspace, entrypoint)
        if not (workspace / relative_entry).is_file():
            raise SlurmSubmissionError(
                f"the entrypoint {entrypoint!r} is not a regular file in the "
                f"prepared workspace {workspace}; there is nothing for the "
                "scheduler to run"
            )
        timeout = (target or self.target).command_timeout_seconds
        ssh.run(f"mkdir -p {_quote(remote_dir)}", timeout=timeout)
        for path in files:
            relative = path.relative_to(workspace).as_posix()
            _safe_relative(relative, "workspace file")
            ssh.put(path, f"{remote_dir}/{relative}")
        if relative_entry.endswith(JOB_SCRIPT_SUFFIX):
            ssh.run(
                f"chmod +x {_quote(f'{remote_dir}/{relative_entry}')}",
                timeout=timeout,
            )

    def _sbatch(
        self,
        ssh: SSHTransport,
        job_name: str,
        remote_dir: str,
        entrypoint: str,
        *,
        target: SlurmTarget | None = None,
    ) -> str:
        """Submit the job script and read back its id.

        `--parsable` makes sbatch print one machine-readable line, which is the
        only reason this can parse an id out of it at all; `--chdir` puts the
        job in its own directory, so the script's own `cd` and the scheduler
        agree about where it ran.

        In target mode the target's partition and account are passed as
        options, and a scheduler response that is not a job id is returned to
        the caller rather than raised: the caller knows whether the job can be
        found by name, and that is the difference between adopting a job and
        saying `UNKNOWN`.

        Raises:
            SlurmSubmissionError: The script is not in the workspace, or the
                scheduler refused it.
        """
        if not entrypoint:
            raise SlurmSubmissionError(
                "the contract named no entrypoint, so there is no job script to submit"
            )
        target = target or self.target
        timeout = target.command_timeout_seconds
        options = ""
        if target.partition:
            options += f" --partition={_quote(target.partition)}"
        if target.account:
            options += f" --account={_quote(target.account)}"
        script = f"{remote_dir}/{entrypoint}"
        result = ssh.run(
            f"test -f {_quote(script)} && sbatch --parsable --job-name={_quote(job_name)} "
            f"--chdir={_quote(remote_dir)}{options} {_quote(script)}",
            timeout=timeout,
        )
        if not result.ok:
            detail = f"the scheduler refused the job: {self.redact(result.describe())}"
            if target.target_id:
                raise _classify_sbatch_failure(result, detail)
            raise SlurmSubmissionError(detail)
        # `--parsable` answers `<job id>` or `<job id>;<cluster>`; the id is the
        # first field either way.
        job_id = result.output.split(";")[0].strip()
        if not job_id.isdigit():
            if target.target_id:
                return result.output
            raise SlurmSubmissionError(
                f"the scheduler answered {result.output!r} instead of a job id; "
                "RAVEL will not guess which job it started"
            )
        return job_id

    # ── Getting the results back ────────────────────────────────────────────

    def _remote_directory_for(
        self,
        ssh: SSHTransport,
        job_id: str,
        digest: str,
        *,
        target: SlurmTarget | None = None,
    ) -> str | None:
        """Find where a job ran, having only its id and the work's digest.

        The scheduler is asked first, because it recorded the answer when it
        started the job and it is the only party that knows. The fallback
        searches RAVEL's own jobs root for the submission record carrying this
        digest — a directory RAVEL made, bounded by RAVEL's layout, and the
        only option when accounting retention has purged a job that finished
        long ago.
        """
        target = target or self.target
        root = (target.remote_root or self.jobs_root).rstrip("/")
        timeout = target.command_timeout_seconds
        result = ssh.run(
            f"sacct -n -P -X -j {job_id} -o WorkDir%512",
            timeout=timeout,
        )
        if result.ok:
            for line in result.output.splitlines():
                candidate = line.strip().split("|")[0].strip()
                if not candidate.startswith("/"):
                    continue
                if target.target_id and not _is_safe_remote_path(candidate):
                    return None
                return candidate
        found = ssh.run(
            f"grep -rl --include={SUBMISSION_FILE} -m1 {_quote(digest)} "
            f"{_quote(root)} 2>/dev/null | head -1",
            timeout=timeout,
        )
        if found.ok and found.output:
            record = found.output.strip().splitlines()[0]
            if target.target_id and not _is_safe_remote_path(record):
                return None
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

    def _schedule_facts(
        self,
        ssh: SSHTransport,
        job_id: str,
        *,
        target: SlurmTarget | None = None,
    ) -> dict[str, object]:
        """The scheduler's account of how the job went, for the record.

        In target mode the accounting row is asked for `AllocTRES` as well.
        A value the scheduler did not report is written as `UNKNOWN`, never as
        zero and never as an absent key: a record that omits metering and a
        record that says "no allocation was reported" are different, and only
        the second is honest about what RAVEL knows.
        """
        target = target or self.target
        extended = bool(target.target_id)
        result = ssh.run(
            self._sacct_command(job_id, target=target),
            timeout=target.command_timeout_seconds,
        )
        if not result.ok:
            if extended:
                return {
                    "slurm_state": "UNKNOWN",
                    "target_id": target.target_id,
                    "accounting": "UNKNOWN",
                    "metering": "UNKNOWN",
                }
            return {}
        facts = _sacct_fields(result, extended=extended)
        if not facts.state:
            if extended:
                return {
                    "slurm_state": "UNKNOWN",
                    "target_id": target.target_id,
                    "accounting": "UNKNOWN",
                    "metering": "UNKNOWN",
                }
            return {}
        exit_code, note = read_exit_code(facts.allocation_exit, facts.batch_exit)
        recorded: dict[str, object] = {"slurm_state": facts.state}
        if extended:
            recorded["target_id"] = target.target_id
            recorded["accounting"] = "REPORTED"
            recorded["elapsed"] = facts.elapsed or "UNKNOWN"
            recorded["max_rss"] = facts.max_rss or "UNKNOWN"
            recorded["alloc_tres"] = facts.alloc_tres or "UNKNOWN"
            recorded["metering"] = (
                "REPORTED"
                if facts.elapsed or facts.max_rss or facts.alloc_tres
                else "UNKNOWN"
            )
        else:
            if facts.elapsed:
                recorded["elapsed"] = facts.elapsed
            if facts.max_rss:
                recorded["max_rss"] = facts.max_rss
        if exit_code is not None:
            recorded["exit_code"] = exit_code
        elif note:
            recorded["exit_code_note"] = note
        elif extended:
            recorded["exit_code"] = "UNKNOWN"
        return recorded

    # ── Command construction ────────────────────────────────────────────────

    def _squeue_command(self, job_id: str) -> str:
        """The job's state, or nothing when the queue no longer holds it."""
        return f"squeue -h -j {job_id} -o %T"

    def _sacct_command(
        self, job_id: str, *, target: SlurmTarget | None = None
    ) -> str:
        """The job's accounting row: state, exit codes, time, peak memory.

        `-P` is parsable, pipe-separated output and `%512` widens the state
        field, because Slurm truncates it to fourteen characters by default and
        `CANCELLED by 1000` is longer than that.

        Target mode asks for `AllocTRES` as a sixth field. The legacy command
        is left byte-for-byte as it was so an existing single-target
        deployment's scripted cluster keeps answering the same command.
        """
        fields = "JobID,State%512,ExitCode,Elapsed,MaxRSS"
        if target is not None and target.target_id:
            fields += ",AllocTRES%512"
        return f"sacct -n -P -j {job_id} -o {fields}"

    def _scancel_command(self, job_id: str) -> str:
        """Stop the job."""
        return f"scancel {job_id}"

    # ── Plumbing ────────────────────────────────────────────────────────────

    def _handle(
        self, job_id: str, digest: str, how: str, *, target_id: str = ""
    ) -> JobHandle:
        """The handle for a job, with the state the scheduler reports for it."""
        return JobHandle(
            backend_job_ref=self.make_ref(job_id, digest, target_id),
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


@dataclass(frozen=True, slots=True)
class _Accounting:
    """One job's accounting row, with absence preserved as absence.

    Every field defaults to the empty string rather than to a number. A
    scheduler that did not report `Elapsed` did not report zero seconds, and a
    value a paper might cite has to be able to say "not known".
    """

    state: str = ""
    allocation_exit: str = ""
    batch_exit: str = ""
    elapsed: str = ""
    max_rss: str = ""
    alloc_tres: str = ""


def _sacct_fields(result: CommandResult, *, extended: bool = False) -> _Accounting:
    """Read one job's accounting row: state, both exit codes, time, memory.

    `sacct` answers with a line per job *and per step*. The allocation's row is
    the one whose JobID has no dot — `<id>` rather than `<id>.batch` — and the
    batch step's exit code is the script's own, which is the one that says how
    the software ended.

    `extended` adds the sixth `AllocTRES` field. A row with fewer fields than
    the command asked for is not an error: an older accounting database may not
    report a TRES string, and the result records that it was not reported.
    """
    state = allocation_exit = batch_exit = elapsed = max_rss = alloc_tres = ""
    for line in result.output.splitlines():
        fields = line.split("|")
        if len(fields) < 5:
            continue
        job_id, job_state, exit_code, job_elapsed, job_rss = (
            field.strip() for field in fields[:5]
        )
        if not job_id:
            continue
        tres = fields[5].strip() if extended and len(fields) > 5 else ""
        if "." not in job_id:
            state = job_state
            allocation_exit = exit_code
            elapsed = job_elapsed
            max_rss = job_rss
            alloc_tres = tres
        elif job_id.endswith(".batch"):
            batch_exit = exit_code
    return _Accounting(
        state=state,
        allocation_exit=allocation_exit,
        batch_exit=batch_exit,
        elapsed=elapsed,
        max_rss=max_rss,
        alloc_tres=alloc_tres,
    )


def _safe_component(value: str, field_name: str) -> str:
    """One path component, so an identifier cannot spell a second directory.

    The remote directory is `root/project/node/attempt`. None of those values
    may be empty, `.` or `..`, and none may contain a separator: a project id
    of `../../etc` would otherwise put a run's files somewhere the operator did
    not name, and the scheduler would happily run them there.
    """
    if not value or value in (".", "..") or not _COMPONENT.fullmatch(value):
        raise SlurmSubmissionError(
            f"{field_name} {value!r} is not one path component; RAVEL builds the "
            "remote directory from identifiers it minted, and a value with a "
            "separator or a '..' segment would name a different directory"
        )
    return value


def _safe_relative(value: str, field_name: str) -> str:
    """A relative remote path with no escape and no ambiguity.

    Workspace files come from a local walk, so their relative names are already
    plausible. This is the check at the boundary where one becomes a remote
    path: an absolute path, a `..` segment, a backslash, an empty segment or a
    control character would each name something other than the file that was
    hashed.
    """
    if not value or value.startswith("/") or "\\" in value:
        raise SlurmSubmissionError(
            f"{field_name} {value!r} is not a relative POSIX path"
        )
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise SlurmSubmissionError(f"{field_name} {value!r} contains a control character")
    parts = value.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise SlurmSubmissionError(
            f"{field_name} {value!r} has an empty, '.' or '..' segment"
        )
    return value


def _is_safe_remote_path(value: str) -> bool:
    """Whether a remote path a scheduler reported is structurally safe to read."""
    if not value.startswith("/") or "\\" in value:
        return False
    if any(character in value for character in ("\x00", "\n", "\r")):
        return False
    parts = value.split("/")
    return ".." not in parts and "." not in parts


def _is_target_token(value: str) -> bool:
    """Whether a reference's second field is a target token rather than a job id."""
    return bool(_TARGET_TOKEN.fullmatch(value))


def _requested_software(request: JobRequest) -> str:
    """The software the contract names, if it names one.

    The contract's `execution_requirements` is `{"software": "<package>"}` and
    reaches a backend inside `task_spec`. A task spec that states `software`
    directly is accepted too, because a RunPlan written before the
    requirements map existed used that spelling. Anything else — a lab
    requirement, a non-string, an empty name — is not software and permits no
    catalog decision; the target is not asked to guess.
    """
    requirements = request.task_spec.get("execution_requirements")
    if isinstance(requirements, Mapping):
        software = requirements.get("software")
        if isinstance(software, str) and software.strip():
            return software.strip()
    direct = request.task_spec.get("software")
    if isinstance(direct, str) and direct.strip():
        return direct.strip()
    return ""


def _classify_sbatch_failure(
    result: CommandResult, detail: str
) -> SlurmSubmissionRejected:
    """Turn a scheduler's stated refusal into its named state.

    Only a message that names the partition or the account is classified as
    that. Everything else is a generic rejection, because guessing which term
    a scheduler meant is how a retryable problem gets recorded as a permanent
    one — and the reverse, which is worse: a partition that does not exist is
    not going to start existing because RAVEL tried again.
    """
    said = f"{result.stderr}\n{result.stdout}".lower()
    # Slurm's account refusal reads "Invalid account or account/partition
    # combination specified", which names both terms. The specific message is
    # checked before the generic mention so that refusal is not recorded as a
    # partition problem and retried against the wrong configuration.
    if "invalid account" in said:
        return SlurmAccountRejected(detail)
    if "invalid partition" in said:
        return SlurmPartitionRejected(detail)
    if "account" in said and "partition" not in said:
        return SlurmAccountRejected(detail)
    if "partition" in said and "account" not in said:
        return SlurmPartitionRejected(detail)
    if "account" in said and "partition" in said:
        # A combination message is the account refusal in Slurm's own wording.
        return SlurmAccountRejected(detail)
    return SlurmSubmissionRejected(detail)


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
