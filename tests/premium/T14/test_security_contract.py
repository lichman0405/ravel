"""Static and executable audits of the shipped Electron boundary.

Electron's window itself is a Windows-only manual artifact; what can be proven
on the Linux gate is that the built main-process bundle and the source it came
from deny Node access to the renderer, expose one named IPC allowlist, keep
tokens out of the renderer, and carry no shell/DAG-mutation bridge. The Node
cases under `tests/premium/T14/node/` are executed here so a gate failure names
the security property, not only a command exit code.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APP = REPO_ROOT / "apps" / "desktop"
NODE_TESTS = sorted((REPO_ROOT / "tests" / "premium" / "T14" / "node").glob("*.test.cjs"))


def read(*parts: str) -> str:
    return (APP.joinpath(*parts)).read_text(encoding="utf-8")


def test_the_shipped_primitives_refuse_foreign_senders_and_plaintext_tokens() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("Node is required to execute the built desktop tests")
    if not (APP / "dist" / "lib" / "security.cjs").is_file():
        pytest.fail("build the desktop first: pnpm --dir apps/desktop build")
    assert NODE_TESTS, "no Node test files were found"
    result = subprocess.run(
        [node, "--test", *[str(path) for path in NODE_TESTS]],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    tail = "\n".join((result.stdout + result.stderr).splitlines()[-25:])
    assert result.returncode == 0, f"Node security tests failed:\n{tail}"
    assert re.search(r"# fail 0\b", result.stdout), f"Node tests did not report success:\n{tail}"
    assert re.search(r"# tests [1-9]\d*\b", result.stdout), f"Node tests collected nothing:\n{tail}"


def test_the_window_bundle_keeps_the_renderer_sandboxed_and_unprivileged() -> None:
    main_source = read("src", "main", "main.ts")
    security_source = read("src", "main", "security.ts")
    preload_source = read("src", "preload", "preload.ts")

    for setting in (
        "contextIsolation: true",
        "nodeIntegration: false",
        "nodeIntegrationInWorker: false",
        "nodeIntegrationInSubFrames: false",
        "sandbox: true",
        "webSecurity: true",
        "allowRunningInsecureContent: false",
        "webviewTag: false",
        "navigateOnDragDrop: false",
    ):
        assert setting in security_source, setting

    assert "new BrowserWindow({" in main_source
    assert "webPreferences: {\n      ...secureWebPreferences(" in main_source
    assert "setWindowOpenHandler(() => windowOpenDecision())" in main_source
    assert "assertTrustedSender(event)" in main_source
    assert "event.senderFrame" in main_source
    assert "permissionRequestAllowed()" in main_source
    assert "shell.openExternal" not in main_source
    assert "openExternal(" not in main_source
    assert "child_process" not in main_source
    assert "execFile" not in main_source
    assert "nodeIntegration: true" not in main_source
    assert "enableRemoteModule" not in main_source
    assert "remote: true" not in main_source

    # No generic bridge: every invoke names a constant from the frozen map.
    assert len(re.findall(r"ipcRenderer\.invoke\(", preload_source)) == 6
    assert len(re.findall(r"ipcRenderer\.invoke\(\s*IPC_CHANNELS\.", preload_source)) == 6
    assert "contextBridge.exposeInMainWorld(BRIDGE_GLOBAL, api)" in preload_source
    assert "ipcRenderer.send(" not in preload_source
    assert "process.env" not in preload_source
    assert "node:fs" not in preload_source
    assert "require(" not in preload_source


def test_the_renderer_bundle_cannot_read_a_token_or_call_the_gateway_itself() -> None:
    renderer_dir = APP / "dist" / "renderer"
    index = (renderer_dir / "index.html").read_text(encoding="utf-8")
    scripts = list((renderer_dir / "assets").glob("*.js"))
    assert scripts, "the renderer bundle is missing; build first"
    bundle = "\n".join(path.read_text(encoding="utf-8") for path in scripts)

    assert "connect-src 'none'" in index
    assert "script-src 'self'" in index
    assert "object-src 'none'" in index
    assert "base-uri 'none'" in index
    assert "unsafe-inline" not in index
    assert "unsafe-eval" not in index

    for forbidden in (
        "ipcRenderer",
        "access_token",
        "refresh_token",
        "Authorization",
        "/v1/auth/",
        "/v1/projects",
        "localStorage",
        "sessionStorage",
        "indexedDB",
        "process.env",
    ):
        assert forbidden not in bundle, f"renderer bundle contains {forbidden}"
    assert "ravelDesktop" in bundle, "the renderer does not use the bridge"


def test_the_bridge_and_desktop_service_have_no_scientific_dag_mutation() -> None:
    contract = read("src", "shared", "contract.ts")
    service = read("src", "main", "gateway-service.ts")
    app_source = read("src", "renderer", "src", "App.tsx")

    assert "accessToken" not in contract
    assert "refreshToken" not in contract
    for channel in (
        "ravel:auth:login",
        "ravel:auth:logout",
        "ravel:session:read",
        "ravel:projects:list",
        "ravel:connection:read",
        "ravel:connection:reconnect",
        "ravel:connection:status",
    ):
        assert channel in contract
    assert "invoke(channel" not in contract
    assert "import { ipcRenderer" not in contract

    called = set(re.findall(r"\.([a-z0-9_]+_v1_[a-z0-9_]+)\(", service))
    assert called == {
        "login_v1_auth_login_post",
        "logout_v1_auth_logout_post",
        "refresh_v1_auth_refresh_post",
        "list_projects_v1_projects_get",
    }
    assert "from \"@ravel/api-client\"" in service
    for mutation in ("approve", "reject", "create_project", "dag", "pause", "resume"):
        assert mutation not in called
    assert "dangerouslySetInnerHTML" not in app_source
    assert "localStorage" not in app_source
    assert "fetch(" not in app_source


def test_the_windows_package_is_portable_x64_internal_and_exactly_pinned() -> None:
    builder = read("electron-builder.yml")
    assert "target: portable" in builder
    assert "- x64" in builder
    assert "signAndEditExecutable: false" in builder
    assert "dist/**/*" in builder
    assert "publish:" not in builder

    manifest = json.loads(read("package.json"))
    assert manifest["type"] == "module"
    assert manifest["main"] == "dist/main/main.cjs"
    assert manifest["devDependencies"]["electron"] == "44.9.1"
    assert manifest["devDependencies"]["typescript"] == "5.6.3"
    assert manifest["devDependencies"]["@ravel/api-client"] == "workspace:*"
    for name, version in manifest["devDependencies"].items():
        assert re.match(r"^\d+\.\d+\.\d+$", version) or version == "workspace:*", (
            f"{name}@{version} is not an exact pin"
        )

    root = json.loads((REPO_ROOT / "package.json").read_text(encoding="utf-8"))
    assert root["packageManager"] == "pnpm@9.15.9"
    assert root["engines"]["node"] == ">=22.12.0 <23"
    lock = (REPO_ROOT / "pnpm-lock.yaml").read_text(encoding="utf-8")
    assert "lockfileVersion: '9.0'" in lock
    assert "electron@44.9.1" in lock
    assert "esbuild@0.28.2" in lock

    # No development secret or environment file is shipped in the shell.
    assert not (APP / ".env").exists()
    for path in (APP / "src").rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8", errors="replace")
            assert "BEGIN PRIVATE KEY" not in text, path
            assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", text), path
