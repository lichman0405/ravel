"""The SandboxProvider RAVEL processes use. It contains no Docker code.

This module is imported by the API, the DSH host, and agent tool servers. It
speaks HTTP to the sandbox supervisor, which is the only process with a Docker
socket. There is deliberately no `docker` import, no `subprocess`, no Unix
socket, and no host-path handling here: a deployment can give an agent this
module and the supervisor's URL and token, and still give it no way to reach
the Docker API.

The provider is bound to one project at construction. Every request carries
that project both as a body field and as `X-RAVEL-Project`, and the supervisor
checks both against the handle. A handle returned for another project is
refused before it is used, so holding a provider cannot become a way to act on
a neighbour.

Execution is Action-gated twice:

- the provider computes a digest of argv, cwd, and environment names;
- an `ActionGuard` verifies that the named `SANDBOX_EXEC` Action was claimed by
  this project and this role and that its recorded digest is this command's.

The supervisor verifies the digest again against the argv it is about to run.
Neither check is a model's recollection; both are comparisons against records.
A provider constructed without a guard refuses to execute, which is why the
test harness has to hand it an explicit guard rather than silently getting an
unguarded path.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import threading
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

from ravel.sandbox.models import (
    ArtifactRef,
    BrowserPage,
    FileEntry,
    RunRef,
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
    SandboxUnavailable,
    SandboxUnsafeTarget,
    SandboxValidationError,
    UnsupportedCapability,
    command_digest,
    hash_bytes,
)

logger = logging.getLogger(__name__)

#: Added to the command's own time for the HTTP round trip. The supervisor
#: waits for the command; the client must not time out first and leave the
#: caller believing nothing ran.
_HTTP_MARGIN_SECONDS = 30.0

#: The capabilities a compliant sandbox must attest before the browser path
#: accepts it. Every one is a fact the supervisor reports about the container
#: it actually started, not a claim the caller makes.
_REQUIRED_CAPABILITIES = (
    "isolated",
    "read_only_rootfs",
    "non_root",
    "capabilities_dropped",
    "no_new_privileges",
    "seccomp",
    "resource_quotas",
    "project_workspace",
)


class ActionGuard(Protocol):
    """The narrow Action check a sandbox execution needs.

    Implemented by `ravel.sandbox.action_guard.DatabaseActionGuard` for a real
    project, and by `StaticActionGuard` where a test or a container without a
    database needs a recorded grant. The protocol is here so the provider does
    not import SQLAlchemy.
    """

    def verify(
        self,
        *,
        action_id: str,
        project_id: str,
        command_digest: str,
        authority: Any,
    ) -> None: ...

    def record_outcome(
        self, *, action_id: str, run: RunRef, authority: Any
    ) -> None: ...


class DockerSandboxProvider:
    """A project-bound HTTP client for the Docker sandbox supervisor."""

    provider = "docker"

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        project_id: str,
        action_guard: ActionGuard | None = None,
        http_timeout_seconds: float = 180.0,
    ) -> None:
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise SandboxValidationError(
                f"sandbox supervisor URL {base_url!r} is not an http(s) origin"
            )
        if parsed.query or parsed.fragment:
            raise SandboxValidationError(
                "the sandbox supervisor URL may not carry a query or fragment"
            )
        if not token or not token.strip():
            raise SandboxValidationError(
                "the sandbox provider needs the supervisor's bearer token; without it "
                "the supervisor refuses every request"
            )
        if not project_id or not project_id.strip():
            raise SandboxValidationError("the sandbox provider must be bound to a project")
        self._base = base_url.rstrip("/")
        self._token = token
        self.project_id = project_id.strip()
        self._action_guard = action_guard
        self._http_timeout = http_timeout_seconds
        self._capabilities: SandboxCapabilities | None = None
        self._runs: dict[str, tuple[RunRef, Any]] = {}
        self._handle_authorities: dict[str, Any] = {}
        self._browser_handle: SandboxHandle | None = None
        self._lock = threading.RLock()

    # ── transport ─────────────────────────────────────────────────────────

    def _url(self, path: str, query: dict[str, str] | None = None) -> str:
        url = f"{self._base}{path}"
        if query:
            url = f"{url}?{urllib.parse.urlencode(query)}"
        return url

    def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        timeout: float | None = None,
        unsafe_url: str | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        data = None
        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-RAVEL-Project": self.project_id,
            "Accept": "application/json",
            "User-Agent": "RAVEL-sandbox-provider/0.1",
        }
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self._url(path, query), data=data, method=method, headers=headers
        )
        bound = timeout if timeout is not None else self._http_timeout
        try:
            with urllib.request.urlopen(request, timeout=bound) as response:
                return response.read(), {k.lower(): v for k, v in response.headers.items()}
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            code, message = _error_from_payload(payload)
            if code == "unsafe_url":
                raise _unsafe_target(unsafe_url or "", message) from exc
            if exc.code == 401:
                raise SandboxForbidden(
                    "the sandbox supervisor rejected the deployment token"
                ) from exc
            if exc.code == 403:
                raise SandboxForbidden(message) from exc
            if exc.code == 404:
                raise SandboxNotFound(message) from exc
            if exc.code == 409:
                if code == "integrity":
                    raise SandboxIntegrityError(message) from exc
                raise SandboxConflict(message) from exc
            if exc.code == 422:
                raise SandboxValidationError(message) from exc
            if exc.code in (502, 503, 504):
                raise SandboxUnavailable(message) from exc
            raise SandboxError(message or f"the sandbox supervisor answered {exc.code}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise SandboxUnavailable(
                f"the sandbox supervisor at {self._base} is unreachable: {exc}"
            ) from exc

    def _json_request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        timeout: float | None = None,
        unsafe_url: str | None = None,
    ) -> dict[str, Any]:
        raw, _headers = self._request(
            method,
            path,
            body=body,
            query=query,
            timeout=timeout,
            unsafe_url=unsafe_url,
        )
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise SandboxError("the sandbox supervisor returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise SandboxError("the sandbox supervisor returned a non-object response")
        return payload

    # ── capabilities ──────────────────────────────────────────────────────

    def capabilities(self) -> SandboxCapabilities:
        """What the supervisor's implementation provides, as it reports it."""
        payload = self._json_request("GET", "/capabilities", timeout=15)
        capabilities = SandboxCapabilities.from_dict(payload)
        self._capabilities = capabilities
        return capabilities

    def _require_compliant(self, handle: SandboxHandle, *, browser: bool = False) -> None:
        capabilities = handle.capabilities
        missing = [
            name
            for name in _REQUIRED_CAPABILITIES
            if not bool(getattr(capabilities, name, False))
        ]
        if capabilities.docker_socket:
            missing.append("docker_socket_present")
        if capabilities.privileged:
            missing.append("privileged")
        if browser and not (capabilities.browser and capabilities.browser_egress_proxy):
            missing.append("browser_egress_proxy")
        if missing:
            raise UnsupportedCapability(
                "the sandbox handle does not attest the isolation this operation "
                f"requires ({', '.join(missing)}); refusing to fall back outside the "
                "sandbox"
            )

    # ── authority plumbing ────────────────────────────────────────────────

    def _authority_dict(self, authority: Any) -> dict[str, Any]:
        if authority is None:
            raise SandboxValidationError("every sandbox operation needs an authority")
        if isinstance(authority, dict):
            payload = dict(authority)
        elif hasattr(authority, "as_dict"):
            payload = authority.as_dict()
        else:
            raise SandboxValidationError("the sandbox authority is not serializable")
        if payload.get("project_id") != self.project_id:
            raise SandboxForbidden(
                f"this provider is bound to project {self.project_id!r}; the authority "
                f"names {payload.get('project_id')!r}"
            )
        return payload

    def _remember(self, handle: SandboxHandle, authority: Any) -> None:
        with self._lock:
            self._handle_authorities[handle.handle_id] = authority

    def _authority_for_handle(self, handle: SandboxHandle) -> Any:
        with self._lock:
            authority = self._handle_authorities.get(handle.handle_id)
        if authority is None:
            raise SandboxValidationError(
                "this provider has no authority for the handle; reconnect it with the "
                "current binding before using it"
            )
        return authority

    def _check_handle(self, handle: SandboxHandle, *, provider: str = "docker") -> None:
        if not isinstance(handle, SandboxHandle):
            raise SandboxValidationError("a sandbox operation needs a SandboxHandle")
        if handle.project_id != self.project_id:
            raise SandboxForbidden(
                f"the handle belongs to project {handle.project_id!r}, not this "
                f"provider's project {self.project_id!r}"
            )
        if handle.provider != provider:
            raise UnsupportedCapability(
                f"this provider runs {provider!r} sandboxes, not {handle.provider!r}"
            )

    # ── lifecycle ─────────────────────────────────────────────────────────

    def provision(
        self,
        project_id: str,
        authority: Any,
        limits: SandboxLimits | None = None,
        *,
        purpose: SandboxPurpose | str = SandboxPurpose.COMMAND,
    ) -> SandboxHandle:
        if project_id != self.project_id:
            raise SandboxForbidden(
                f"this provider is bound to project {self.project_id!r}, not {project_id!r}"
            )
        payload = self._authority_dict(authority)
        body: dict[str, Any] = {
            "project_id": project_id,
            "authority": payload,
            "purpose": SandboxPurpose(purpose).value,
        }
        if limits is not None:
            body["limits"] = limits.as_dict()
        handle = SandboxHandle.from_dict(
            self._json_request("POST", "/provision", body=body, timeout=180)
        )
        if handle.project_id != self.project_id:
            raise SandboxIntegrityError(
                "the supervisor returned a handle for a different project"
            )
        self._remember(handle, authority)
        return handle

    def reconnect(self, handle: SandboxHandle, authority: Any) -> SandboxHandle:
        self._check_handle(handle)
        payload = self._authority_dict(authority)
        result = SandboxHandle.from_dict(
            self._json_request(
                "POST",
                f"/handles/{handle.handle_id}/reconnect",
                body={"project_id": self.project_id, "authority": payload},
                timeout=60,
            )
        )
        if result.project_id != self.project_id:
            raise SandboxIntegrityError("reconnect returned a handle for another project")
        self._remember(result, authority)
        return result

    def stop(self, handle: SandboxHandle, authority: Any | None = None) -> SandboxHandle:
        self._check_handle(handle)
        payload = self._authority_dict(
            authority if authority is not None else self._authority_for_handle(handle)
        )
        result = SandboxHandle.from_dict(
            self._json_request(
                "POST",
                f"/handles/{handle.handle_id}/stop",
                body={"project_id": self.project_id, "authority": payload},
                timeout=60,
            )
        )
        if result.state.value not in ("STOPPED",):
            raise SandboxConflict(
                f"stop returned state {result.state.value}, not STOPPED"
            )
        return result

    def destroy(self, handle: SandboxHandle, authority: Any | None = None) -> SandboxHandle:
        self._check_handle(handle)
        payload = self._authority_dict(
            authority if authority is not None else self._authority_for_handle(handle)
        )
        result = SandboxHandle.from_dict(
            self._json_request(
                "POST",
                f"/handles/{handle.handle_id}/destroy",
                body={"project_id": self.project_id, "authority": payload},
                timeout=90,
            )
        )
        with self._lock:
            self._handle_authorities.pop(handle.handle_id, None)
            if self._browser_handle and self._browser_handle.handle_id == handle.handle_id:
                self._browser_handle = None
        if result.state.value != "DESTROYED":
            raise SandboxConflict(
                f"destroy returned state {result.state.value}, not DESTROYED"
            )
        return result

    # ── execution ─────────────────────────────────────────────────────────

    def execute(
        self,
        handle: SandboxHandle,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: str = "",
        env_refs: list[str] | tuple[str, ...] = (),
        timeout: float = 120.0,
        action_id: str = "",
        authority: Any | None = None,
    ) -> RunRef:
        self._check_handle(handle)
        self._require_compliant(handle)
        if handle.state.value != "READY":
            raise SandboxConflict(
                f"the sandbox handle is {handle.state.value}, not READY; reconnect it"
            )
        if not action_id:
            raise SandboxForbidden(
                "a sandbox command must cite a claimed SANDBOX_EXEC Action"
            )
        caller_authority = (
            authority if authority is not None else self._authority_for_handle(handle)
        )
        self._authority_dict(caller_authority)
        if self._action_guard is None:
            raise SandboxForbidden(
                "no Action verifier is configured; a sandbox command cannot be executed "
                "without a claimed SANDBOX_EXEC Action"
            )
        command = tuple(argv)
        digest = command_digest(argv=command, cwd=cwd, env_refs=env_refs)
        try:
            self._action_guard.verify(
                action_id=action_id,
                project_id=self.project_id,
                command_digest=digest,
                authority=caller_authority,
            )
        except PermissionError as exc:
            raise SandboxForbidden(str(exc)) from exc
        body = {
            "project_id": self.project_id,
            "authority": self._authority_dict(caller_authority),
            "argv": list(command),
            "cwd": cwd,
            "env_refs": list(env_refs),
            "timeout_seconds": timeout,
            "action_id": action_id,
            "command_digest": digest,
        }
        run = RunRef.from_dict(
            self._json_request(
                "POST",
                f"/handles/{handle.handle_id}/execute",
                body=body,
                timeout=timeout + _HTTP_MARGIN_SECONDS,
            )
        )
        if run.project_id != self.project_id or run.handle_id != handle.handle_id:
            raise SandboxIntegrityError(
                "the supervisor returned a run for a different project or handle"
            )
        with self._lock:
            self._runs[run.run_id] = (run, caller_authority)
        if self._action_guard is not None:
            try:
                self._action_guard.record_outcome(
                    action_id=action_id, run=run, authority=caller_authority
                )
            except Exception:
                # The command has already run. Failing this call would make the
                # caller believe nothing happened and invite a second attempt.
                logger.exception("could not record the Action outcome for %s", action_id)
        return run

    def status(self, run_ref: RunRef) -> RunStatus:
        if run_ref.project_id != self.project_id:
            raise SandboxForbidden("the run belongs to another project")
        return RunStatus.from_dict(
            self._json_request(
                "GET",
                f"/runs/{run_ref.run_id}",
                query={"project_id": self.project_id},
                timeout=30,
            )
        )

    def cancel(self, run_ref: RunRef) -> RunStatus:
        if run_ref.project_id != self.project_id:
            raise SandboxForbidden("the run belongs to another project")
        with self._lock:
            stored = self._runs.get(run_ref.run_id)
        if stored is None:
            raise SandboxForbidden(
                "this provider no longer holds the authority under which the run was "
                "started; reconnect and cancel it under the current binding"
            )
        run, authority = stored
        return RunStatus.from_dict(
            self._json_request(
                "POST",
                f"/runs/{run.run_id}/cancel",                body={
                    "project_id": self.project_id,
                    "authority": self._authority_dict(authority),
                },
                timeout=30,
            )
        )

    def collect(self, run_ref: RunRef) -> list[ArtifactRef]:
        if run_ref.project_id != self.project_id:
            raise SandboxForbidden("the run belongs to another project")
        payload = self._json_request(
            "POST",
            f"/runs/{run_ref.run_id}/collect",
            body={"project_id": self.project_id},
            timeout=120,
        )
        return [ArtifactRef.from_dict(item) for item in payload.get("artifacts") or []]

    def artifact_bytes(self, run_ref: RunRef, name: str) -> tuple[bytes, str]:
        if run_ref.project_id != self.project_id:
            raise SandboxForbidden("the run belongs to another project")
        raw, headers = self._request(
            "GET",
            f"/runs/{run_ref.run_id}/artifacts/{urllib.parse.quote(name)}",
            query={"project_id": self.project_id},
            timeout=120,
        )
        digest = headers.get("x-ravel-content-hash", "")
        if digest != hash_bytes(raw):
            raise SandboxIntegrityError(
                f"artifact {name!r} bytes do not match the supervisor's content hash"
            )
        return raw, digest

    # ── files ─────────────────────────────────────────────────────────────

    def files_list(self, handle: SandboxHandle, relative_path: str) -> list[FileEntry]:
        self._check_handle(handle)
        payload = self._json_request(
            "POST",
            f"/handles/{handle.handle_id}/files/list",
            body={
                "project_id": self.project_id,
                "authority": self._authority_dict(self._authority_for_handle(handle)),
                "path": relative_path,
            },
            timeout=60,
        )
        return [FileEntry(**item) for item in payload.get("entries") or []]

    def files_read(self, handle: SandboxHandle, relative_path: str) -> tuple[bytes, str]:
        self._check_handle(handle)
        payload = self._json_request(
            "POST",
            f"/handles/{handle.handle_id}/files/read",
            body={
                "project_id": self.project_id,
                "authority": self._authority_dict(self._authority_for_handle(handle)),
                "path": relative_path,
            },
            timeout=120,
        )
        data = base64.b64decode(str(payload["data_b64"]).encode("ascii"), validate=True)
        digest = hash_bytes(data)
        if digest != payload.get("content_hash"):
            raise SandboxIntegrityError(
                "the bytes the provider decoded do not match the stored content hash"
            )
        return data, digest

    def files_write(self, handle: SandboxHandle, relative_path: str, data: bytes) -> str:
        self._check_handle(handle)
        payload = self._json_request(
            "POST",
            f"/handles/{handle.handle_id}/files/write",
            body={
                "project_id": self.project_id,
                "authority": self._authority_dict(self._authority_for_handle(handle)),
                "path": relative_path,
                "data_b64": base64.b64encode(data).decode("ascii"),
            },
            timeout=120,
        )
        digest = str(payload.get("content_hash", ""))
        if digest != hash_bytes(data):
            raise SandboxIntegrityError(
                "the file hash the supervisor reports differs from the bytes sent"
            )
        return digest

    # ── browser ───────────────────────────────────────────────────────────

    def fetch(
        self,
        *,
        url: str,
        authority: Any,
        timeout_ms: int = 45_000,
        settle_ms: int = 10_000,
        max_bytes: int | None = None,
    ) -> BrowserPage:
        """Render a URL in this project's browser sandbox.

        The browser handle is provisioned on first use and reused. A stopped
        handle is reconnected; a destroyed or failed one is replaced by the
        supervisor's one-per-project rule on the next provision.
        """
        authority_payload = self._authority_dict(authority)
        handle = self._browser_handle
        if handle is not None and handle.state.value == "STOPPED":
            handle = self.reconnect(handle, authority)
            self._browser_handle = handle
        if handle is None:
            handle = self.provision(
                self.project_id, authority, purpose=SandboxPurpose.BROWSER
            )
            self._browser_handle = handle
        self._check_handle(handle)
        self._require_compliant(handle, browser=True)
        if handle.state.value != "READY":
            handle = self.reconnect(handle, authority)
            self._browser_handle = handle
        body: dict[str, Any] = {
            "project_id": self.project_id,
            "authority": authority_payload,
            "url": url,
            "timeout_ms": timeout_ms,
            "settle_ms": settle_ms,
        }
        if max_bytes is not None:
            body["max_bytes"] = max_bytes
        bound = timeout_ms / 1000.0 + settle_ms / 1000.0 + 180.0
        page = BrowserPage.from_dict(
            self._json_request(
                "POST",
                f"/handles/{handle.handle_id}/browser/fetch",
                body=body,
                timeout=bound,
                unsafe_url=url,
            )
        )
        actual = hashlib.sha256(page.html.encode("utf-8", errors="replace")).hexdigest()
        if page.content_hash != f"sha256:{actual}":
            raise SandboxIntegrityError(
                "the rendered page's hash does not match the bytes returned"
            )
        return page

    def close_browser(self, authority: Any | None = None) -> None:
        """Stop the browser sandbox; the project workspace volume is retained."""
        handle = self._browser_handle
        if handle is None:
            return
        try:
            self.stop(handle, authority=authority)
        except SandboxNotFound:
            pass
        finally:
            self._browser_handle = None

    def logs(self, handle: SandboxHandle) -> str:
        self._check_handle(handle)
        payload = self._json_request(
            "GET",
            f"/handles/{handle.handle_id}/logs",
            query={"project_id": self.project_id},
            timeout=30,
        )
        return str(payload.get("logs", ""))


def _unsafe_target(url: str, message: str) -> SandboxUnsafeTarget:
    """Rebuild the typed refusal from the supervisor's error text.

    The supervisor raised the typed error first and serialized its message. The
    prefix is stripped so constructing the client-side error does not nest it
    twice in the text a caller logs.
    """
    prefix = f"refusing to fetch {url}: "
    reason = message[len(prefix) :] if message.startswith(prefix) else message
    return SandboxUnsafeTarget(url, reason)


def _error_from_payload(payload: bytes) -> tuple[str, str]:
    """The `(code, message)` a supervisor error carries, with a safe fallback."""
    try:
        document = json.loads(payload.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return "", "the sandbox supervisor returned an error without a JSON body"
    if not isinstance(document, dict):
        return "", "the sandbox supervisor returned a non-object error"
    return str(document.get("code", "")), str(document.get("message", "sandbox request refused"))
