"""Small builders shared by the T19 conformance tests.

The tests in this directory run real Docker containers through the real
supervisor and, where the assertion is about authoritative state, a real
PostgreSQL. Nothing here fakes a container result: the helpers start the
shipped supervisor, construct the shipped HTTP provider, and read the container
Docker actually created. A Docker argument string is never treated as evidence
that the container has that property.
"""

from __future__ import annotations

import io
import json
import shutil
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from ravel.sandbox.action_guard import StaticActionGuard
from ravel.sandbox.authority import SandboxAuthority
from ravel.sandbox.models import RunRef, RunStatus
from ravel.sandbox.provider import DockerSandboxProvider
from ravel.state.store import StoredObject, hash_chunks

#: The test tree's repository root, as the host running the tests sees it.
REPO_ROOT = Path(__file__).resolve().parents[3]

#: The sandbox image the tests exercise. The tag is the deployment default;
#: the test fixture asserts the daemon has it rather than silently pulling a
#: different one.
SANDBOX_IMAGE = "ravel-sandbox:latest"

#: The canonical RAVEL application image. It is used to prove that an app
#: container with the repository's own environment and the supervisor's URL
#: still has no Docker socket.
APP_IMAGE = "ravel-premium-ubuntu24:latest"

#: A documentation-range address and port for the browser fixture. The
#: address is public by RAVEL's policy (documentation ranges are not blocked)
#: and routable only on the fixture Docker network, so the browser can reach it
#: through the proxy but a command sandbox has no route to it at all.
FIXTURE_ADDRESS = "203.0.113.9"
FIXTURE_PORT = 18008
FIXTURE_URL = f"http://{FIXTURE_ADDRESS}:{FIXTURE_PORT}/"
FIXTURE_SUBNET = "203.0.113.0/24"

#: Environment name whose value is injected into sandbox commands by the
#: supervisor's allowlist. It is a test secret: it must never appear in a run
#: record, output, or log.
SECRET_ENV_NAME = "T19_TEST_SECRET"


@dataclass(frozen=True, slots=True)
class Deployment:
    """A running supervisor and the coordinates a RAVEL process uses."""

    base_url: str
    token: str
    state_dir: Path
    env_secret: str

    def provider(
        self,
        project_id: str,
        *,
        guard: Any | None = None,
    ) -> DockerSandboxProvider:
        return DockerSandboxProvider(
            base_url=self.base_url,
            token=self.token,
            project_id=project_id,
            action_guard=guard,
        )

    def authority(
        self,
        project_id: str,
        *,
        role: str = "EXPERIMENTAL_WORKER",
        binding: str = "t19-binding-1",
        epoch: int | None = None,
        envelope: Any | None = None,
    ) -> SandboxAuthority:
        return SandboxAuthority(
            project_id=project_id,
            binding_id=binding,
            agent_role=role,
            lease_epoch=epoch,
            envelope=envelope,
        )

    def project_id(self, label: str = "project") -> str:
        return f"t19-{label}-{uuid4().hex}"


class MemoryArtifactStore:
    """An `ArtifactStore` that keeps bytes in memory.

    The property under test is the supervisor's artifact path and the Gateway's
    hash comparison, neither of which is a property of MinIO. Keeping the bytes
    here lets a case compare what was registered with what the sandbox wrote,
    and leaves the object-store boundary to its own suite.
    """

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.media: dict[str, str] = {}

    def put(
        self, key: str, chunks: Iterable[bytes], *, media_type: str = ""
    ) -> StoredObject:
        pieces = list(chunks)
        body = b"".join(pieces)
        digest, size = hash_chunks(pieces)
        self.objects[key] = body
        self.media[key] = media_type
        return StoredObject(key=key, content_hash=digest, size_bytes=size)

    def get(self, key: str) -> bytes:
        return self.objects[key]

    def open(self, key: str) -> io.BytesIO:
        return io.BytesIO(self.objects[key])

    def exists(self, key: str) -> bool:
        return key in self.objects

    def verify(self, key: str, expected_hash: str) -> bool:
        body = self.objects.get(key)
        if body is None:
            return False
        return hash_chunks([body])[0] == expected_hash

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)
        self.media.pop(key, None)


def execute_command(
    deployment: Deployment,
    handle: Any,
    authority: SandboxAuthority,
    argv: list[str] | tuple[str, ...],
    *,
    cwd: str = "",
    env_refs: list[str] | tuple[str, ...] = (),
    timeout: float = 120.0,
) -> tuple[DockerSandboxProvider, RunRef]:
    """Run one command under a digest-bound test grant.

    The grant is a real `StaticActionGuard`, not a permission to skip the check:
    it holds the digest of exactly this argv, and the provider and supervisor
    both verify it. Tests that need the production guard use
    `DatabaseActionGuard` themselves.
    """
    action_id = f"act_{uuid4().hex}"
    command = tuple(argv)
    guard = StaticActionGuard.granting(
        action_id=action_id,
        project_id=authority.project_id,
        argv=command,
        cwd=cwd,
        env_refs=env_refs,
    )
    provider = deployment.provider(authority.project_id, guard=guard)
    run = provider.execute(
        handle,
        command,
        cwd=cwd,
        env_refs=env_refs,
        timeout=timeout,
        action_id=action_id,
        authority=authority,
    )
    return provider, run


def run_status(
    deployment: Deployment,
    run: RunRef,
    *,
    authority: SandboxAuthority | None = None,
) -> RunStatus:
    provider = deployment.provider(
        run.project_id,
        guard=None,
    )
    if authority is not None:
        # The provider's status call only uses the run's project; keeping the
        # argument makes the binding explicit at the call site.
        assert authority.project_id == run.project_id
    return provider.status(run)


def wait_until(
    predicate: Callable[[], bool],
    *,
    timeout: float = 20.0,
    interval: float = 0.1,
    message: str = "condition was not met before the deadline",
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError(message)


def handle_record(deployment: Deployment, handle_id: str) -> dict[str, Any]:
    """The supervisor's durable record for one handle, as JSON."""
    for path in sorted((deployment.state_dir / "handles").glob("*.json")):
        payload = json.loads(path.read_text("utf-8"))
        if payload.get("handle", {}).get("handle_id") == handle_id:
            return payload
    raise AssertionError(f"no supervisor handle record for {handle_id}")


def run_record(deployment: Deployment, run_id: str) -> dict[str, Any]:
    """The supervisor's durable record for one run, as JSON."""
    path = deployment.state_dir / "runs" / f"{run_id}.json"
    if not path.exists():
        raise AssertionError(f"no supervisor run record for {run_id}")
    return json.loads(path.read_text("utf-8"))


def run_records(deployment: Deployment) -> list[dict[str, Any]]:
    """Every run record the supervisor has written, in no particular order."""
    directory = deployment.state_dir / "runs"
    if not directory.exists():
        return []
    return [
        json.loads(path.read_text("utf-8")) for path in sorted(directory.glob("*.json"))
    ]


def run_records_for_handle(deployment: Deployment, handle_id: str) -> list[dict[str, Any]]:
    return [
        record
        for record in run_records(deployment)
        if record.get("handle_id") == handle_id
    ]


def container_of(deployment: Deployment, handle: Any) -> str:
    """The Docker container name behind a handle, from the durable record."""
    return str(handle_record(deployment, handle.handle_id)["container"])


def docker_available() -> tuple[bool, str]:
    """Whether a Docker CLI and daemon are usable, with a diagnosis."""
    binary = shutil.which("docker")
    if binary is None:
        return False, "the Docker CLI is not on PATH"
    import subprocess

    completed = subprocess.run(
        [binary, "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip().splitlines()
        return False, f"the Docker daemon did not answer: {detail[-1] if detail else 'no detail'}"
    return True, completed.stdout.strip()


def container_process_args(engine: Any, container: str) -> list[str]:
    """Every process argument line in a container, read from `/proc`."""
    script = (
        "import os,sys\n"
        "out=[]\n"
        "for pid in os.listdir('/proc'):\n"
        "    if not pid.isdigit():\n"
        "        continue\n"
        "    try:\n"
        "        raw=open('/proc/'+pid+'/cmdline','rb').read()\n"
        "    except OSError:\n"
        "        continue\n"
        "    out.append(raw.replace(b'\\x00',b' ').decode('utf-8','replace'))\n"
        "print('\\n'.join(out))\n"
    )
    result = engine.exec_capture(
        container, ["python3", "-c", script], timeout=15
    )
    return [line for line in result.stdout.splitlines() if line]


def sleep_seconds(seconds: float) -> list[str]:
    """An argv that sleeps, for timeout and cancellation cases."""
    return ["/bin/sleep", f"{seconds:g}"]


def python_argv(code: str) -> list[str]:
    return ["python3", "-c", code]


def read_json_line(text: str) -> dict[str, Any]:
    """The last JSON object a helper printed, ignoring daemon chatter."""
    for line in reversed([line for line in text.splitlines() if line.strip()]):
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return payload
    raise AssertionError(f"no JSON object in helper output: {text[-2000:]!r}")
