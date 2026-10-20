"""T19-AC1: a real container is the boundary, and a real app container has no socket.

Every assertion here is read from a container that Docker actually started, not
from the argument list that was meant to start it. The application-container
case runs the repository's canonical image with its Docker CLI present and no
socket, and drives the supervisor over the host network: it proves that the
agent-side path works without granting the agent Docker.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any
from uuid import uuid4

import pytest
from tests.premium.T19.support import (
    REPO_ROOT,
    SECRET_ENV_NAME,
    Deployment,
    execute_command,
    handle_record,
    python_argv,
    read_json_line,
)

from ravel.sandbox.docker_engine import DockerEngine
from ravel.sandbox.models import (
    SandboxError,
    SandboxForbidden,
    SandboxValidationError,
)

pytestmark = pytest.mark.integration

GIB = 1024**3


def _raw_request(
    deployment: Deployment,
    method: str,
    path: str,
    *,
    project_id: str,
    body: dict[str, Any] | None = None,
    token: str | None = None,
    send_project_header: bool = True,
) -> tuple[int, bytes]:
    headers = {
        "Authorization": f"Bearer {deployment.token if token is None else token}",
        "Accept": "application/json",
    }
    if send_project_header:
        headers["X-RAVEL-Project"] = project_id
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{deployment.base_url}{path}", data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _container_of(deployment: Deployment, handle: Any) -> str:
    return str(handle_record(deployment, handle.handle_id)["container"])


def test_command_container_is_actually_isolated(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """The flags are on the running container, read back from `docker inspect`."""
    project_id = deployment.project_id("ac1-isolation")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    container = _container_of(deployment, handle)
    document = docker_engine.inspect(container)
    config = document["Config"]
    host = document["HostConfig"]

    assert host["ReadonlyRootfs"] is True
    assert config["User"] == "65534:65534"
    assert host["Privileged"] is False
    assert host["CapDrop"] == ["ALL"]
    assert host["PidMode"] in ("", None)
    assert host["IpcMode"] != "host"
    security = " ".join(host.get("SecurityOpt") or [])
    assert "no-new-privileges" in security
    assert "seccomp=unconfined" not in security, security
    # Docker's built-in default profile is not repeated in SecurityOpt, so the
    # assertion is read from the kernel state of a process Docker actually
    # started: mode 2 is SECCOMP_MODE_FILTER. `grep` is run inside the
    # container, not in the test process.
    status = docker_engine.exec_capture(
        container,
        ["grep", "-E", "^(Seccomp|NoNewPrivs):", "/proc/self/status"],
        timeout=10,
    )
    assert status.returncode == 0, status.stderr[-1000:]
    kernel = dict(line.split(":", 1) for line in status.stdout.splitlines())
    assert kernel["Seccomp"].strip() == "2", status.stdout
    assert kernel["NoNewPrivs"].strip() == "1", status.stdout
    assert host["NanoCpus"] == 2_000_000_000
    assert host["Memory"] == 4 * GIB
    assert host["MemorySwap"] == 4 * GIB
    assert host["PidsLimit"] == 256
    assert host["NetworkMode"] == "none"

    tmpfs = host.get("Tmpfs") or {}
    assert "size=10737418240" in tmpfs["/tmp"]
    assert "noexec" in tmpfs["/tmp"]

    mounts = document.get("Mounts") or []
    assert len(mounts) == 1
    mount = mounts[0]
    assert mount["Type"] == "volume"
    assert mount["Destination"] == "/workspace"
    assert mount["RW"] is True
    assert mount["Name"].startswith("ravel-t19-ws-")
    assert all("/var/run/docker.sock" not in (m.get("Source") or "") for m in mounts)
    assert all(m["Type"] != "bind" for m in mounts)

    environment = config.get("Env") or []
    joined = "\n".join(environment)
    assert deployment.token not in joined
    assert deployment.env_secret not in joined
    assert not any(name.startswith("RAVEL_POSTGRES") for name in environment)

    record = handle_record(deployment, handle.handle_id)
    limits = record["limits"]
    assert limits["cpu_cores"] == 2.0
    assert limits["memory_bytes"] == 4 * GIB
    assert limits["pids"] == 256
    assert limits["scratch_bytes"] == 10 * GIB
    assert limits["timeout_seconds"] == 120.0
    assert record["binding_id"] == authority.binding_id


def test_command_container_cannot_reach_host_secrets_or_any_network(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """The host's socket, credentials, metadata, and database are unreachable."""
    project_id = deployment.project_id("ac1-reach")
    authority = deployment.authority(project_id)
    handle = deployment.provider(project_id).provision(project_id, authority)
    code = r"""
import json, os, socket

env = dict(os.environ)
init_env = ""
try:
    raw = open("/proc/1/environ", "rb").read()
    init_env = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
except OSError as exc:
    init_env = f"unreadable: {exc.__class__.__name__}"

routes = open("/proc/net/route").read().splitlines()[1:]

targets = {
    "metadata": ("169.254.169.254", 80),
    "host_gateway": ("172.17.0.1", 5432),
    "loopback_db": ("127.0.0.1", 55432),
}
results = {}
for name, (host, port) in targets.items():
    try:
        s = socket.create_connection((host, port), timeout=1)
        s.close()
        results[name] = "connected"
    except OSError as exc:
        results[name] = exc.__class__.__name__

try:
    socket.getaddrinfo("postgres", 5432, proto=socket.IPPROTO_TCP)
    dns = "resolved"
except OSError as exc:
    dns = exc.__class__.__name__

print(json.dumps({
    "socket_present": os.path.exists("/var/run/docker.sock"),
    "env": env,
    "init_env": init_env,
    "routes": routes,
    "targets": results,
    "dns": dns,
    "workspace": os.listdir("/workspace"),
    "host_path": os.path.exists("/mnt/c") or os.path.exists("/Users"),
    "supervisor_state": os.path.exists("/var/lib/ravel-sandbox"),
    "workspace_mounts": [
        line for line in open("/proc/mounts").read().splitlines() if "/workspace" in line
    ],
}))
"""
    provider, run = execute_command(
        deployment,
        handle,
        authority,
        python_argv(code),
        timeout=30,
    )
    status = provider.status(run)
    assert status.state.value == "SUCCEEDED", status.detail
    payload = read_json_line(status.stdout)

    assert payload["socket_present"] is False
    assert payload["host_path"] is False
    assert payload["supervisor_state"] is False
    assert payload["routes"] == []
    for name in ("metadata", "host_gateway", "loopback_db"):
        assert payload["targets"][name] in (
            "NetworkUnreachable",
            "OSError",
            "ConnectionRefusedError",
            "TimeoutError",
        ), payload
        assert payload["targets"][name] != "connected"
    assert payload["dns"] != "resolved"
    assert deployment.token not in json.dumps(payload)
    assert deployment.env_secret not in json.dumps(payload)
    assert SECRET_ENV_NAME not in payload["env"]
    assert deployment.token not in payload["init_env"]
    assert payload["workspace"] == []

    # The only writable filesystem is /tmp and the project workspace; the
    # workspace line is a volume mount, not a host bind.
    assert any(" /workspace " in f" {line} " for line in payload["workspace_mounts"])
    assert not any("/mnt/c" in line or "/Users" in line for line in payload["workspace_mounts"])


def test_cross_project_files_and_handles_do_not_leak(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """A second project's sandbox sees its own empty volume, not the first's."""
    project_a = deployment.project_id("ac1-a")
    project_b = deployment.project_id("ac1-b")
    authority_a = deployment.authority(project_a, binding="binding-a")
    authority_b = deployment.authority(project_b, binding="binding-b")
    provider_a = deployment.provider(project_a)
    provider_b = deployment.provider(project_b)
    handle_a = provider_a.provision(project_a, authority_a)
    handle_b = provider_b.provision(project_b, authority_b)

    digest = provider_a.files_write(handle_a, "private.txt", b"project-a-only")
    assert digest.startswith("sha256:")
    assert provider_a.files_read(handle_a, "private.txt")[0] == b"project-a-only"

    entries_b = provider_b.files_list(handle_b, "")
    assert "private.txt" not in {entry.name for entry in entries_b}
    with pytest.raises(SandboxError):
        provider_b.files_read(handle_b, "private.txt")

    # The client refuses another project's handle before sending anything.
    with pytest.raises(SandboxForbidden):
        provider_b.stop(handle_a)

    # The supervisor refuses it too, even when the client is bypassed: the
    # handle is hidden behind the project named in the request.
    code, body = _raw_request(
        deployment,
        "GET",
        f"/handles/{handle_a.handle_id}?project_id={project_b}",
        project_id=project_b,
    )
    assert code == 404, body
    code, body = _raw_request(
        deployment,
        "POST",
        f"/handles/{handle_a.handle_id}/stop",
        project_id=project_b,
        body={
            "project_id": project_b,
            "authority": {
                "project_id": project_b,
                "binding_id": "binding-b",
                "agent_role": "EXPERIMENTAL_WORKER",
                "lease_epoch": None,
                "envelope": None,
            },
        },
    )
    assert code == 404, body

    # A missing or mismatched project header is refused at the boundary.
    code, _ = _raw_request(
        deployment,
        "GET",
        f"/handles/{handle_a.handle_id}?project_id={project_a}",
        project_id=project_a,
        send_project_header=False,
    )
    assert code == 403
    code, _ = _raw_request(
        deployment,
        "GET",
        f"/handles/{handle_a.handle_id}?project_id={project_a}",
        project_id=project_b,
    )
    assert code == 403

    # And the owner can still use it.
    assert provider_a.files_read(handle_a, "private.txt")[0] == b"project-a-only"


def test_paths_that_escape_the_workspace_are_refused(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """Lexical escapes, symlinks, and hard links are all refused."""
    project_id = deployment.project_id("ac1-paths")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)

    with pytest.raises(SandboxValidationError):
        provider.files_read(handle, "../outside.txt")
    with pytest.raises(SandboxValidationError):
        provider.files_read(handle, "/etc/passwd")

    provider.files_write(handle, "regular.txt", b"regular")
    code = r"""
import os
os.makedirs("/workspace/links", exist_ok=True)
os.symlink("/etc/passwd", "/workspace/links/escape")
os.link("/workspace/regular.txt", "/workspace/links/hard")
"""
    execution_provider, run = execute_command(
        deployment,
        handle,
        authority,
        python_argv(code),
        timeout=30,
    )
    assert execution_provider.status(run).state.value == "SUCCEEDED"

    with pytest.raises(SandboxForbidden, match="symbolic link"):
        provider.files_read(handle, "links/escape")
    with pytest.raises(SandboxForbidden, match="hard link"):
        provider.files_read(handle, "links/hard")
    entries = {entry.name: entry.kind for entry in provider.files_list(handle, "links")}
    assert entries == {"escape": "symlink", "hard": "file"}


def test_application_container_has_no_docker_socket_and_uses_the_supervisor(
    deployment: Deployment,
    docker_engine: DockerEngine,
    app_code_image: str,
) -> None:
    """The repository's own image can drive a sandbox without holding Docker.

    The application image is the canonical image plus this checkout's source,
    built once and run with no mounts, a read-only root, and a non-root user.
    It provisions and runs a real sandbox over the host network. There is no
    socket in its mount table and `docker version` cannot reach a daemon.
    """
    project_id = deployment.project_id("ac1-app")
    name = f"ravel-t19-app-{uuid4().hex[:8]}"
    docker_engine.create(
        [
            "--name",
            name,
            "--network",
            "host",
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777",
            "--env",
            "HOME=/tmp",
            "--env",
            "PYTHONDONTWRITEBYTECODE=1",
            "--entrypoint",
            "python3",
            app_code_image,
            "-c",
            "import time; time.sleep(900)",
        ]
    )
    try:
        docker_engine.start(name)
        document = docker_engine.inspect(name)
        assert document["HostConfig"]["ReadonlyRootfs"] is True
        assert document["Config"]["User"] == "65534:65534"
        assert document.get("Mounts") == []
        assert "/var/run/docker.sock" not in json.dumps(document.get("Mounts") or [])

        script = r"""
import json, os, shutil, subprocess, sys
sys.path.insert(0, "/opt/t19-src")

from ravel.sandbox.models import command_digest
from ravel.sandbox.provider import DockerSandboxProvider

project_id = os.environ["T19_APP_PROJECT"]
base_url = os.environ["T19_APP_SUPERVISOR_URL"]
token = os.environ["T19_APP_TOKEN"]
authority = {
    "project_id": project_id,
    "binding_id": "app-binding",
    "agent_role": "EXPERIMENTAL_WORKER",
    "lease_epoch": None,
    "envelope": None,
}
argv = ["/bin/echo", "from-app-container"]
expected = command_digest(argv=argv, cwd="", env_refs=())

class Guard:
    def verify(self, *, action_id, project_id, command_digest, authority):
        if action_id != "app-action":
            raise PermissionError("unexpected action")
        if command_digest != expected:
            raise PermissionError("digest mismatch")

    def record_outcome(self, *, action_id, run, authority):
        return None

docker_cli = shutil.which("docker")
docker_version = None
if docker_cli:
    probe = subprocess.run(
        [docker_cli, "version"], capture_output=True, text=True, timeout=20
    )
    docker_version = {
        "returncode": probe.returncode,
        "output": ((probe.stderr or "") + (probe.stdout or ""))[-300:],
    }

provider = DockerSandboxProvider(
    base_url=base_url,
    token=token,
    project_id=project_id,
    action_guard=Guard(),
)
handle = provider.provision(project_id, authority)
run = provider.execute(
    handle,
    argv,
    timeout=30,
    action_id="app-action",
    authority=authority,
)
status = provider.status(run)
print(json.dumps({
    "socket_present": os.path.exists("/var/run/docker.sock"),
    "docker": docker_version,
    "run_state": status.state.value,
    "stdout": status.stdout,
    "handle_project": handle.project_id,
    "host_path": os.path.exists("/mnt/c") or os.path.exists("/Users"),
    "mounts": open("/proc/mounts").read().splitlines(),
}))
"""
        result = docker_engine.exec_capture(
            name,
            ["python3", "-c", script],
            timeout=180,
            env={
                "T19_APP_PROJECT": project_id,
                "T19_APP_SUPERVISOR_URL": deployment.base_url,
                "T19_APP_TOKEN": deployment.token,
            },
        )
        assert result.returncode == 0, result.stderr
        payload = read_json_line(result.stdout)
        assert payload["socket_present"] is False
        assert payload["docker"] is not None
        assert payload["docker"]["returncode"] != 0
        assert "Cannot connect" in payload["docker"]["output"] or (
            "permission denied" in payload["docker"]["output"].lower()
        ) or "docker.sock" in payload["docker"]["output"]
        assert payload["run_state"] == "SUCCEEDED"
        assert "from-app-container" in payload["stdout"]
        assert payload["handle_project"] == project_id
        assert payload["host_path"] is False
        assert not any("/var/run/docker.sock" in line for line in payload["mounts"])
    finally:
        docker_engine.remove(name)


def test_only_the_supervisor_compose_service_mounts_the_socket() -> None:
    """The repository's compose files put the socket on one service only."""
    root = (REPO_ROOT / "docker-compose.yml").read_text("utf-8")
    overlay = (REPO_ROOT / "infra" / "sandbox" / "docker-compose.sandbox.yml").read_text(
        "utf-8"
    )
    assert "/var/run/docker.sock" not in root

    socket_lines = [
        line.strip()
        for line in overlay.splitlines()
        if line.strip().startswith("- /var/run/docker.sock:")
    ]
    assert socket_lines == ["- /var/run/docker.sock:/var/run/docker.sock"]

    services = [
        line.strip().rstrip(":")
        for line in overlay.splitlines()
        if line.startswith("  ") and not line.startswith("    ") and line.strip().endswith(":")
    ]
    assert "sandbox-supervisor" in services
    assert "127.0.0.1:" in overlay
    assert "privileged: true" not in overlay
    assert "network_mode: host" not in overlay
