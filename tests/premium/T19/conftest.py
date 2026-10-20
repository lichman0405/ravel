"""Fixtures for the T19 sandbox conformance suite.

The suite runs the shipped supervisor as a real process against the real Docker
daemon, and it drives it through the shipped HTTP provider. That is deliberate:
the acceptance question is whether a real container has the isolation, quotas,
and egress behaviour the design claims, and an argument-string test would only
prove that the argv was spelled as intended.

Two fixture topologies are set up here:

- a fixture Docker network on the documentation range `203.0.113.0/24`, with a
  small HTTP server at `203.0.113.9:18008`. The server is the browser's
  allowlisted target. It is *not* attached to the browser's internal network:
  the only route to it is the supervisor's egress proxy, which is what the
  browser test is about.
- the supervisor process, with a random bearer token, a temporary state
  directory, the fixture network as its proxy egress network, and one secret
  injected through the environment allowlist so redaction can be observed.

If Docker or the pinned image is missing, the isolation cases fail with a
`BLOCKED_EXTERNAL` diagnosis naming the resource. They do not skip, because a
skipped isolation test is a gate that reports success without evidence.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest

# Re-exported on purpose: pytest registers an imported fixture under the name
# it has in this module, and the local helpers request them by those names.
from tests.integration.conftest import (  # noqa: F401
    _database_test_lock,
    clean,
    database,
    integration_settings,
    other_project,
    project,
)
from tests.premium.T19.support import (
    APP_IMAGE,
    FIXTURE_ADDRESS,
    FIXTURE_PORT,
    FIXTURE_SUBNET,
    REPO_ROOT,
    SANDBOX_IMAGE,
    SECRET_ENV_NAME,
    Deployment,
    docker_available,
)

from ravel.sandbox.docker_engine import DockerEngine

pytestmark = pytest.mark.integration

#: The fixture HTTP server. It serves one page with a script that fills the
#: document, so a rendered retrieval differs from the bytes the server sends.
_SERVER_CODE = r"""
import http.server
import socketserver

PAGE = (
    b"<!doctype html><html><head><title>T19 Sandbox Fixture</title></head>"
    b"<body><h1 id='heading'>raw-bytes-before-script</h1>"
    b"<a href='/other'>other target</a>"
    b"<script>document.getElementById('heading').textContent="
    b"'rendered-by-javascript';</script></body></html>"
)
OTHER = (
    b"<!doctype html><html><head><title>Other Fixture</title></head>"
    b"<body>a second allowlisted path</body></html>"
)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path in ("/", "/index.html"):
            body, status = PAGE, 200
        elif self.path == "/other":
            body, status = OTHER, 200
        else:
            body, status = b"not found", 404
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(("0.0.0.0", PORT), Handler) as httpd:
    httpd.serve_forever()
"""


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _log_tail(path: Path, lines: int = 40) -> str:
    try:
        content = path.read_text("utf-8", errors="replace").splitlines()
    except OSError:
        return "(no log)"
    return "\n".join(content[-lines:])


@pytest.fixture(scope="session")
def docker_engine() -> DockerEngine:
    """A real Docker daemon, or an explicit external blocker."""
    ok, detail = docker_available()
    if not ok:
        pytest.fail(
            "T19 isolation gate is BLOCKED_EXTERNAL(resource=Linux/Docker): "
            f"{detail}. The sandbox cases require a real Docker daemon; they are "
            "not reported as passing or skipped."
        )
    return DockerEngine()


@pytest.fixture(scope="session")
def sandbox_image(docker_engine: DockerEngine) -> str:
    """The pinned sandbox image, which must already exist on the daemon."""
    if not docker_engine.image_present(SANDBOX_IMAGE):
        pytest.fail(
            f"the sandbox image {SANDBOX_IMAGE!r} is not present in the Docker "
            "daemon; build it with `docker build -f infra/sandbox/Dockerfile.sandbox "
            f"-t {SANDBOX_IMAGE} .` before running T19"
        )
    return SANDBOX_IMAGE


@pytest.fixture(scope="session")
def app_image(docker_engine: DockerEngine) -> str:
    """The canonical application image, used for the no-socket proof."""
    if not docker_engine.image_present(APP_IMAGE):
        pytest.fail(
            f"the canonical application image {APP_IMAGE!r} is not present; T19's "
            "no-socket proof runs the repository's own application environment, "
            "not a substitute image"
        )
    return APP_IMAGE


@pytest.fixture(scope="session")
def app_code_image(
    docker_engine: DockerEngine,
    app_image: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """The canonical image with this checkout's source, for the no-socket proof.

    The source is built into the image rather than copied into a container:
    `docker cp` refuses a container whose rootfs is read-only, and dropping the
    read-only flag just to copy code would weaken the case. The base is the
    canonical application image, so the runtime is the repository's own.
    """
    tag = f"ravel-t19-app:{uuid4().hex[:12]}"
    context = tmp_path_factory.mktemp("t19-app-context")
    dockerfile = context / "Dockerfile"
    dockerfile.write_text(
        f"FROM {app_image}\nCOPY . /opt/t19-src\n", encoding="utf-8"
    )
    docker_engine.build(
        str(REPO_ROOT / "src"), str(dockerfile), tag, timeout=300
    )
    try:
        yield tag
    finally:
        docker_engine.run(["image", "rm", "--force", tag], timeout=60, check=False)


@pytest.fixture(scope="session")
def fixture_network(docker_engine: DockerEngine) -> Iterator[str]:
    """A non-internal network on the documentation range for the fixture server."""
    suffix = uuid4().hex[:8]
    name = f"ravel-t19-fixture-{suffix}"
    # Remove only containers left behind by a previous interrupted fixture run
    # in this namespace; project sandboxes and volumes are not touched.
    leftovers = docker_engine.run(
        ["ps", "-aq", "--filter", "label=ravel.t19.fixture=1"], check=False
    ).stdout.split()
    for container in leftovers:
        docker_engine.remove(container)
    docker_engine.network_create(name, internal=False, subnet=FIXTURE_SUBNET)
    try:
        yield name
    finally:
        docker_engine.network_remove(name)


@pytest.fixture(scope="session")
def fixture_http_server(
    docker_engine: DockerEngine,
    sandbox_image: str,
    fixture_network: str,
) -> Iterator[str]:
    """A real HTTP server at a fixed address, reachable only on its network."""
    name = f"ravel-t19-fixture-http-{uuid4().hex[:8]}"
    docker_engine.create(
        [
            "--name",
            name,
            "--label",
            "ravel.t19.fixture=1",
            "--network",
            fixture_network,
            "--ip",
            FIXTURE_ADDRESS,
            "--read-only",
            "--user",
            "65534:65534",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,nodev,size=16m,mode=1777",
            "--env",
            "HOME=/tmp",
            "--entrypoint",
            "python3",
            sandbox_image,
            "-c",
            _SERVER_CODE.replace("PORT", str(FIXTURE_PORT)),
        ]
    )
    docker_engine.start(name)
    try:
        deadline = time.monotonic() + 30
        last = ""
        while time.monotonic() < deadline:
            result = docker_engine.exec_capture(
                name,
                [
                    "python3",
                    "-c",
                    (
                        "import urllib.request;"
                        f"r=urllib.request.urlopen('http://127.0.0.1:{FIXTURE_PORT}/',"
                        "timeout=1);print(r.status)"
                    ),
                ],
                timeout=10,
            )
            if result.returncode == 0:
                break
            last = result.stderr[-500:]
            time.sleep(0.2)
        else:
            raise AssertionError(f"the fixture HTTP server did not start: {last}")
        yield name
    finally:
        docker_engine.remove(name)


@pytest.fixture(scope="session")
def deployment(
    docker_engine: DockerEngine,
    sandbox_image: str,
    fixture_http_server: str,
    fixture_network: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Deployment]:
    """The shipped supervisor, running against the real daemon."""
    state_dir = tmp_path_factory.mktemp("t19-supervisor-state")
    token = f"t19-{uuid4().hex}{uuid4().hex}"
    env_secret = f"t19-secret-{uuid4().hex}"
    port = _free_port()
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(REPO_ROOT / "src"),
            "RAVEL_SANDBOX_SUPERVISOR_TOKEN": token,
            "RAVEL_SANDBOX_IMAGE": sandbox_image,
            "RAVEL_SANDBOX_STATE_DIR": str(state_dir),
            "RAVEL_SANDBOX_SUPERVISOR_BIND": "127.0.0.1",
            "RAVEL_SANDBOX_PROXY_ALLOWLIST": f"{FIXTURE_ADDRESS}:{FIXTURE_PORT}",
            "RAVEL_SANDBOX_PROXY_EGRESS_NETWORKS": fixture_network,
            "RAVEL_SANDBOX_ENV_ALLOWLIST": f"{SECRET_ENV_NAME}={env_secret}",
            "RAVEL_SANDBOX_BROWSER_STARTUP_SECONDS": "60",
            "RAVEL_SANDBOX_STOP_GRACE_SECONDS": "2",
            "RAVEL_SANDBOX_SUPERVISOR_LOG_LEVEL": "info",
        }
    )
    log_path = state_dir / "supervisor.log"
    log_handle = log_path.open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "ravel.sandbox.service",
            "--bind",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=REPO_ROOT,
        env=environment,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    log_handle.close()
    base_url = f"http://127.0.0.1:{port}"
    try:
        # The supervisor imports the sandbox package and talks to the daemon
        # before it listens. On a mounted filesystem that import can take tens
        # of seconds on a cold cache, so readiness is allowed a generous window;
        # this wait is not an isolation assertion.
        deadline = time.monotonic() + 180
        last_problem = "no health response was received"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                pytest.fail(
                    "the sandbox supervisor exited during startup "
                    f"({process.returncode}):\n{_log_tail(log_path)}"
                )
            try:
                # `/healthz` asks Docker three questions (daemon, version,
                # image). On a busy host the three CLI round trips can take a
                # few seconds; a one-second client timeout turns readiness into
                # a race with the daemon, not a statement about isolation.
                with urllib.request.urlopen(f"{base_url}/healthz", timeout=15) as response:
                    health = json.loads(response.read())
            except Exception as exc:
                last_problem = f"{base_url}/healthz failed: {type(exc).__name__}: {exc}"
                time.sleep(0.1)
                continue
            last_problem = f"{base_url}/healthz answered {health!r}"
            if health.get("ok") and health.get("image_present"):
                break
            time.sleep(0.1)
        else:
            pytest.fail(
                "the sandbox supervisor did not become healthy "
                f"({last_problem}):\n{_log_tail(log_path)}"
            )
        yield Deployment(
            base_url=base_url,
            token=token,
            state_dir=state_dir,
            env_secret=env_secret,
        )
    finally:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=10)
        _remove_sandbox_records(docker_engine, state_dir)


def _remove_sandbox_records(engine: DockerEngine, state_dir: Path) -> None:
    """Remove every container, proxy, network, and volume the test supervisor made.

    The supervisor's durable handle records are the inventory: removing by a
    label would touch another run's sandboxes, and leaving them behind would
    make a later run's assertions depend on this one's leftovers.
    """
    handles = state_dir / "handles"
    for path in sorted(handles.glob("*.json")) if handles.exists() else []:
        try:
            record = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        for key in ("container", "proxy"):
            name = record.get(key)
            if name:
                engine.remove(name)
        network = record.get("network")
        if network:
            engine.network_remove(network)
        volume = record.get("volume")
        if volume:
            engine.volume_remove(volume)


@pytest.fixture(scope="session")
def fixture_url() -> str:
    return f"http://{FIXTURE_ADDRESS}:{FIXTURE_PORT}/"
