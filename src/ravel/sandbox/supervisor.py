"""The sandbox supervisor: the one process that owns Docker.

RAVEL's agent containers are given no Docker socket, no cluster credential, and
no cloud credential. They ask this service for a sandbox over HTTP, and the
service turns the request into Docker arguments. The split is enforced by where
the socket is mounted, not by an agent's promise: in the compose file only
`ravel-sandbox-supervisor` has `/var/run/docker.sock`, and the API/DSH/agent
services have none.

The supervisor's own rules are deliberately narrow:

- **One sandbox per (project, purpose).** A project's command sandbox is reused
  across runs; a browser sandbox is a second container because it needs the
  proxy network while a command sandbox must have no network at all. Both are
  keyed by the project id the caller proves, not by a handle the caller kept.
- **The project is checked on every operation.** Provision, execute, files,
  browser, status, cancel, collect, stop, reconnect, and destroy all carry an
  authority whose project must equal the handle's. A handle id from another
  project finds nothing.
- **Resource limits are the intersection of the request, the deployment
  ceiling, and the authority envelope.** A request wider than a ceiling is
  refused. The Docker flags are generated from the resolved limits, and the
  effectively enforced values are recorded on the handle so a test can compare
  what was asked with what the container has.
- **The container has no host paths, no socket, no capabilities, and no
  privilege.** It mounts one named volume (the project workspace) and
  optionally a per-project internal network whose only other peer is the
  egress proxy. It never gets `--privileged`, `--pid=host`, or `--network=host`.
- **Records outlive processes.** Handles and runs are JSON files under
  `state_dir`. Stop, destroy, and reconnect act on the records; a run record can
  be read after its container is gone. A supervisor restart marks in-flight
  runs `LOST` rather than pretending they finished.
- **Output is redacted on the way in.** The stored argv and captured output go
  through `SecretRedactor` before they are written, and again when served.

The HTTP API is intentionally small and non-RESTful where it helps: every
request names a project, and the token is a deployment capability rather than a
scientific authority. The authority object is checked against the handle, so a
token holder cannot reach another project's *handle*; provisioning a handle for
another project is a deployment-level capability and is recorded with the
binding that asked for it.
"""

from __future__ import annotations

import contextlib
import json
import mimetypes
import os
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from ravel.sandbox import addressing
from ravel.sandbox.authority import (
    SandboxAuthority,
    SandboxCeilings,
    resolve_limits,
)
from ravel.sandbox.docker_engine import DockerEngine, DockerError
from ravel.sandbox.models import (
    ArtifactRef,
    BrowserPage,
    FileEntry,
    RunRef,
    RunState,
    RunStatus,
    SandboxCapabilities,
    SandboxConflict,
    SandboxError,
    SandboxForbidden,
    SandboxHandle,
    SandboxIntegrityError,
    SandboxLimits,
    SandboxNotFound,
    SandboxPurpose,
    SandboxState,
    SandboxUnavailable,
    SandboxUnsafeTarget,
    SandboxValidationError,
    hash_bytes,
)
from ravel.sandbox.paths import validate_artifact_name, validate_relative_path
from ravel.sandbox.redaction import SecretRedactor

#: One executable, one volume, one proxy. Any of these changing under a
#: running sandbox would be a new sandbox, so the fingerprint is recorded on
#: the handle and a reconnect refuses a mismatch.
_PROJECT_HASH = re.compile(r"^[0-9a-f]{16}$")

#: The internal workspace path. Not configurable: the image's `fileops` and
#: the mount target must agree, and a deployment that changed one without the
#: other would produce a sandbox whose filesystem is silently empty.
WORKSPACE_MOUNT = "/workspace"

#: Where in the image the sandbox code lives.
SANDBOX_CODE_PATH = "/opt/ravel-sandbox"

#: The fixed shell wrapper that records the command's pid for cancellation.
#: `exec` keeps the pid the same process, so killing it kills the command.
#: The run id is generated here (hex), never taken from a request.
_PID_WRAPPER = 'echo $$ > "{pid_file}"; exec "$@"'

#: Default command timeout, from the T19 card.
DEFAULT_COMMAND_TIMEOUT_SECONDS = 120.0


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _csv(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _env_allowlist(value: str | None) -> dict[str, str]:
    """Parse `NAME=VALUE,NAME=VALUE` into the only environment a sandbox gets.

    Values are kept in the supervisor process and passed per exec with `-e`.
    A malformed pair is a startup error: a deployment that meant to allow
    `DATABASE_URL` and typoed the separator should not end up with an empty
    environment that every command silently lacks.
    """
    allowed: dict[str, str] = {}
    if not value:
        return allowed
    for pair in value.split(","):
        if not pair.strip():
            continue
        name, separator, secret = pair.partition("=")
        name = name.strip()
        if not separator or not name:
            raise ValueError(
                f"sandbox env allowlist entry {pair!r} is not NAME=VALUE; values are "
                "deployment configuration, not request input"
            )
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"sandbox env allowlist name {name!r} is not a shell name")
        if name in allowed:
            raise ValueError(f"sandbox env allowlist names {name} more than once")
        allowed[name] = secret
    return allowed


@dataclass(frozen=True, slots=True)
class SupervisorConfig:
    """Everything the supervisor reads from its environment."""

    image: str = "ravel-sandbox:latest"
    state_dir: Path = Path("/var/lib/ravel-sandbox")
    token: str = ""
    docker_binary: str = "docker"
    proxy_port: int = 3128
    proxy_allowlist: tuple[str, ...] = ()
    proxy_egress_networks: tuple[str, ...] = ("bridge",)
    env_allowlist: dict[str, str] = field(default_factory=dict)
    ceilings: SandboxCeilings = field(default_factory=SandboxCeilings)
    output_limit_bytes: int = 2 * 1024 * 1024
    max_file_bytes: int = 8 * 1024 * 1024
    max_artifact_bytes: int = 32 * 1024 * 1024
    max_artifacts: int = 32
    browser_startup_seconds: float = 30.0
    stop_grace_seconds: int = 5
    max_argv_bytes: int = 64 * 1024
    max_argv_arguments: int = 128

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> SupervisorConfig:
        env = os.environ if environ is None else environ
        get = env.get
        ceilings = SandboxCeilings(
            cpu_cores=float(get("RAVEL_SANDBOX_MAX_CPU_CORES", "2")),
            memory_bytes=int(get("RAVEL_SANDBOX_MAX_MEMORY_BYTES", str(4 * 1024**3))),
            pids=int(get("RAVEL_SANDBOX_MAX_PIDS", "256")),
            scratch_bytes=int(get("RAVEL_SANDBOX_MAX_SCRATCH_BYTES", str(10 * 1024**3))),
            timeout_seconds=float(
                get("RAVEL_SANDBOX_MAX_TIMEOUT_SECONDS", str(DEFAULT_COMMAND_TIMEOUT_SECONDS))
            ),
        )
        state_dir = Path(get("RAVEL_SANDBOX_STATE_DIR", "/var/lib/ravel-sandbox"))
        return cls(
            image=get("RAVEL_SANDBOX_IMAGE", "ravel-sandbox:latest"),
            state_dir=state_dir,
            token=get("RAVEL_SANDBOX_SUPERVISOR_TOKEN", ""),
            docker_binary=get("RAVEL_SANDBOX_DOCKER_BINARY", "docker"),
            proxy_port=int(get("RAVEL_SANDBOX_PROXY_PORT", "3128")),
            proxy_allowlist=_csv(get("RAVEL_SANDBOX_PROXY_ALLOWLIST")),
            proxy_egress_networks=_csv(
                get("RAVEL_SANDBOX_PROXY_EGRESS_NETWORKS", "bridge")
            ),
            env_allowlist=_env_allowlist(get("RAVEL_SANDBOX_ENV_ALLOWLIST")),
            ceilings=ceilings,
            output_limit_bytes=int(get("RAVEL_SANDBOX_OUTPUT_LIMIT_BYTES", str(2 * 1024**2))),
            max_file_bytes=int(get("RAVEL_SANDBOX_MAX_FILE_BYTES", str(8 * 1024**2))),
            max_artifact_bytes=int(
                get("RAVEL_SANDBOX_MAX_ARTIFACT_BYTES", str(32 * 1024**2))
            ),
            max_artifacts=int(get("RAVEL_SANDBOX_MAX_ARTIFACTS", "32")),
            browser_startup_seconds=float(
                get("RAVEL_SANDBOX_BROWSER_STARTUP_SECONDS", "30")
            ),
            stop_grace_seconds=int(get("RAVEL_SANDBOX_STOP_GRACE_SECONDS", "5")),
        )

    def validate(self) -> None:
        if not self.token or len(self.token) < 16:
            raise ValueError(
                "RAVEL_SANDBOX_SUPERVISOR_TOKEN must be set to at least 16 characters; "
                "the supervisor is a Docker-capable service and an unauthenticated "
                "one is a host escape"
            )
        if not self.image.strip():
            raise ValueError("RAVEL_SANDBOX_IMAGE is empty")
        if self.ceilings.timeout_seconds <= 0:
            raise ValueError("the sandbox command ceiling must be positive")
        if self.output_limit_bytes <= 0:
            raise ValueError("the output limit must be positive")


@dataclass
class HandleRecord:
    """The durable record behind a public `SandboxHandle`."""

    handle: SandboxHandle
    container: str
    volume: str
    limits: SandboxLimits
    binding_id: str
    network: str | None = None
    proxy: str | None = None
    proxy_url: str | None = None
    artifact_root: str = "artifacts"
    last_error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle": self.handle.as_dict(),
            "container": self.container,
            "volume": self.volume,
            "limits": self.limits.as_dict(),
            "binding_id": self.binding_id,
            "network": self.network,
            "proxy": self.proxy,
            "proxy_url": self.proxy_url,
            "artifact_root": self.artifact_root,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> HandleRecord:
        return cls(
            handle=SandboxHandle.from_dict(data["handle"]),
            container=str(data["container"]),
            volume=str(data["volume"]),
            limits=SandboxLimits.from_dict(data.get("limits") or {}),
            binding_id=str(data.get("binding_id", "")),
            network=data.get("network"),
            proxy=data.get("proxy"),
            proxy_url=data.get("proxy_url"),
            artifact_root=str(data.get("artifact_root", "artifacts")),
            last_error=str(data.get("last_error", "")),
        )


@dataclass
class RunRecord:
    """The durable record of one command or one browser fetch."""

    run: RunRef
    handle_id: str
    container: str
    command: tuple[str, ...]
    cwd: str
    env_refs: tuple[str, ...]
    command_digest: str
    started_at: datetime
    finished_at: datetime | None = None
    exit_code: int | None = None
    timed_out: bool = False
    cancelled: bool = False
    duration_seconds: float = 0.0
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    detail: str = ""
    artifacts: list[ArtifactRef] = field(default_factory=list)

    @property
    def state(self) -> RunState:
        return self.run.state

    def status(self) -> RunStatus:
        return RunStatus(
            run=self.run,
            state=self.run.state,
            exit_code=self.exit_code,
            timed_out=self.timed_out,
            cancelled=self.cancelled,
            duration_seconds=self.duration_seconds,
            command=self.command,
            cwd=self.cwd,
            env_refs=self.env_refs,
            stdout=self.stdout,
            stderr=self.stderr,
            stdout_truncated=self.stdout_truncated,
            stderr_truncated=self.stderr_truncated,
            finished_at=self.finished_at,
            detail=self.detail,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run.as_dict(),
            "handle_id": self.handle_id,
            "container": self.container,
            "command": list(self.command),
            "cwd": self.cwd,
            "env_refs": list(self.env_refs),
            "command_digest": self.command_digest,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "duration_seconds": self.duration_seconds,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "detail": self.detail,
            "artifacts": [ref.as_dict() for ref in self.artifacts],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRecord:
        return cls(
            run=RunRef.from_dict(data["run"]),
            handle_id=str(data["handle_id"]),
            container=str(data.get("container", "")),
            command=tuple(data.get("command") or ()),
            cwd=str(data.get("cwd", "")),
            env_refs=tuple(data.get("env_refs") or ()),
            command_digest=str(data.get("command_digest", "")),
            started_at=_parse_dt(data.get("started_at")),
            finished_at=_parse_dt(data["finished_at"]) if data.get("finished_at") else None,
            exit_code=data.get("exit_code"),
            timed_out=bool(data.get("timed_out", False)),
            cancelled=bool(data.get("cancelled", False)),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            stdout_truncated=bool(data.get("stdout_truncated", False)),
            stderr_truncated=bool(data.get("stderr_truncated", False)),
            detail=str(data.get("detail", "")),
            artifacts=[ArtifactRef.from_dict(item) for item in data.get("artifacts") or []],
        )


@dataclass
class _ActiveRun:
    """The in-process half of a run, used for cancellation and timeout."""

    record: RunRecord
    pid_file: str
    stdout_path: Path
    stderr_path: Path
    process: Any = None
    cancel_requested: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


class _BrowserFailure(RuntimeError):
    """The in-container browser helper reported a typed refusal."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


class _ApiError(Exception):
    """A refusal with an HTTP status and a stable code for the client."""

    def __init__(self, status: int, code: str, message: str) -> None:
        self.status = status
        self.code = code
        self.message = message
        super().__init__(message)


def _parse_dt(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return _utcnow()


def sanitize_project(project_id: str) -> str:
    """A filesystem- and Docker-name-safe digest of one project id.

    The digest is what names the volume, the container, and the network. A
    project id is data from the database and may contain any character; using
    it directly would make a project id a path component and a Docker name.
    """
    if not isinstance(project_id, str) or not project_id.strip():
        raise SandboxValidationError("a project id is required")
    digest = hash_bytes(project_id.encode("utf-8")).split(":", 1)[1][:16]
    if not _PROJECT_HASH.match(digest):
        raise SandboxValidationError("could not derive a sandbox name from the project")
    return digest


class SandboxSupervisor:
    """The state machine and Docker orchestration behind the HTTP API."""

    def __init__(
        self,
        config: SupervisorConfig,
        *,
        engine: DockerEngine | None = None,
    ) -> None:
        config.validate()
        self.config = config
        self.state_dir = Path(config.state_dir)
        self.handles_dir = self.state_dir / "handles"
        self.runs_dir = self.state_dir / "runs"
        self.artifacts_dir = self.state_dir / "artifacts"
        for directory in (self.state_dir, self.handles_dir, self.runs_dir, self.artifacts_dir):
            directory.mkdir(parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(directory, 0o700)
        secrets_in_use = [config.token, *config.env_allowlist.values()]
        self.redactor = SecretRedactor(secrets_in_use)
        self.engine = engine or DockerEngine(
            config.docker_binary, redactor=self.redactor
        )
        self._handles: dict[str, HandleRecord] = {}
        self._runs: dict[str, RunRecord] = {}
        self._active: dict[str, _ActiveRun] = {}
        self._lock = threading.RLock()
        self._key_locks: dict[tuple[str, str], threading.Lock] = {}
        self._load()

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        for path in self.handles_dir.glob("*.json"):
            try:
                record = HandleRecord.from_dict(json.loads(path.read_text("utf-8")))
            except (OSError, ValueError, KeyError):
                continue
            self._handles[record.handle.handle_id] = record
        for path in self.runs_dir.glob("*.json"):
            try:
                record = RunRecord.from_dict(json.loads(path.read_text("utf-8")))
            except (OSError, ValueError, KeyError):
                continue
            if record.state is RunState.RUNNING:
                record.run = RunRef(
                    run_id=record.run.run_id,
                    handle_id=record.handle_id,
                    project_id=record.run.project_id,
                    action_id=record.run.action_id,
                    state=RunState.LOST,
                    created_at=record.run.created_at,
                )
                record.finished_at = _utcnow()
                record.detail = (
                    "the supervisor restarted while this run was active; the command's "
                    "outcome is unknown and the container may still hold a process"
                )
                self._write_run(record)
            self._runs[record.run.run_id] = record
        # Reconcile handles whose container vanished while the supervisor was
        # down. A stopped sandbox is still reconnectable; a missing container
        # without a volume is a failed handle rather than a silent fresh start.
        for record in list(self._handles.values()):
            if record.handle.state in (
                SandboxState.PROVISIONING,
                SandboxState.READY,
                SandboxState.STOPPING,
            ):
                try:
                    running = self.engine.container_running(record.container)
                    present = self.engine.container_present(record.container)
                except DockerError:
                    running, present = False, True
                if running:
                    self._set_state(record, SandboxState.READY)
                elif present or self.engine.volume_present(record.volume):
                    self._set_state(record, SandboxState.STOPPED)
                else:
                    self._set_state(record, SandboxState.FAILED, "container and volume are gone")

    def _write_handle(self, record: HandleRecord) -> None:
        self._atomic_write(self.handles_dir / f"{record.handle.handle_id}.json", record.as_dict())

    def _write_run(self, record: RunRecord) -> None:
        self._atomic_write(self.runs_dir / f"{record.run.run_id}.json", record.as_dict())

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(temporary)
            raise

    def _set_state(
        self, record: HandleRecord, state: SandboxState, detail: str = ""
    ) -> SandboxHandle:
        record.handle = SandboxHandle(
            handle_id=record.handle.handle_id,
            provider=record.handle.provider,
            project_id=record.handle.project_id,
            epoch=record.handle.epoch,
            capabilities=record.handle.capabilities,
            state=state,
            purpose=record.handle.purpose,
            created_at=record.handle.created_at,
            updated_at=_utcnow(),
        )
        if detail:
            record.last_error = detail
        self._write_handle(record)
        return record.handle

    # ── lookups and authority ─────────────────────────────────────────────

    def _lock_for(self, project_id: str, purpose: SandboxPurpose) -> threading.Lock:
        with self._lock:
            return self._key_locks.setdefault((project_id, purpose.value), threading.Lock())

    def _handle(self, handle_id: str, *, project_id: str) -> HandleRecord:
        with self._lock:
            record = self._handles.get(handle_id)
        if record is None or record.handle.project_id != project_id:
            # The same answer for "absent" and "another project's": a caller
            # must not learn that a handle id exists elsewhere.
            raise SandboxNotFound(f"no sandbox handle {handle_id!r} in project {project_id!r}")
        return record

    def _run(self, run_id: str, *, project_id: str) -> RunRecord:
        with self._lock:
            record = self._runs.get(run_id)
        if record is None or record.run.project_id != project_id:
            raise SandboxNotFound(f"no sandbox run {run_id!r} in project {project_id!r}")
        return record

    @staticmethod
    def _authority(data: dict[str, Any] | SandboxAuthority) -> SandboxAuthority:
        try:
            authority = (
                data
                if isinstance(data, SandboxAuthority)
                else SandboxAuthority.from_dict(data)
            )
        except SandboxValidationError:
            raise
        except Exception as exc:
            raise SandboxValidationError(f"the sandbox authority is malformed: {exc}") from exc
        return authority

    def _authorize_handle(
        self,
        record: HandleRecord,
        project_id: str,
        authority_data: dict[str, Any] | SandboxAuthority,
    ) -> SandboxAuthority:
        authority = self._authority(authority_data)
        try:
            authority.require_project(project_id)
        except SandboxValidationError as exc:
            raise SandboxForbidden(str(exc)) from exc
        if record.handle.project_id != project_id:
            raise SandboxNotFound(
                f"no sandbox handle {record.handle.handle_id!r} in project {project_id!r}"
            )
        authority.check_epoch(record.handle.epoch)
        return authority

    # ── provisioning ──────────────────────────────────────────────────────

    def provision(
        self,
        *,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
        limits: dict[str, Any] | SandboxLimits | None = None,
        purpose: SandboxPurpose | str = SandboxPurpose.COMMAND,
    ) -> SandboxHandle:
        parsed_authority = self._authority(authority)
        try:
            parsed_authority.require_project(project_id)
        except SandboxValidationError as exc:
            raise SandboxForbidden(str(exc)) from exc
        chosen_purpose = SandboxPurpose(purpose)
        if chosen_purpose is SandboxPurpose.BROWSER and parsed_authority.role != "RESEARCH":
            raise SandboxForbidden(
                "only the Research role may hold a browser sandbox; command execution "
                "is the Worker roles' authority"
            )
        requested = (
            limits
            if isinstance(limits, SandboxLimits)
            else SandboxLimits.from_dict(limits) if limits else None
        )
        resolved = resolve_limits(requested, self.config.ceilings, parsed_authority)
        if not self.engine.image_present(self.config.image):
            raise SandboxUnavailable(
                f"sandbox image {self.config.image!r} is not present in the Docker daemon; "
                "build it from infra/sandbox/Dockerfile.sandbox or configure "
                "RAVEL_SANDBOX_IMAGE to an image the daemon can run"
            )
        if chosen_purpose is SandboxPurpose.BROWSER and not self.config.proxy_allowlist:
            raise SandboxUnavailable(
                "the browser sandbox needs an operator-approved proxy allowlist; "
                "RAVEL_SANDBOX_PROXY_ALLOWLIST is empty, so no target is permitted"
            )

        with self._lock_for(project_id, chosen_purpose):
            existing = self._find_existing(project_id, chosen_purpose)
            if existing is not None and existing.handle.state in (
                SandboxState.READY,
                SandboxState.STOPPED,
            ):
                if (
                    existing.handle.state is SandboxState.STOPPED
                    or not self.engine.container_running(existing.container)
                ):
                    self._start_record(existing)
                return self._set_state(existing, SandboxState.READY)
            if existing is not None and existing.handle.state is SandboxState.STOPPING:
                raise SandboxConflict(
                    "the project's sandbox is stopping; reconnect it after the stop "
                    "completes or destroy it before provisioning another"
                )
            if existing is not None and existing.handle.state in (
                SandboxState.FAILED,
                SandboxState.PROVISIONING,
            ):
                # A failed or half-created record is swept aside. The named
                # volume is preserved: it may be the only copy of a workspace.
                self._remove_container_quietly(existing)
                self._set_state(existing, SandboxState.DESTROYED, "replaced after failure")
            return self._create(project_id, parsed_authority, resolved, chosen_purpose)

    def _remove_container_quietly(self, record: HandleRecord) -> None:
        """Best-effort removal after a failed create or a replaced record.

        A provisioning failure may have left a container, a proxy, or neither;
        the caller is already handling the original error and a cleanup error
        must not replace it. The workspace volume is never removed here: it may
        hold the only copy of the project's files.
        """
        for name in (record.proxy, record.container):
            if not name:
                continue
            with contextlib.suppress(DockerError):
                self.engine.stop(name, timeout=1)
            with contextlib.suppress(DockerError):
                self.engine.remove(name)

    def _find_existing(
        self, project_id: str, purpose: SandboxPurpose
    ) -> HandleRecord | None:
        candidates = [
            record
            for record in self._handles.values()
            if record.handle.project_id == project_id
            and record.handle.purpose is purpose
            and record.handle.state not in (SandboxState.DESTROYED,)
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda record: record.handle.created_at)

    def _create(
        self,
        project_id: str,
        authority: SandboxAuthority,
        limits: SandboxLimits,
        purpose: SandboxPurpose,
    ) -> SandboxHandle:
        digest = sanitize_project(project_id)
        handle_id = f"sbx_{uuid.uuid4().hex}"
        purpose_slug = "browser" if purpose is SandboxPurpose.BROWSER else "cmd"
        container = f"ravel-t19-{purpose_slug}-{digest}"
        volume = f"ravel-t19-ws-{digest}"
        network: str | None = None
        proxy: str | None = None
        proxy_url: str | None = None
        capabilities = SandboxCapabilities()
        handle = SandboxHandle(
            handle_id=handle_id,
            provider="docker",
            project_id=project_id,
            epoch=authority.lease_epoch,
            capabilities=capabilities,
            state=SandboxState.PROVISIONING,
            purpose=purpose,
            created_at=_utcnow(),
            updated_at=_utcnow(),
        )
        record = HandleRecord(
            handle=handle,
            container=container,
            volume=volume,
            limits=limits,
            binding_id=authority.binding_id,
        )
        try:
            self.engine.volume_create(volume)
            self.engine.prepare_volume(self.config.image, volume)
            if purpose is SandboxPurpose.BROWSER:
                network = f"ravel-t19-net-{digest}"
                proxy = f"ravel-t19-proxy-{digest}"
                if not self.engine.network_present(network):
                    self.engine.network_create(network, internal=True)
                proxy_url = f"http://{proxy}:{self.config.proxy_port}"
                self._start_proxy(proxy, network)
                self._wait_for_proxy(proxy)
                self._attach_proxy_egress(proxy)
            self._create_container(record, purpose, network)
            self.engine.start(container)
            self._wait_for_container(container)
        except Exception as exc:
            detail = self.redactor.redact(str(exc))
            self._remove_container_quietly(record)
            if proxy:
                self.engine.stop(proxy, timeout=self.config.stop_grace_seconds)
                self.engine.remove(proxy)
            if network:
                self.engine.network_remove(network)
            record.handle = SandboxHandle(
                handle_id=handle_id,
                provider="docker",
                project_id=project_id,
                epoch=authority.lease_epoch,
                capabilities=capabilities,
                state=SandboxState.FAILED,
                purpose=purpose,
                created_at=handle.created_at,
                updated_at=_utcnow(),
            )
            record.last_error = detail
            with self._lock:
                self._handles[handle_id] = record
            self._write_handle(record)
            if isinstance(exc, SandboxError):
                raise
            raise SandboxUnavailable(f"could not provision the sandbox: {detail}") from exc

        record.network = network
        record.proxy = proxy
        record.proxy_url = proxy_url
        with self._lock:
            self._handles[handle_id] = record
        self._set_state(record, SandboxState.READY)
        return record.handle

    def _create_container(
        self, record: HandleRecord, purpose: SandboxPurpose, network: str | None
    ) -> None:
        limits = record.limits
        args = [
            "--name",
            record.container,
            "--label",
            "ravel.sandbox=1",
            "--label",
            f"ravel.sandbox.project={sanitize_project(record.handle.project_id)}",
            "--label",
            f"ravel.sandbox.purpose={purpose.value}",
            "--label",
            f"ravel.sandbox.handle={record.handle.handle_id}",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            # No `seccomp=` argument: Docker's built-in default profile is
            # applied to every container that does not explicitly disable it.
            # Passing the literal `default` is a profile-file path to the
            # daemon, not a keyword, and fails on this engine. The tests read
            # Seccomp mode 2 back from inside the running container, which is
            # stronger evidence than repeating a flag string.
            "--cpus",
            f"{limits.cpu_cores:g}",
            "--memory",
            str(limits.memory_bytes),
            "--memory-swap",
            str(limits.memory_bytes),
            "--pids-limit",
            str(limits.pids),
            "--tmpfs",
            f"/tmp:rw,noexec,nosuid,nodev,size={limits.scratch_bytes},mode=1777",
            "--mount",
            (
                f"type=volume,source={record.volume},target={WORKSPACE_MOUNT}"
                + (",readonly" if purpose is SandboxPurpose.BROWSER else "")
            ),
            "--env",
            "HOME=/tmp",
            "--env",
            "TMPDIR=/tmp",
            "--env",
            f"RAVEL_SANDBOX_WORKSPACE={WORKSPACE_MOUNT}",
            "--network",
            "none" if network is None else network,
        ]
        if purpose is SandboxPurpose.BROWSER:
            args.extend(["--shm-size", "512m"])
        args.extend(
            [
                "--entrypoint",
                "/bin/sh",
                self.config.image,
                "-c",
                "trap : TERM INT; while :; do sleep 3600; done",
            ]
        )
        self.engine.create(args)

    def _start_proxy(self, proxy: str, network: str) -> None:
        if self.engine.container_present(proxy):
            if not self.engine.container_running(proxy):
                self.engine.start(proxy)
            return
        client_cidr = self.engine.network_subnet(network)
        if not client_cidr:
            raise SandboxUnavailable(
                f"could not read the subnet Docker assigned to {network!r}; the proxy "
                "cannot check that its clients are on the sandbox's internal network"
            )
        allowlist = ",".join(self.config.proxy_allowlist)
        args = [
            "--name",
            proxy,
            "--label",
            "ravel.sandbox=1",
            "--label",
            "ravel.sandbox.proxy=1",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            # No `seccomp=` argument: Docker's built-in default profile is
            # applied to every container that does not explicitly disable it.
            # Passing the literal `default` is a profile-file path to the
            # daemon, not a keyword, and fails on this engine. The tests read
            # Seccomp mode 2 back from inside the running container, which is
            # stronger evidence than repeating a flag string.
            "--cpus",
            "0.5",
            "--memory",
            str(256 * 1024 * 1024),
            "--pids-limit",
            "64",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--network",
            network,
            "--env",
            f"RAVEL_PROXY_ALLOWLIST={allowlist}",
            "--env",
            f"RAVEL_PROXY_CLIENT_CIDR={client_cidr}",
            "--env",
            f"RAVEL_PROXY_PORT={self.config.proxy_port}",
            "--env",
            "PYTHONPATH=" + SANDBOX_CODE_PATH,
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "python3",
            self.config.image,
            "-m",
            "ravel.sandbox.proxy",
        ]
        self.engine.create(args)
        self.engine.start(proxy)

    def _attach_proxy_egress(self, proxy: str) -> None:
        for network in self.config.proxy_egress_networks:
            if not network:
                continue
            if not self.engine.network_present(network):
                raise SandboxUnavailable(
                    f"the proxy egress network {network!r} does not exist; the "
                    "supervisor will not start a browser sandbox whose proxy has no "
                    "route to the public internet"
                )
            self.engine.network_connect(network, proxy)

    def _wait_for_proxy(self, proxy: str) -> None:
        deadline = time.monotonic() + self.config.browser_startup_seconds
        command = [
            "python3",
            "-c",
            (
                "import socket,sys; s=socket.socket(); s.settimeout(1); "
                f"s.connect(('127.0.0.1',{self.config.proxy_port})); s.close()"
            ),
        ]
        last = ""
        while time.monotonic() < deadline:
            result = self.engine.exec_capture(
                proxy,
                command,
                timeout=5,
                env={"PYTHONDONTWRITEBYTECODE": "1"},
            )
            if result.returncode == 0:
                return
            last = self.redactor.redact(result.stderr[-500:])
            time.sleep(0.2)
        raise SandboxUnavailable(f"the egress proxy did not start listening: {last}")

    def _wait_for_container(self, container: str) -> None:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if self.engine.container_running(container):
                return
            time.sleep(0.1)
        raise SandboxUnavailable(f"sandbox container {container!r} did not enter running state")

    def _start_record(self, record: HandleRecord) -> None:
        if record.handle.purpose is SandboxPurpose.BROWSER and record.proxy:
            if not self.engine.container_running(record.proxy):
                self.engine.start(record.proxy)
            self._wait_for_proxy(record.proxy)
        if not self.engine.container_running(record.container):
            self.engine.start(record.container)
            self._wait_for_container(record.container)
        self._set_state(record, SandboxState.READY)

    # ── command execution ─────────────────────────────────────────────────

    def execute(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
        argv: list[str],
        cwd: str = "",
        env_refs: list[str] | None = None,
        timeout_seconds: float | None = None,
        action_id: str = "",
        command_digest: str = "",
    ) -> RunRef:
        record = self._handle(handle_id, project_id=project_id)
        parsed_authority = self._authorize_handle(record, project_id, authority)
        if record.handle.state is not SandboxState.READY:
            raise SandboxConflict(
                f"the sandbox {handle_id!r} is {record.handle.state.value}, not READY"
            )
        if record.handle.purpose is SandboxPurpose.BROWSER and parsed_authority.role != "RESEARCH":
            raise SandboxForbidden("only Research may execute in a browser sandbox")

        command = self._validate_argv(argv)
        working = validate_relative_path(cwd, allow_empty=True)
        refs = self._validate_env_refs(env_refs or [])
        bound_timeout = self._validate_timeout(timeout_seconds, record.limits)
        action = self._validate_action_id(action_id)
        digest = self._validate_command_digest(
            command_digest,
            command,
            working,
            refs,
        )
        run_id = f"run_{uuid.uuid4().hex}"
        run = RunRef(
            run_id=run_id,
            handle_id=handle_id,
            project_id=project_id,
            action_id=action,
            state=RunState.RUNNING,
        )
        record_dir = self.runs_dir / run_id
        record_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(record_dir, 0o700)
        pid_file = f"/tmp/ravel-run-{run_id}.pid"
        stdout_path = record_dir / "stdout.raw"
        stderr_path = record_dir / "stderr.raw"
        active = _ActiveRun(
            record=RunRecord(
                run=run,
                handle_id=handle_id,
                container=record.container,
                command=self.redactor.redact_argv(command),
                cwd=working,
                env_refs=tuple(refs),
                command_digest=digest,
                started_at=_utcnow(),
            ),
            pid_file=pid_file,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
        )
        with self._lock:
            self._runs[run_id] = active.record
            self._active[run_id] = active
        self._write_run(active.record)
        try:
            with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                process = self.engine.popen(
                    self._exec_argv(record, command, working, refs, pid_file),
                    stdout=stdout,
                    stderr=stderr,
                )
                active.process = process
                exit_code, timed_out, cancelled = self._wait_for_process(
                    active, process, bound_timeout
                )
            self._finalize_run(active, exit_code, timed_out, cancelled, bound_timeout)
        except Exception as exc:
            self._kill_run(active)
            with active.lock:
                active.record.run = RunRef(
                    run_id=run_id,
                    handle_id=handle_id,
                    project_id=project_id,
                    action_id=action,
                    state=RunState.FAILED,
                    created_at=run.created_at,
                )
                active.record.finished_at = _utcnow()
                active.record.detail = self.redactor.redact(
                    f"the supervisor could not run the command: {type(exc).__name__}: {exc}"
                )
                self._write_run(active.record)
            if isinstance(exc, SandboxError):
                raise
            raise SandboxUnavailable(active.record.detail) from exc
        finally:
            with self._lock:
                self._active.pop(run_id, None)
            for path in (stdout_path, stderr_path):
                with contextlib.suppress(OSError):
                    path.unlink()
        return active.record.run

    def _exec_argv(
        self,
        record: HandleRecord,
        command: tuple[str, ...],
        cwd: str,
        refs: tuple[str, ...],
        pid_file: str,
    ) -> list[str]:
        args = ["exec", "--interactive"]
        env = {
            "HOME": "/tmp",
            "TMPDIR": "/tmp",
            "PYTHONDONTWRITEBYTECODE": "1",
            "RAVEL_SANDBOX_WORKSPACE": WORKSPACE_MOUNT,
        }
        for name in refs:
            env[name] = self.config.env_allowlist[name]
        for name, value in env.items():
            args.extend(["--env", f"{name}={value}"])
        workdir = WORKSPACE_MOUNT if not cwd else f"{WORKSPACE_MOUNT}/{cwd}"
        args.extend(["--workdir", workdir, record.container])
        wrapper = _PID_WRAPPER.format(pid_file=pid_file)
        args.extend(["/bin/sh", "-c", wrapper, "sh", *command])
        return args

    def _wait_for_process(
        self, active: _ActiveRun, process: Any, timeout: float
    ) -> tuple[int | None, bool, bool]:
        deadline = time.monotonic() + timeout
        while True:
            with active.lock:
                if active.cancel_requested:
                    self._kill_run(active)
                    try:
                        process.wait(timeout=5)
                    except Exception:
                        process.kill()
                    return process.returncode, False, True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._kill_run(active)
                try:
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
                return process.returncode, True, False
            try:
                exit_code = process.wait(timeout=min(1.0, remaining))
            except Exception:
                continue
            # The wait may have returned because a concurrent `stop` killed the
            # command rather than because the command finished. Re-read the
            # flag after wait, or the cancellation becomes a generic exit code
            # (143) and the run is recorded as FAILED.
            with active.lock:
                if active.cancel_requested:
                    self._kill_run(active)
                    return exit_code, False, True
            return exit_code, False, False

    def _kill_run(self, active: _ActiveRun) -> None:
        """Kill the command by its recorded pid, then the Docker CLI.

        Killing the CLI alone detaches from a process that may still be running
        in the container; killing the pid alone leaves the CLI waiting. Both
        are attempted, and the container is disposable if either fails.
        """
        script = (
            f'if [ -f "{active.pid_file}" ]; then pid=$(cat "{active.pid_file}"); '
            'kill -TERM -"$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null; '
            'sleep 0.2; '
            'kill -KILL -"$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null; fi'
        )
        with contextlib.suppress(DockerError):
            self.engine.exec_capture(
                active.record.container, ["/bin/sh", "-c", script], timeout=10
            )

    def _finalize_run(
        self,
        active: _ActiveRun,
        exit_code: int | None,
        timed_out: bool,
        cancelled: bool,
        timeout_bound: float,
    ) -> None:
        record = active.record
        now = _utcnow()
        duration = max(0.0, (now - record.started_at).total_seconds())
        stdout, out_truncated = self._read_output(active.stdout_path)
        stderr, err_truncated = self._read_output(active.stderr_path)
        with active.lock:
            # A `stop` that set the flag while `_wait_for_process` was already
            # returning still owns the outcome. Deciding inside the lock makes
            # cancellation deterministic rather than a race with exit code 143.
            cancelled = cancelled or active.cancel_requested
            if cancelled:
                state = RunState.CANCELLED
                detail = "the run was cancelled by its project"
            elif timed_out:
                state = RunState.TIMED_OUT
                detail = f"the run exceeded its {timeout_bound:g}s time limit"
            elif exit_code == 0:
                state = RunState.SUCCEEDED
                detail = "completed with exit code 0"
            else:
                state = RunState.FAILED
                detail = f"the command exited with code {exit_code}"
            record.run = RunRef(
                run_id=record.run.run_id,
                handle_id=record.handle_id,
                project_id=record.run.project_id,
                action_id=record.run.action_id,
                state=state,
                created_at=record.run.created_at,
            )
            record.finished_at = now
            record.exit_code = exit_code
            record.timed_out = timed_out
            record.cancelled = cancelled
            record.duration_seconds = duration
            record.stdout = self.redactor.redact(stdout)
            record.stderr = self.redactor.redact(stderr)
            record.stdout_truncated = out_truncated
            record.stderr_truncated = err_truncated
            record.detail = detail
            self._write_run(record)

    def _read_output(self, path: Path) -> tuple[str, bool]:
        limit = self.config.output_limit_bytes
        try:
            with path.open("rb") as handle:
                data = handle.read(limit + 1)
        except OSError:
            return "", False
        truncated = len(data) > limit
        return data[:limit].decode("utf-8", errors="replace"), truncated

    def status(self, *, run_id: str, project_id: str) -> RunStatus:
        return self._run(run_id, project_id=project_id).status()

    def cancel(
        self,
        *,
        run_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
    ) -> RunStatus:
        record = self._run(run_id, project_id=project_id)
        handle = self._handle(record.handle_id, project_id=project_id)
        self._authorize_handle(handle, project_id, authority)
        active = self._active.get(run_id)
        if active is not None:
            with active.lock:
                active.cancel_requested = True
            self._kill_run(active)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and run_id in self._active:
                time.sleep(0.05)
        else:
            if record.state is RunState.RUNNING:
                with self._lock:
                    record.run = RunRef(
                        run_id=run_id,
                        handle_id=record.handle_id,
                        project_id=project_id,
                        action_id=record.run.action_id,
                        state=RunState.CANCELLED,
                        created_at=record.run.created_at,
                    )
                    record.finished_at = _utcnow()
                    record.cancelled = True
                    record.detail = "cancelled after the supervisor no longer held the process"
                    self._write_run(record)
        return self._run(run_id, project_id=project_id).status()

    def collect(self, *, run_id: str, project_id: str) -> list[ArtifactRef]:
        record = self._run(run_id, project_id=project_id)
        if record.state is RunState.RUNNING:
            raise SandboxConflict("artifacts cannot be collected while the run is active")
        handle = self._handle(record.handle_id, project_id=project_id)
        result = self._fileops(
            handle,
            [
                "collect",
                handle.artifact_root,
                str(self.config.max_artifacts),
                str(self.config.max_artifact_bytes),
                str(self.config.max_file_bytes),
            ],
            timeout=60,
        )
        entries = result.get("entries") or []
        refs: list[ArtifactRef] = []
        artifact_dir = self.artifacts_dir / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            relative = str(entry["path"])
            read = self._fileops(
                handle,
                ["read", relative, str(self.config.max_file_bytes)],
                timeout=60,
            )
            import base64

            data = base64.b64decode(read["data_b64"].encode("ascii"), validate=True)
            digest = hash_bytes(data)
            if digest != read["content_hash"] or digest != entry["content_hash"]:
                raise SandboxIntegrityError(
                    f"artifact {relative!r} changed between the manifest and the read"
                )
            name = validate_artifact_name(str(entry["name"]))
            stored = artifact_dir / digest.split(":", 1)[1]
            if not stored.exists():
                descriptor = os.open(
                    stored, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
                )
                with os.fdopen(descriptor, "wb") as output:
                    output.write(data)
            refs.append(
                ArtifactRef(
                    name=name,
                    relative_path=relative,
                    content_hash=digest,
                    size_bytes=len(data),
                    media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
                )
            )
        record.artifacts = refs
        self._write_run(record)
        return refs

    def artifact_bytes(self, *, run_id: str, name: str, project_id: str) -> tuple[bytes, str]:
        record = self._run(run_id, project_id=project_id)
        wanted = validate_artifact_name(name)
        for ref in record.artifacts:
            if ref.name == wanted:
                path = self.artifacts_dir / run_id / ref.content_hash.split(":", 1)[1]
                data = path.read_bytes()
                digest = hash_bytes(data)
                if digest != ref.content_hash:
                    raise SandboxIntegrityError(
                        f"stored artifact {name!r} no longer matches its hash"
                    )
                return data, digest
        raise SandboxNotFound(f"run {run_id!r} has no collected artifact named {name!r}")

    # ── files ─────────────────────────────────────────────────────────────

    def files_list(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
        path: str,
    ) -> list[FileEntry]:
        record = self._handle(handle_id, project_id=project_id)
        self._authorize_handle(record, project_id, authority)
        relative = validate_relative_path(path, allow_empty=True)
        result = self._fileops(record, ["list", relative])
        return [
            FileEntry(
                name=str(item["name"]),
                path=str(item["path"]),
                kind=str(item["kind"]),
                size_bytes=int(item.get("size_bytes", 0)),
            )
            for item in result.get("entries") or []
        ]

    def files_read(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
        path: str,
        max_bytes: int | None = None,
    ) -> tuple[bytes, str]:
        record = self._handle(handle_id, project_id=project_id)
        self._authorize_handle(record, project_id, authority)
        relative = validate_relative_path(path, allow_empty=False)
        limit = max_bytes or self.config.max_file_bytes
        if limit <= 0 or limit > self.config.max_file_bytes:
            raise SandboxValidationError(
                f"max_bytes must be in 1..{self.config.max_file_bytes}"
            )
        result = self._fileops(record, ["read", relative, str(limit)], timeout=60)
        import base64

        data = base64.b64decode(result["data_b64"].encode("ascii"), validate=True)
        digest = hash_bytes(data)
        if digest != result["content_hash"]:
            raise SandboxIntegrityError("the bytes read from the sandbox do not match their hash")
        return data, digest

    def files_write(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
        path: str,
        data: bytes,
    ) -> tuple[str, int]:
        record = self._handle(handle_id, project_id=project_id)
        self._authorize_handle(record, project_id, authority)
        relative = validate_relative_path(path, allow_empty=False)
        if len(data) > self.config.max_file_bytes:
            raise SandboxValidationError(
                f"a sandbox file write is limited to {self.config.max_file_bytes} bytes"
            )
        import base64

        digest = hash_bytes(data)
        envelope = json.dumps(
            {
                "data_b64": base64.b64encode(data).decode("ascii"),
                "expected_hash": digest,
            }
        ).encode("utf-8")
        result = self._fileops(
            record,
            ["write", relative, str(self.config.max_file_bytes)],
            stdin=envelope,
            timeout=60,
        )
        if result.get("content_hash") != digest:
            raise SandboxIntegrityError(
                "the file content hash the sandbox reports differs from the bytes sent"
            )
        return digest, int(result.get("size_bytes", len(data)))

    def _fileops(
        self,
        record: HandleRecord,
        arguments: list[str],
        *,
        timeout: float = 30,
        stdin: bytes | None = None,
    ) -> dict[str, Any]:
        result = self.engine.exec_capture(
            record.container,
            ["python3", "-m", "ravel.sandbox.fileops", *arguments],
            timeout=timeout,
            env={
                "PYTHONPATH": SANDBOX_CODE_PATH,
                "PYTHONDONTWRITEBYTECODE": "1",
                "HOME": "/tmp",
            },
            input_bytes=stdin,
        )
        try:
            payload = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError) as exc:
            raise SandboxError(
                self.redactor.redact(
                    f"the sandbox file helper returned no JSON: {result.stderr[-500:]}"
                )
            ) from exc
        if not isinstance(payload, dict) or not payload.get("ok"):
            code = (
                payload.get("error_code", "fileop_failed")
                if isinstance(payload, dict)
                else ""
            )
            message = (
                payload.get("error", "the file helper refused the request")
                if isinstance(payload, dict)
                else ""
            )
            if code in ("invalid_path", "invalid_limit", "invalid_request"):
                raise SandboxValidationError(message)
            if code in ("symlink", "hardlink", "escape"):
                raise SandboxForbidden(message)
            raise SandboxError(message)
        return payload

    # ── browser ───────────────────────────────────────────────────────────

    def browser_fetch(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
        url: str,
        timeout_ms: int = 45_000,
        settle_ms: int = 10_000,
        max_bytes: int | None = None,
    ) -> BrowserPage:
        record = self._handle(handle_id, project_id=project_id)
        parsed_authority = self._authorize_handle(record, project_id, authority)
        if record.handle.purpose is not SandboxPurpose.BROWSER:
            raise SandboxForbidden("this handle is a command sandbox and has no browser")
        if parsed_authority.role != "RESEARCH":
            raise SandboxForbidden("only the Research role may fetch a rendered page")
        if record.handle.state is not SandboxState.READY:
            raise SandboxConflict(
                f"the browser sandbox {handle_id!r} is {record.handle.state.value}, not READY"
            )
        if not 1 <= timeout_ms <= 300_000:
            raise SandboxValidationError("timeout_ms must be in 1..300000")
        if not 1 <= settle_ms <= 60_000:
            raise SandboxValidationError("settle_ms must be in 1..60000")
        limit = max_bytes or self.config.max_file_bytes
        if limit <= 0 or limit > self.config.max_file_bytes:
            raise SandboxValidationError(
                f"max_bytes must be in 1..{self.config.max_file_bytes}"
            )
        # A pre-flight policy check on the supervisor's own resolver. The
        # browser helper and the proxy check again; this one turns a refusal
        # into an UnsafeURL-shaped error before a container is involved.
        try:
            addressing.guard(url)
        except addressing.UnsafeURL as exc:
            raise SandboxUnsafeTarget(url, exc.reason) from exc
        bound = min(
            record.limits.timeout_seconds,
            timeout_ms / 1000.0 + settle_ms / 1000.0 + self.config.browser_startup_seconds,
        )
        if bound < timeout_ms / 1000.0:
            raise SandboxValidationError(
                "the requested navigation timeout exceeds the sandbox command ceiling"
            )
        run_id = f"run_{uuid.uuid4().hex}"
        run = RunRef(
            run_id=run_id,
            handle_id=handle_id,
            project_id=project_id,
            action_id="browser",
            state=RunState.RUNNING,
        )
        run_record = RunRecord(
            run=run,
            handle_id=handle_id,
            container=record.container,
            command=self.redactor.redact_argv(("browser_fetch", url)),
            cwd="",
            env_refs=(),
            command_digest=hash_bytes(url.encode("utf-8")),
            started_at=_utcnow(),
        )
        with self._lock:
            self._runs[run_id] = run_record
        self._write_run(run_record)
        command = [
            "python3",
            "-m",
            "ravel.sandbox.browser_fetch",
            "--url",
            url,
            "--proxy-server",
            record.proxy_url or "",
            "--timeout-ms",
            str(timeout_ms),
            "--settle-ms",
            str(settle_ms),
            "--max-bytes",
            str(limit),
        ]
        started = time.monotonic()
        try:
            result = self.engine.exec_capture(
                record.container,
                command,
                # The helper's bound includes browser startup, settlement, and
                # navigation; it must stay inside the sandbox's command ceiling,
                # which is what makes a hung page a quota event rather than an
                # indefinitely held container.
                timeout=bound,
                env={
                    "PYTHONPATH": SANDBOX_CODE_PATH,
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "HOME": "/tmp",
                    "TMPDIR": "/tmp",
                },
            )
            payload = self._browser_payload(result.stdout)
            if not payload.get("ok"):
                code = str(payload.get("error_code", "browser_failed"))
                message = self.redactor.redact(
                    str(payload.get("error", "browser fetch failed"))
                )
                raise _BrowserFailure(code, message)
            page = BrowserPage(
                requested_url=str(payload["requested_url"]),
                final_url=str(payload["final_url"]),
                status_code=payload.get("status_code"),
                title=str(payload.get("title", "")),
                html=str(payload["html"]),
                content_hash=str(payload["content_hash"]),
                media_type=str(payload.get("media_type", "text/html")),
                links=tuple(payload.get("links") or ()),
                note=str(payload.get("note", "")),
                truncated=bool(payload.get("truncated", False)),
            )
            if hash_bytes(page.body) != page.content_hash:
                raise SandboxIntegrityError(
                    "the rendered page's hash does not match the bytes returned"
                )
        except DockerError as exc:
            duration = time.monotonic() - started
            message = self.redactor.redact(str(exc))
            self._finish_browser_run(run_record, RunState.FAILED, duration, detail=message)
            raise SandboxUnavailable(f"the browser helper did not return: {message}") from exc
        except _BrowserFailure as exc:
            duration = time.monotonic() - started
            self._finish_browser_run(
                run_record, RunState.FAILED, duration, detail=f"{exc.code}: {exc.message}"
            )
            if exc.code == "unsafe_url":
                raise SandboxUnsafeTarget(url, exc.message) from exc
            raise SandboxError(exc.message) from exc
        except SandboxError as exc:
            duration = time.monotonic() - started
            self._finish_browser_run(
                run_record, RunState.FAILED, duration, detail=self.redactor.redact(str(exc))
            )
            raise
        duration = time.monotonic() - started
        self._finish_browser_run(
            run_record, RunState.SUCCEEDED, duration, detail="rendered page fetched"
        )
        return page

    def _browser_payload(self, stdout: str) -> dict[str, Any]:
        lines = [line for line in stdout.splitlines() if line.strip()]
        if not lines:
            raise SandboxError("the browser helper returned no output")
        try:
            payload = json.loads(lines[-1])
        except ValueError as exc:
            raise SandboxError("the browser helper returned output that is not JSON") from exc
        if not isinstance(payload, dict):
            raise SandboxError("the browser helper returned a non-object")
        return payload

    def _finish_browser_run(
        self, record: RunRecord, state: RunState, duration: float, *, detail: str
    ) -> None:
        record.run = RunRef(
            run_id=record.run.run_id,
            handle_id=record.handle_id,
            project_id=record.run.project_id,
            action_id=record.run.action_id,
            state=state,
            created_at=record.run.created_at,
        )
        record.finished_at = _utcnow()
        record.duration_seconds = duration
        record.detail = self.redactor.redact(detail)
        if state is RunState.SUCCEEDED:
            record.exit_code = 0
        self._write_run(record)

    # ── lifecycle ─────────────────────────────────────────────────────────

    def stop(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
    ) -> SandboxHandle:
        record = self._handle(handle_id, project_id=project_id)
        self._authorize_handle(record, project_id, authority)
        if record.handle.state is SandboxState.DESTROYED:
            raise SandboxConflict("a destroyed sandbox cannot be stopped")
        self._set_state(record, SandboxState.STOPPING)
        self._cancel_active_for(record)
        self.engine.stop(record.container, timeout=self.config.stop_grace_seconds)
        if record.proxy:
            self.engine.stop(record.proxy, timeout=self.config.stop_grace_seconds)
        return self._set_state(record, SandboxState.STOPPED)

    def destroy(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
    ) -> SandboxHandle:
        record = self._handle(handle_id, project_id=project_id)
        self._authorize_handle(record, project_id, authority)
        self._cancel_active_for(record)
        self.engine.remove(record.container)
        if record.proxy:
            self.engine.remove(record.proxy)
        if record.network:
            self.engine.network_remove(record.network)
        self.engine.volume_remove(record.volume)
        return self._set_state(record, SandboxState.DESTROYED)

    def reconnect(
        self,
        *,
        handle_id: str,
        project_id: str,
        authority: dict[str, Any] | SandboxAuthority,
    ) -> SandboxHandle:
        record = self._handle(handle_id, project_id=project_id)
        self._authorize_handle(record, project_id, authority)
        if record.handle.state is SandboxState.DESTROYED:
            raise SandboxConflict("a destroyed sandbox cannot be reconnected")
        if record.handle.state is SandboxState.FAILED:
            raise SandboxConflict(
                f"the sandbox is FAILED and cannot be reconnected: {record.last_error}"
            )
        if record.handle.state is SandboxState.READY:
            return record.handle
        if not self.engine.container_present(record.container):
            self._set_state(
                record,
                SandboxState.FAILED,
                "the container no longer exists; destroy and reprovision",
            )
            raise SandboxConflict(
                "the sandbox container no longer exists; destroy the handle and provision a "
                "new one"
            )
        self._start_record(record)
        return record.handle

    def _cancel_active_for(self, record: HandleRecord) -> None:
        for active in list(self._active.values()):
            if active.record.handle_id != record.handle.handle_id:
                continue
            with active.lock:
                active.cancel_requested = True
            self._kill_run(active)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not any(
                active.record.handle_id == record.handle.handle_id
                for active in self._active.values()
            ):
                return
            time.sleep(0.05)

    def logs(self, *, handle_id: str, project_id: str) -> str:
        record = self._handle(handle_id, project_id=project_id)
        parts = [f"--- sandbox {record.container} ---", self.engine.logs(record.container)]
        if record.proxy:
            parts.extend(
                [f"--- proxy {record.proxy} ---", self.engine.logs(record.proxy)]
            )
        return self.redactor.redact("\n".join(parts))

    def handle(self, *, handle_id: str, project_id: str) -> SandboxHandle:
        return self._handle(handle_id, project_id=project_id).handle

    # ── validation helpers ────────────────────────────────────────────────

    def _validate_argv(self, argv: list[str]) -> tuple[str, ...]:
        if not isinstance(argv, list) or not argv:
            raise SandboxValidationError("a sandbox command is a non-empty argument list")
        if len(argv) > self.config.max_argv_arguments:
            raise SandboxValidationError(
                f"a sandbox command may have at most {self.config.max_argv_arguments} arguments"
            )
        for part in argv:
            if not isinstance(part, str):
                raise SandboxValidationError("every sandbox command argument must be a string")
            if "\x00" in part:
                raise SandboxValidationError("a sandbox command argument may not contain NUL")
            if len(part) > 16 * 1024:
                raise SandboxValidationError("a sandbox command argument is too long")
        total = sum(len(part) for part in argv)
        if total > self.config.max_argv_bytes:
            raise SandboxValidationError(
                f"the command's arguments exceed {self.config.max_argv_bytes} bytes"
            )
        return tuple(argv)

    def _validate_env_refs(self, refs: list[str]) -> tuple[str, ...]:
        if not isinstance(refs, list):
            raise SandboxValidationError("env_refs must be a list of environment names")
        result: list[str] = []
        for name in refs:
            if not isinstance(name, str) or not name:
                raise SandboxValidationError("an env ref must be a non-empty name")
            if name not in self.config.env_allowlist:
                raise SandboxForbidden(
                    f"environment name {name!r} is not in the supervisor's allowlist; "
                    "a sandbox never inherits the supervisor's environment"
                )
            if name not in result:
                result.append(name)
        return tuple(result)

    def _validate_timeout(self, requested: float | None, limits: SandboxLimits) -> float:
        if requested is None:
            return limits.timeout_seconds
        try:
            timeout = float(requested)
        except (TypeError, ValueError) as exc:
            raise SandboxValidationError("timeout_seconds must be a number") from exc
        if timeout <= 0:
            raise SandboxValidationError("timeout_seconds must be positive")
        if timeout > limits.timeout_seconds:
            raise SandboxValidationError(
                f"timeout_seconds={timeout} exceeds this sandbox's limit "
                f"{limits.timeout_seconds}; a command may be shorter, never longer"
            )
        return timeout

    @staticmethod
    def _validate_action_id(action_id: str) -> str:
        if not isinstance(action_id, str) or not action_id.strip():
            raise SandboxValidationError(
                "a sandbox command must cite the Action that authorizes it"
            )
        if len(action_id) > 200:
            raise SandboxValidationError("the action id is too long")
        return action_id

    def _validate_command_digest(
        self,
        digest: str,
        command: tuple[str, ...],
        cwd: str,
        refs: tuple[str, ...],
    ) -> str:
        computed = hash_bytes(
            json.dumps(
                {"argv": list(command), "cwd": cwd, "env_refs": sorted(set(refs))},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        )
        if digest and digest != computed:
            raise SandboxForbidden(
                "the command digest in the request does not match the command the "
                "supervisor would run; refusing to execute a command whose authorization "
                "names different bytes"
            )
        return computed


def _handle_error(exc: SandboxError) -> _ApiError:
    if isinstance(exc, SandboxNotFound):
        return _ApiError(404, "not_found", str(exc))
    if isinstance(exc, SandboxUnsafeTarget):
        return _ApiError(403, "unsafe_url", str(exc))
    if isinstance(exc, SandboxForbidden):
        return _ApiError(403, "forbidden", str(exc))
    if isinstance(exc, SandboxValidationError):
        return _ApiError(422, "invalid_request", str(exc))
    if isinstance(exc, SandboxConflict):
        return _ApiError(409, "conflict", str(exc))
    if isinstance(exc, SandboxIntegrityError):
        return _ApiError(409, "integrity", str(exc))
    if isinstance(exc, SandboxUnavailable):
        return _ApiError(503, "unavailable", str(exc))
    return _ApiError(500, "sandbox_error", str(exc))


def _request_project(
    request: Request, body: dict[str, Any] | None = None, query: str | None = None
) -> str:
    project = str((body or {}).get("project_id") or query or "").strip()
    header = request.headers.get("x-ravel-project", "").strip()
    if not project:
        raise _ApiError(422, "invalid_request", "the request must name a project_id")
    if not header:
        raise _ApiError(
            403,
            "forbidden",
            "the request must carry X-RAVEL-Project; the supervisor binds every "
            "operation to the project stated at the boundary",
        )
    if header != project:
        raise _ApiError(
            403,
            "forbidden",
            "X-RAVEL-Project does not match the project_id in the request",
        )
    return project


def create_app(
    config: SupervisorConfig | None = None,
    *,
    supervisor: SandboxSupervisor | None = None,
) -> FastAPI:
    """The FastAPI application, with the supervisor injected for tests."""
    configuration = config or SupervisorConfig.from_env()
    service = supervisor or SandboxSupervisor(configuration)
    expected_token = configuration.token
    app = FastAPI(title="RAVEL sandbox supervisor", docs_url=None, redoc_url=None)

    @app.exception_handler(_ApiError)
    async def api_error_handler(_: Request, exc: _ApiError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status, content={"code": exc.code, "message": exc.message}
        )

    @app.exception_handler(SandboxError)
    async def sandbox_error_handler(_: Request, exc: SandboxError) -> JSONResponse:
        api = _handle_error(exc)
        return JSONResponse(
            status_code=api.status, content={"code": api.code, "message": api.message}
        )

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any):  # type: ignore[no-untyped-def]
        if request.url.path == "/healthz":
            return await call_next(request)
        authorization = request.headers.get("authorization", "")
        scheme, _, presented = authorization.partition(" ")
        if scheme.lower() != "bearer" or not secrets.compare_digest(presented, expected_token):
            return JSONResponse(
                status_code=401,
                content={
                    "code": "unauthorized",
                    "message": "a bearer supervisor token is required",
                },
            )
        return await call_next(request)

    async def json_body(request: Request) -> dict[str, Any]:
        try:
            data = await request.json()
        except ValueError as exc:
            raise _ApiError(400, "invalid_json", "the request body is not JSON") from exc
        if not isinstance(data, dict):
            raise _ApiError(400, "invalid_json", "the request body must be a JSON object")
        return data

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        try:
            docker_ok = service.engine.available()
            version = service.engine.server_version() if docker_ok else ""
            image_present = (
                service.engine.image_present(configuration.image) if docker_ok else False
            )
        except Exception:
            docker_ok, version, image_present = False, "", False
        return {
            "ok": docker_ok,
            "docker": docker_ok,
            "docker_version": version,
            "image": configuration.image,
            "image_present": image_present,
            "provider": "docker",
        }

    @app.get("/capabilities")
    def capabilities() -> dict[str, Any]:
        return SandboxCapabilities().as_dict()

    @app.post("/provision")
    async def provision(request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        handle = service.provision(
            project_id=project,
            authority=body.get("authority") or {},
            limits=body.get("limits"),
            purpose=body.get("purpose", SandboxPurpose.COMMAND),
        )
        return handle.as_dict()

    @app.get("/handles/{handle_id}")
    def get_handle(handle_id: str, request: Request, project_id: str = "") -> dict[str, Any]:
        project = _request_project(request, query=project_id)
        return service.handle(handle_id=handle_id, project_id=project).as_dict()

    @app.post("/handles/{handle_id}/reconnect")
    async def reconnect(handle_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        handle = service.reconnect(
            handle_id=handle_id,
            project_id=project,
            authority=body.get("authority") or {},
        )
        return handle.as_dict()

    @app.post("/handles/{handle_id}/stop")
    async def stop(handle_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        handle = service.stop(
            handle_id=handle_id,
            project_id=project,
            authority=body.get("authority") or {},
        )
        return handle.as_dict()

    @app.post("/handles/{handle_id}/destroy")
    async def destroy(handle_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        handle = service.destroy(
            handle_id=handle_id,
            project_id=project,
            authority=body.get("authority") or {},
        )
        return handle.as_dict()

    @app.post("/handles/{handle_id}/files/list")
    async def files_list(handle_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        entries = service.files_list(
            handle_id=handle_id,
            project_id=project,
            authority=body.get("authority") or {},
            path=str(body.get("path", "")),
        )
        return {"entries": [entry.as_dict() for entry in entries]}

    @app.post("/handles/{handle_id}/files/read")
    async def files_read(handle_id: str, request: Request) -> dict[str, Any]:
        import base64

        body = await json_body(request)
        project = _request_project(request, body)
        data, digest = service.files_read(
            handle_id=handle_id,
            project_id=project,
            authority=body.get("authority") or {},
            path=str(body.get("path", "")),
            max_bytes=body.get("max_bytes"),
        )
        return {
            "path": body.get("path"),
            "content_hash": digest,
            "size_bytes": len(data),
            "data_b64": base64.b64encode(data).decode("ascii"),
        }

    @app.post("/handles/{handle_id}/files/write")
    async def files_write(handle_id: str, request: Request) -> dict[str, Any]:
        import base64

        body = await json_body(request)
        project = _request_project(request, body)
        try:
            data = base64.b64decode(str(body.get("data_b64", "")).encode("ascii"), validate=True)
        except (ValueError, UnicodeEncodeError) as exc:
            raise _ApiError(422, "invalid_request", "data_b64 is not base64") from exc
        digest, size = service.files_write(
            handle_id=handle_id,
            project_id=project,
            authority=body.get("authority") or {},
            path=str(body.get("path", "")),
            data=data,
        )
        return {"path": body.get("path"), "content_hash": digest, "size_bytes": size}

    @app.post("/handles/{handle_id}/browser/fetch")
    async def browser_fetch(handle_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        # A render can take the better part of a minute, and the service is
        # synchronous. Running it on the event loop would make every other
        # request — including the stop that must end it — wait for the render.
        page = await run_in_threadpool(
            lambda: service.browser_fetch(
                handle_id=handle_id,
                project_id=project,
                authority=body.get("authority") or {},
                url=str(body.get("url", "")),
                timeout_ms=int(body.get("timeout_ms", 45_000)),
                settle_ms=int(body.get("settle_ms", 10_000)),
                max_bytes=body.get("max_bytes"),
            )
        )
        return page.as_dict()

    @app.post("/handles/{handle_id}/execute")
    async def execute(handle_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        # The command blocks until it finishes or its wall clock expires. Off
        # the event loop, a concurrent stop/destroy can still be served and
        # kill the process, which is the lifecycle guarantee T19-AC2 makes.
        run = await run_in_threadpool(
            lambda: service.execute(
                handle_id=handle_id,
                project_id=project,
                authority=body.get("authority") or {},
                argv=body.get("argv") or [],
                cwd=str(body.get("cwd", "")),
                env_refs=list(body.get("env_refs") or []),
                timeout_seconds=body.get("timeout_seconds"),
                action_id=str(body.get("action_id", "")),
                command_digest=str(body.get("command_digest", "")),
            )
        )
        return run.as_dict()

    @app.get("/runs/{run_id}")
    def run_status(run_id: str, request: Request, project_id: str = "") -> dict[str, Any]:
        project = _request_project(request, query=project_id)
        return service.status(run_id=run_id, project_id=project).as_dict()

    @app.post("/runs/{run_id}/cancel")
    async def cancel(run_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        status = service.cancel(
            run_id=run_id,
            project_id=project,
            authority=body.get("authority") or {},
        )
        return status.as_dict()

    @app.post("/runs/{run_id}/collect")
    async def collect(run_id: str, request: Request) -> dict[str, Any]:
        body = await json_body(request)
        project = _request_project(request, body)
        refs = service.collect(run_id=run_id, project_id=project)
        return {"artifacts": [ref.as_dict() for ref in refs]}

    @app.get("/runs/{run_id}/artifacts/{name}")
    def artifact_bytes(run_id: str, name: str, request: Request, project_id: str = "") -> Response:
        project = _request_project(request, query=project_id)
        data, digest = service.artifact_bytes(run_id=run_id, name=name, project_id=project)
        return Response(
            content=data,
            media_type=mimetypes.guess_type(name)[0] or "application/octet-stream",
            headers={"X-RAVEL-Content-Hash": digest},
        )

    @app.get("/handles/{handle_id}/logs")
    def logs(handle_id: str, request: Request, project_id: str = "") -> dict[str, Any]:
        project = _request_project(request, query=project_id)
        return {"logs": service.logs(handle_id=handle_id, project_id=project)}

    app.state.supervisor = service
    return app


__all__ = [
    "HandleRecord",
    "RunRecord",
    "SandboxSupervisor",
    "SupervisorConfig",
    "create_app",
    "sanitize_project",
]
