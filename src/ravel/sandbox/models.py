"""The values the SandboxProvider contract exchanges.

These are the objects on the wire between the RAVEL process and the sandbox
supervisor, and they are deliberately plain: enums for the closed vocabularies,
frozen dataclasses for the records, JSON-ready dictionaries for the wire. A
provider that returns "a dict shaped roughly like a handle" is a provider whose
callers discover its fields by reading the implementation, and the contract in
`docs/premium/CONTRACTS.md` exists so that does not happen.

The defaults in `SandboxLimits` are the production ceilings from the T19 card:
2 CPUs, 4 GiB of memory, 256 PIDs, 10 GiB of scratch, 120 seconds per command.
They are *ceilings*, not grants: `ravel.sandbox.authority` intersects them with
whatever an authority envelope allows, and only ever in the stricter direction.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utcnow() -> datetime:
    """The current time, timezone-aware, in the one spelling RAVEL stores."""
    return datetime.now(UTC)


class SandboxState(StrEnum):
    """Where a sandbox handle stands.

    The transitions are PROVISIONING -> READY -> STOPPING -> STOPPED, with
    DESTROYED reachable from STOPPED (or directly from READY, by destroying a
    running sandbox), and FAILED reachable from any live state when the runtime
    underneath it fails. A stopped sandbox can be started again: that is what
    `reconnect` is for, and it is why STOPPED is not terminal.
    """

    PROVISIONING = "PROVISIONING"
    READY = "READY"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    DESTROYED = "DESTROYED"
    FAILED = "FAILED"


class RunState(StrEnum):
    """Where one command stands.

    `CANCELLED` and `TIMED_OUT` are separate outcomes rather than both being
    "failed": a command that hit its wall clock and a command a person stopped
    are different records, and the exit code alone cannot tell them apart.
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMED_OUT = "TIMED_OUT"
    CANCELLED = "CANCELLED"
    LOST = "LOST"


class SandboxPurpose(StrEnum):
    """Why a sandbox exists.

    A COMMAND sandbox has no network at all. A BROWSER sandbox is attached to an
    internal network whose only exit is the controlled proxy, which is what
    makes a rendered fetch compatible with the browser's fail-closed rule.
    Purpose is fixed at provisioning and not a per-command option: a command
    sandbox that could ask for network at execution time would be a network
    switch a compromised command could flip.
    """

    COMMAND = "COMMAND"
    BROWSER = "BROWSER"


@dataclass(frozen=True, slots=True)
class SandboxCapabilities:
    """What a compliant sandbox this deployment can run actually provides.

    These are facts about the implementation, not requests. A caller that needs
    a capability that is False gets `UnsupportedCapability`; it never gets a
    fallback that runs outside the sandbox.
    """

    provider: str = "docker"
    isolated: bool = True
    read_only_rootfs: bool = True
    non_root: bool = True
    capabilities_dropped: bool = True
    no_new_privileges: bool = True
    seccomp: bool = True
    resource_quotas: bool = True
    project_workspace: bool = True
    browser: bool = True
    browser_egress_proxy: bool = True
    docker_socket: bool = False
    privileged: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "isolated": self.isolated,
            "read_only_rootfs": self.read_only_rootfs,
            "non_root": self.non_root,
            "capabilities_dropped": self.capabilities_dropped,
            "no_new_privileges": self.no_new_privileges,
            "seccomp": self.seccomp,
            "resource_quotas": self.resource_quotas,
            "project_workspace": self.project_workspace,
            "browser": self.browser,
            "browser_egress_proxy": self.browser_egress_proxy,
            "docker_socket": self.docker_socket,
            "privileged": self.privileged,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SandboxCapabilities:
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


#: The byte constants, named so a reading of the limits does not require
#: counting zeroes.
MIB = 1024 * 1024
GIB = 1024 * MIB


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource ceilings for one sandbox.

    Every value is an upper bound. The supervisor applies the intersection of
    this object and the authority envelope; a request that tries to widen the
    configured ceiling is refused rather than silently clamped, because a
    caller asking for more is a caller operating on a wrong belief about what
    it was granted.
    """

    cpu_cores: float = 2.0
    memory_bytes: int = 4 * GIB
    pids: int = 256
    scratch_bytes: int = 10 * GIB
    timeout_seconds: float = 120.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpu_cores": self.cpu_cores,
            "memory_bytes": self.memory_bytes,
            "pids": self.pids,
            "scratch_bytes": self.scratch_bytes,
            "timeout_seconds": self.timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SandboxLimits:
        known = {field_name for field_name in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass(frozen=True, slots=True)
class SandboxHandle:
    """A sandbox a caller may use, and the project it belongs to.

    `project_id` is stated by the handle rather than remembered by the caller,
    so a handle that reaches a different project's supervisor is refused on the
    value in the handle and not on a caller's belief. `epoch` is the authority
    lease epoch at provisioning time; an execute that carries a different epoch
    is refused, because a new lease is a new grant.
    """

    handle_id: str
    provider: str
    project_id: str
    epoch: int | None
    capabilities: SandboxCapabilities
    state: SandboxState
    purpose: SandboxPurpose = SandboxPurpose.COMMAND
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "handle_id": self.handle_id,
            "provider": self.provider,
            "project_id": self.project_id,
            "epoch": self.epoch,
            "capabilities": self.capabilities.as_dict(),
            "state": self.state.value,
            "purpose": self.purpose.value,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SandboxHandle:
        return cls(
            handle_id=str(data["handle_id"]),
            provider=str(data.get("provider", "docker")),
            project_id=str(data["project_id"]),
            epoch=data.get("epoch"),
            capabilities=SandboxCapabilities.from_dict(data.get("capabilities") or {}),
            state=SandboxState(data["state"]),
            purpose=SandboxPurpose(data.get("purpose", SandboxPurpose.COMMAND)),
            created_at=_datetime(data.get("created_at")),
            updated_at=_datetime(data.get("updated_at")),
        )


@dataclass(frozen=True, slots=True)
class RunRef:
    """A handle on one command, returned before the command has finished.

    The contract distinguishes submission from outcome: `execute` answers "which
    run is this", and `status` answers "what happened". That is what makes a
    supervisor restart survivable — the run id is durable, and the process that
    started it need not be.
    """

    run_id: str
    handle_id: str
    project_id: str
    action_id: str
    state: RunState
    created_at: datetime = field(default_factory=utcnow)

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "handle_id": self.handle_id,
            "project_id": self.project_id,
            "action_id": self.action_id,
            "state": self.state.value,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunRef:
        return cls(
            run_id=str(data["run_id"]),
            handle_id=str(data["handle_id"]),
            project_id=str(data["project_id"]),
            action_id=str(data.get("action_id", "")),
            state=RunState(data["state"]),
            created_at=_datetime(data.get("created_at")),
        )


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """One output file a run produced, identified by its content.

    The hash is computed over the bytes read out of the sandbox, and the bytes
    are held by the supervisor until the caller registers them through the
    Gateway. Registration re-hashes and must agree; a mismatch is a failure, not
    a correction.
    """

    name: str
    relative_path: str
    content_hash: str
    size_bytes: int
    media_type: str = "application/octet-stream"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "relative_path": self.relative_path,
            "content_hash": self.content_hash,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ArtifactRef:
        return cls(
            name=str(data["name"]),
            relative_path=str(data["relative_path"]),
            content_hash=str(data["content_hash"]),
            size_bytes=int(data["size_bytes"]),
            media_type=str(data.get("media_type", "application/octet-stream")),
        )


@dataclass(frozen=True, slots=True)
class FileEntry:
    """One entry in a sandbox directory listing.

    `kind` is `file`, `directory`, or `symlink`. A symlink is reported rather
    than followed: its target is not part of the listing, and reading through
    one is refused by `ravel.sandbox.fileops`.
    """

    name: str
    path: str
    kind: str
    size_bytes: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "kind": self.kind,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True, slots=True)
class RunStatus:
    """What happened to a run, as the supervisor recorded it.

    Output is already redacted and already truncated to `output_limit_bytes`.
    `command` is the redacted argv, and that is the only spelling the record
    keeps: an argument that held a credential is not recoverable from a run
    record.
    """

    run: RunRef
    state: RunState
    exit_code: int | None
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    command: tuple[str, ...] = ()
    cwd: str = ""
    env_refs: tuple[str, ...] = ()
    stdout: str = ""
    stderr: str = ""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    finished_at: datetime | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "run": self.run.as_dict(),
            "state": self.state.value,
            "exit_code": self.exit_code,
            "timed_out": self.timed_out,
            "cancelled": self.cancelled,
            "duration_seconds": self.duration_seconds,
            "command": list(self.command),
            "cwd": self.cwd,
            "env_refs": list(self.env_refs),
            "stdout": self.stdout,
            "stderr": self.stderr,
            "stdout_truncated": self.stdout_truncated,
            "stderr_truncated": self.stderr_truncated,
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RunStatus:
        return cls(
            run=RunRef.from_dict(data["run"]),
            state=RunState(data["state"]),
            exit_code=data.get("exit_code"),
            timed_out=bool(data.get("timed_out", False)),
            cancelled=bool(data.get("cancelled", False)),
            duration_seconds=float(data.get("duration_seconds", 0.0)),
            command=tuple(data.get("command") or ()),
            cwd=str(data.get("cwd", "")),
            env_refs=tuple(data.get("env_refs") or ()),
            stdout=str(data.get("stdout", "")),
            stderr=str(data.get("stderr", "")),
            stdout_truncated=bool(data.get("stdout_truncated", False)),
            stderr_truncated=bool(data.get("stderr_truncated", False)),
            finished_at=_datetime(data.get("finished_at")) if data.get("finished_at") else None,
            detail=str(data.get("detail", "")),
        )


@dataclass(frozen=True, slots=True)
class BrowserPage:
    """A rendered page, returned by a browser sandbox.

    `final_url` is the address the browser actually landed on, after redirects.
    `html` is the rendered DOM, already bounded by the supervisor's output
    limit. Hashing happens where the bytes are, so the hash in this record is
    the hash of the bytes in `html` and a registration can verify it.
    """

    requested_url: str
    final_url: str
    status_code: int | None
    title: str
    html: str
    content_hash: str
    media_type: str = "text/html"
    links: tuple[str, ...] = ()
    note: str = "read from the rendered document inside a sandbox with proxy egress"
    truncated: bool = False

    @property
    def body(self) -> bytes:
        return self.html.encode("utf-8", errors="replace")

    def as_dict(self) -> dict[str, Any]:
        return {
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "status_code": self.status_code,
            "title": self.title,
            "html": self.html,
            "content_hash": self.content_hash,
            "media_type": self.media_type,
            "links": list(self.links),
            "note": self.note,
            "truncated": self.truncated,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BrowserPage:
        return cls(
            requested_url=str(data["requested_url"]),
            final_url=str(data["final_url"]),
            status_code=data.get("status_code"),
            title=str(data.get("title", "")),
            html=str(data["html"]),
            content_hash=str(data["content_hash"]),
            media_type=str(data.get("media_type", "text/html")),
            links=tuple(data.get("links") or ()),
            note=str(data.get("note", "")),
            truncated=bool(data.get("truncated", False)),
        )


class SandboxError(RuntimeError):
    """Base class for a sandbox request that did not produce its result."""


class SandboxUnavailable(SandboxError):
    """The supervisor cannot be reached, or cannot run a sandbox at all."""


class SandboxNotFound(SandboxError):
    """The named handle or run does not exist, or is not the caller's."""


class SandboxForbidden(SandboxError):
    """The caller's authority does not cover this request."""


class SandboxConflict(SandboxError):
    """The request does not fit the handle's current state."""


class SandboxValidationError(SandboxError):
    """The request is malformed before it reaches Docker."""


class UnsupportedCapability(SandboxError):
    """The requested operation is not provided by this sandbox implementation.

    Raised rather than answered by running the operation somewhere less
    isolated. A fallback here would be a way for a caller to obtain host
    execution by asking for a capability the sandbox does not have.
    """


class SandboxIntegrityError(SandboxError):
    """Bytes read from a sandbox do not match the hash that names them."""


class SandboxUnsafeTarget(SandboxForbidden):
    """A browser sandbox refused a URL before the browser requested it.

    Separate from an ordinary `SandboxForbidden` because the reason is a
    decision about the URL, not about the caller's authority. The browser path
    reports it as `addressing.UnsafeURL`, which is the type every other research
    path uses for "RAVEL would not fetch this".
    """

    def __init__(self, url: str, reason: str) -> None:
        self.url = url
        self.reason = reason
        super().__init__(f"refusing to fetch {url}: {reason}")


def hash_bytes(data: bytes) -> str:
    """The one content-address spelling: `sha256:<hex>`."""
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def canonical_json(data: Any) -> bytes:
    """A deterministic JSON encoding, used for command digests.

    Sorted keys and no whitespace, so two processes that agree on the value
    agree on its digest without agreeing on a serializer's defaults.
    """
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def command_digest(
    *, argv: list[str] | tuple[str, ...], cwd: str, env_refs: list[str] | tuple[str, ...]
) -> str:
    """A digest of exactly what a sandbox would execute.

    The digest covers argv, working directory, and environment names — not
    environment values. A run record and an Action both hold this digest, so a
    claim that "the action authorized this command" is a comparison of hashes
    rather than of prose. Values are omitted because one of them may be a
    credential, and a digest is a record that gets stored.

    Note that this is a *digest of the request*, not of the effect. A command
    can write files, and no digest can predict those; that is why the artifact
    path re-hashes the bytes it registers.
    """
    if not argv:
        raise SandboxValidationError("a sandbox command must have at least one argument")
    if any(not isinstance(part, str) or "\x00" in part for part in argv):
        raise SandboxValidationError("sandbox command arguments must be NUL-free strings")
    return hash_bytes(
        canonical_json(
            {
                "argv": list(argv),
                "cwd": cwd,
                "env_refs": sorted(set(env_refs)),
            }
        )
    )


def _datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        return datetime.fromisoformat(value)
    return utcnow()
