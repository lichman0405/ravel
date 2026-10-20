"""Real Gateway + real PostgreSQL + the desktop's built main-process service.

The Node child is not a stand-in for the desktop: it constructs the same
`GatewayService` and `SessionStore` classes the Electron main process uses and
speaks to a real uvicorn Gateway over a socket. What cannot be real here is the
OS keychain/DPAPI and the Electron window; those are exercised on Windows by
`tests/premium/T14/electron/run-electron.cjs` and recorded as manual evidence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from tests.integration.conftest import (  # noqa: F401
    _database_test_lock,
    clean,
    database,
    integration_settings,
    project,
)
from tests.integration.gateway.conftest import (  # noqa: F401
    PASSWORD,
    SECRET,
    TTL_SECONDS,
    ScriptedDeliveries,
    account,
    app,
    deliveries,
    gateway_settings,
    tokens,
)

from ravel.domain.enums import UserRole

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolated(clean: None) -> None:  # noqa: F811
    """Every database case starts from an empty database."""


@pytest.fixture
def desktop_account(database, project):  # noqa: F811
    """A real account that owns a real project and can sign in.

    The `project` fixture's owner cannot authenticate (its hash is a literal
    placeholder), so this account is the one the desktop signs in as.
    """
    account(
        database,
        username="desktop-owner",
        password=PASSWORD,
        role=UserRole.PROJECT_OWNER,
        project=project,
    )
    return {
        "username": "desktop-owner",
        "password": PASSWORD,
        "project_id": project.project_id,
        "project_title": project.title,
        "display_id": project.display_id,
    }

REPO_ROOT = Path(__file__).resolve().parents[3]
HARNESS = REPO_ROOT / "tests" / "premium" / "T14" / "node" / "gateway-harness.cjs"
DIST = REPO_ROOT / "apps" / "desktop" / "dist" / "lib" / "gateway-service.cjs"


class LiveGateway:
    """The real application on a real TCP socket, with a real lifespan."""

    def __init__(self, application: Any) -> None:
        self.app = application
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.base_url = ""

    def start(self) -> str:
        config = uvicorn.Config(
            self.app,
            host="127.0.0.1",
            port=0,
            log_level="warning",
            lifespan="on",
            access_log=False,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, name="t14-gateway", daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 20
        while not self.server.started:
            if not self.thread.is_alive():
                raise RuntimeError("the Gateway thread exited before it started")
            if time.monotonic() > deadline:
                raise RuntimeError("the Gateway did not start within 20 seconds")
            time.sleep(0.01)
        assert self.server.servers, "uvicorn has no bound server"
        port = self.server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self.base_url

    def stop(self) -> None:
        if self.server is None:
            return
        self.server.should_exit = True
        assert self.thread is not None
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            raise RuntimeError("the Gateway did not stop")
        self.server = None
        self.thread = None


@pytest.fixture
def live_gateway(app) -> Iterator[LiveGateway]:  # noqa: F811
    gateway = LiveGateway(app)
    try:
        yield gateway
    finally:
        gateway.stop()


def node_binary() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is required to run the built desktop service")
    if not DIST.is_file():
        pytest.fail("build the desktop first: pnpm --dir apps/desktop build")
    return node


def run_phase(
    gateway: LiveGateway,
    desktop_account: dict[str, Any],
    session_dir: Path,
    phase: str,
    *,
    base_url: str | None = None,
    username: str | None = None,
    password: str | None = None,
) -> dict[str, Any]:
    argv = [
        node_binary(),
        str(HARNESS),
        "--phase",
        phase,
        "--base-url",
        base_url or gateway.base_url,
        "--username",
        username or desktop_account["username"],
        "--password",
        password or desktop_account["password"],
        "--session-dir",
        str(session_dir),
    ]
    if phase != "scope":
        argv.extend(["--project", desktop_account["project_title"]])
    result = subprocess.run(
        argv,
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, (
        f"desktop harness {phase} failed (exit {result.returncode}): "
        f"{result.stderr.strip() or result.stdout.strip()}"
    )
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    assert lines, "desktop harness printed nothing"
    payload = json.loads(lines[-1])
    rendered = json.dumps(payload)
    assert "access_token" not in rendered, "an access token field reached the output"
    assert "refresh_token" not in rendered, "a refresh token field reached the output"
    assert "eyJ" not in rendered, "something shaped like a JWT reached the output"
    return payload


def login_http(base_url: str, username: str, password: str) -> httpx.Response:
    return httpx.post(
        f"{base_url}/v1/auth/login",
        json={"username": username, "password": password},
        timeout=10,
    )


def test_the_desktop_service_signs_in_and_lists_only_a_real_owned_project(
    live_gateway: LiveGateway,
    desktop_account: dict[str, Any],
    tmp_path: Path,
) -> None:
    live_gateway.start()
    session_dir = tmp_path / "session"

    result = run_phase(live_gateway, desktop_account, session_dir, "login")

    assert result["wrong_password"] == "INVALID_CREDENTIALS"
    assert result["authenticated"] is True
    assert result["user"] == {"username": desktop_account["username"]}
    assert result["gateway_origin"] == live_gateway.base_url
    assert result["project"] == {
        "display_id": desktop_account["display_id"],
        "title": desktop_account["project_title"],
        "status": "CREATED",
        "role": "PROJECT_OWNER",
    }
    assert result["project_count"] == 1
    assert result["projection_keys"] == [
        "authenticated",
        "configurationRequired",
        "gatewayOrigin",
        "user",
    ]
    assert result["session_file"]["envelope_keys"] == ["ciphertext", "format", "version"]
    assert result["session_file"]["contains_plaintext_token"] is False
    assert result["session_file"]["size"] > 0

    # The desktop process has exited; the Gateway it was reading from is still
    # serving. Closing a desktop is not stopping the cloud Supervisor.
    response = login_http(
        live_gateway.base_url,
        desktop_account["username"],
        desktop_account["password"],
    )
    assert response.status_code == 200
    assert "access_token" in response.json()


def test_membership_scoping_is_the_gateways_and_the_desktop_cannot_widen_it(
    live_gateway: LiveGateway,
    database,  # noqa: F811
    desktop_account: dict[str, Any],
    tmp_path: Path,
) -> None:
    account(database, username="unrelated-person", password=PASSWORD)
    live_gateway.start()

    result = run_phase(
        live_gateway,
        desktop_account,
        tmp_path / "stranger-session",
        "scope",
        username="unrelated-person",
    )

    assert result["authenticated"] is True
    assert result["project_count"] == 0
    assert result["titles"] == []


def test_offline_restore_keeps_the_session_then_reconnects_and_logout_revokes(
    live_gateway: LiveGateway,
    desktop_account: dict[str, Any],
    tmp_path: Path,
) -> None:
    live_gateway.start()
    session_dir = tmp_path / "restart-session"
    login = run_phase(live_gateway, desktop_account, session_dir, "login")
    assert login["session_file"]["exists"] is True

    old_base_url = live_gateway.base_url
    live_gateway.stop()

    offline = run_phase(
        live_gateway,
        desktop_account,
        session_dir,
        "offline",
        base_url=old_base_url,
    )
    assert offline["restored_authenticated"] is True
    assert offline["status"] == "OFFLINE"
    assert offline["project_read"] == "GATEWAY_OFFLINE"
    assert offline["reconnect_state"] == "OFFLINE"
    assert offline["detail_is_displayable"] is True

    # A new Gateway process, as after a Supervisor restart, on a new port.
    live_gateway.start()
    restored = run_phase(live_gateway, desktop_account, session_dir, "restored")

    assert restored["authenticated_after_restart"] is True
    assert restored["status_after_restart"] == "ONLINE"
    assert restored["refresh_rotated"] is True
    assert restored["projects_visible"] == 1
    assert restored["logout_authenticated"] is False
    assert restored["session_file_after_logout"] is False
    assert restored["revoked_refresh_status"] == 401

    assert login_http(
        live_gateway.base_url,
        desktop_account["username"],
        desktop_account["password"],
    ).status_code == 200
