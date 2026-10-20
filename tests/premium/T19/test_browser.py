"""T19: a real Chromium in a container whose only exit is an allowlisted proxy.

The command sandbox has no network at all. A browser cannot work under that
rule, so T19 builds a second shape for it: a per-project internal Docker network
with exactly two peers, the browser and a proxy container, and a proxy that is
also attached to an egress network so it can reach the operator's targets. The
browser's network has no route anywhere else, and the proxy refuses every
connection that is not on the operator's exact `host:port` allowlist or that
resolves anywhere other than a public address.

These cases observe the deployed objects, not the argv:

- `docker inspect` of the browser and proxy containers, the internal network's
  peer list, and the proxy's environment, to establish the topology;
- a probe executed inside the browser container that speaks raw HTTP proxy to
  the proxy, and raw TCP to the fixture target and the metadata service;
- a real Chromium render through `BrowserNavigator` and an artifact snapshot
  registered through `ResearchSourceGateway`, to prove the Research path is
  wired to this provider and still hashes the bytes it stores;
- a client on the proxy's *egress* network, which must be refused, because the
  proxy would otherwise be an open proxy for anything that can reach it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import SecretStr
from tests.premium.T19.support import (
    FIXTURE_ADDRESS,
    FIXTURE_PORT,
    FIXTURE_URL,
    SANDBOX_IMAGE,
    Deployment,
    MemoryArtifactStore,
    execute_command,
    handle_record,
)

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.research import addressing
from ravel.research.browser import BrowserNavigator, BrowserPolicy, BrowserUnavailable
from ravel.research.gateway import ResearchSourceGateway, SourceRequest
from ravel.sandbox import addressing as sandbox_addressing
from ravel.sandbox.egress import EgressRefused, ProxyPolicy
from ravel.sandbox.models import (
    SandboxForbidden,
    SandboxPurpose,
    SandboxUnsafeTarget,
)
from ravel.sandbox.provider import DockerSandboxProvider
from ravel.state.repositories.research import ArtifactRepository, EvidenceSourceRepository
from ravel.state.store import hash_chunks

pytestmark = pytest.mark.integration

GIB = 1024**3
MIB = 1024**2


def _kernel_security(engine: Any, container: str) -> dict[str, str]:
    """Read seccomp and no-new-privs from a process inside the container.

    Docker's built-in default seccomp profile is applied when no `seccomp=`
    option is given, and it does not appear in `HostConfig.SecurityOpt`. The
    kernel status of a real process is the evidence that the profile is active.
    """
    result = engine.exec_capture(
        container,
        ["grep", "-E", "^(Seccomp|NoNewPrivs):", "/proc/self/status"],
        timeout=10,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    return dict(line.split(":", 1) for line in result.stdout.splitlines())

#: A probe that runs inside the browser sandbox. It has no route except the
#: internal network, so every one of these requests is a statement about the
#: deployed topology rather than about a configuration string.
_PROBE = r"""
import json
import os
import socket
import sys
from urllib.parse import urlsplit

proxy_host = sys.argv[1]
proxy_port = int(sys.argv[2])


def through_proxy(url):
    parts = urlsplit(url)
    try:
        sock = socket.create_connection((proxy_host, proxy_port), timeout=15)
        request = (
            "GET " + url + " HTTP/1.1\r\n"
            "Host: " + parts.netloc + "\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(request.encode("ascii"))
        chunks = []
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            chunks.append(chunk)
        sock.close()
        raw = b"".join(chunks)
        head, _, body = raw.partition(b"\r\n\r\n")
        first = head.split(b"\r\n", 1)[0].decode("iso-8859-1")
        return {
            "status": int(first.split(" ")[1]),
            "first_line": first,
            "body": body.decode("utf-8", "replace"),
        }
    except OSError as exc:
        return {"status": None, "error": type(exc).__name__}


def direct(host, port):
    try:
        sock = socket.create_connection((host, port), timeout=2)
        sock.close()
        return "connected"
    except OSError as exc:
        return type(exc).__name__


def workspace_write():
    try:
        with open("/workspace/probe.txt", "w", encoding="utf-8") as handle:
            handle.write("written")
        return {"wrote": True}
    except OSError as exc:
        return {"wrote": False, "errno": exc.errno, "type": type(exc).__name__}


print(json.dumps({
    "allowlisted": through_proxy("http://%s:%d/" % (sys.argv[3], int(sys.argv[4]))),
    "other": through_proxy("http://%s:%d/other" % (sys.argv[3], int(sys.argv[4]))),
    "unlisted": through_proxy("http://203.0.113.10:%d/" % int(sys.argv[4])),
    "metadata": through_proxy("http://169.254.169.254/latest/meta-data/"),
    "private": through_proxy("http://10.1.2.3/"),
    "forbidden_port": through_proxy("http://%s:22/" % sys.argv[3]),
    "direct_fixture": direct(sys.argv[3], int(sys.argv[4])),
    "direct_metadata": direct("169.254.169.254", 80),
    "direct_private": direct("10.1.2.3", 80),
    "docker_socket": os.path.exists("/var/run/docker.sock"),
    "workspace": workspace_write(),
}))
"""


def _browser_handle(
    deployment: Deployment,
    project_id: str,
    authority: Any,
) -> tuple[DockerSandboxProvider, Any]:
    provider = deployment.provider(project_id)
    handle = provider.provision(
        project_id, authority, purpose=SandboxPurpose.BROWSER
    )
    return provider, handle


def _network(engine: Any, name: str) -> dict[str, Any]:
    result = engine.run(["network", "inspect", name], timeout=15)
    document = json.loads(result.stdout)
    assert isinstance(document, list) and document, document
    return document[0]


def _peer_names(network: dict[str, Any]) -> set[str]:
    return {str(peer["Name"]) for peer in network.get("Containers", {}).values()}


def test_browser_sandbox_is_one_container_behind_one_proxy_on_an_internal_network(
    deployment: Deployment,
    docker_engine: Any,
    fixture_network: str,
) -> None:
    """The browser has one peer, one route, and the same hardening as a command."""
    project_id = deployment.project_id("browser-topology")
    authority = deployment.authority(project_id, role="RESEARCH")
    provider, handle = _browser_handle(deployment, project_id, authority)
    try:
        record = handle_record(deployment, handle.handle_id)
        container = str(record["container"])
        proxy = str(record["proxy"])
        internal_network = str(record["network"])
        assert record["proxy_url"] == f"http://{proxy}:3128"
        assert handle.purpose is SandboxPurpose.BROWSER

        host = docker_engine.inspect(container)["HostConfig"]
        assert host["ReadonlyRootfs"] is True
        assert host["Privileged"] is False
        assert host["PidMode"] == ""
        assert host["IpcMode"] != "host"
        assert host["CapDrop"] == ["ALL"]
        assert any("no-new-privileges" in option for option in host["SecurityOpt"])
        assert not any("seccomp=unconfined" in option for option in host["SecurityOpt"])
        browser_security = _kernel_security(docker_engine, container)
        assert browser_security["Seccomp"].strip() == "2", browser_security
        assert browser_security["NoNewPrivs"].strip() == "1", browser_security
        assert host["NanoCpus"] == 2_000_000_000
        assert host["Memory"] == 4 * GIB
        assert host["MemorySwap"] == 4 * GIB
        assert host["PidsLimit"] == 256
        assert host["ShmSize"] == 512 * MIB
        assert host["NetworkMode"] == internal_network
        tmpfs = host["Tmpfs"]["/tmp"]
        assert "size=" in tmpfs and "noexec" in tmpfs and "nosuid" in tmpfs

        mounts = docker_engine.inspect(container)["Mounts"]
        workspace = [mount for mount in mounts if mount["Destination"] == "/workspace"]
        assert len(mounts) == 1, mounts
        assert workspace and workspace[0]["Type"] == "volume"
        assert workspace[0]["RW"] is False, "the browser must not write its workspace"
        assert not any("docker.sock" in str(mount.get("Source", "")) for mount in mounts)

        config = docker_engine.inspect(container)["Config"]
        assert config["User"] == "65534:65534"
        names = {entry.split("=", 1)[0] for entry in config["Env"]}
        assert not {name for name in names if name.startswith(("RAVEL_POSTGRES", "PG", "DSN"))}

        internal = _network(docker_engine, internal_network)
        assert internal["Internal"] is True, internal
        assert _peer_names(internal) == {container, proxy}

        # The proxy is the only thing on the fixture's egress network besides
        # the fixture server itself; the browser is not there.
        egress = _network(docker_engine, fixture_network)
        egress_peers = _peer_names(egress)
        assert proxy in egress_peers, egress_peers
        assert container not in egress_peers, egress_peers

        proxy_host = docker_engine.inspect(proxy)["HostConfig"]
        assert proxy_host["ReadonlyRootfs"] is True
        assert proxy_host["Privileged"] is False
        assert proxy_host["PidMode"] == ""
        assert proxy_host["CapDrop"] == ["ALL"]
        assert any("no-new-privileges" in option for option in proxy_host["SecurityOpt"])
        assert not any("seccomp=unconfined" in option for option in proxy_host["SecurityOpt"])
        proxy_security = _kernel_security(docker_engine, proxy)
        assert proxy_security["Seccomp"].strip() == "2", proxy_security
        assert proxy_security["NoNewPrivs"].strip() == "1", proxy_security
        assert proxy_host["NetworkMode"] == internal_network
        assert docker_engine.inspect(proxy)["Mounts"] == []

        proxy_config = docker_engine.inspect(proxy)["Config"]
        assert proxy_config["User"] == "65534:65534"
        environment = dict(
            entry.split("=", 1) for entry in proxy_config["Env"] if "=" in entry
        )
        assert environment["RAVEL_PROXY_ALLOWLIST"] == f"{FIXTURE_ADDRESS}:{FIXTURE_PORT}"
        assert environment["RAVEL_PROXY_CLIENT_CIDR"] == _subnet(docker_engine, internal_network)
        assert environment["RAVEL_PROXY_PORT"] == "3128"
        assert deployment.token not in json.dumps(proxy_config["Env"])

        proxy_networks = set(
            docker_engine.inspect(proxy)["NetworkSettings"]["Networks"]
        )
        assert proxy_networks == {internal_network, fixture_network}, proxy_networks
    finally:
        provider.destroy(handle, authority)


def _subnet(engine: Any, network: str) -> str:
    result = engine.run(
        [
            "network",
            "inspect",
            "--format",
            "{{range .IPAM.Config}}{{.Subnet}} {{end}}",
            network,
        ],
        timeout=15,
    )
    return result.stdout.split()[0]


def test_the_browser_has_no_direct_route_and_the_proxy_refuses_everything_else(
    deployment: Deployment,
    docker_engine: Any,
    fixture_network: str,
) -> None:
    """Raw sockets from inside the browser container: one door, and it is guarded."""
    project_id = deployment.project_id("browser-egress")
    authority = deployment.authority(project_id, role="RESEARCH")
    provider, handle = _browser_handle(deployment, project_id, authority)
    try:
        record = handle_record(deployment, handle.handle_id)
        container = str(record["container"])
        proxy = str(record["proxy"])
        proxy_name = proxy
        result = docker_engine.exec_capture(
            container,
            [
                "python3",
                "-c",
                _PROBE,
                proxy_name,
                "3128",
                FIXTURE_ADDRESS,
                str(FIXTURE_PORT),
            ],
            timeout=120,
            env={"PYTHONDONTWRITEBYTECODE": "1", "HOME": "/tmp"},
        )
        assert result.returncode == 0, result.stderr[-2000:]
        probe = json.loads(result.stdout.strip().splitlines()[-1])

        allowed = probe["allowlisted"]
        assert allowed["status"] == 200, allowed
        assert "raw-bytes-before-script" in allowed["body"]
        other = probe["other"]
        assert other["status"] == 200, other
        assert "a second allowlisted path" in other["body"]

        for name in ("unlisted", "metadata", "private", "forbidden_port"):
            refusal = probe[name]
            assert refusal["status"] == 403, (name, refusal)
            assert "RAVEL sandbox egress refused" in refusal["body"], (name, refusal)
        assert "allowlist" in probe["unlisted"]["body"]
        assert "infrastructure" in probe["forbidden_port"]["body"]

        # The browser itself has no route to the fixture server, the metadata
        # service, or a private address. Only the proxy does.
        for name in ("direct_fixture", "direct_metadata", "direct_private"):
            assert probe[name] != "connected", (name, probe[name])

        assert probe["docker_socket"] is False
        assert probe["workspace"]["wrote"] is False, probe["workspace"]
        assert probe["workspace"]["errno"] == 30, probe["workspace"]  # EROFS

        # A client on the proxy's egress network is not a sandbox client. The
        # proxy must refuse it before reading a request, or it is an open proxy.
        proxy_ip = docker_engine.inspect(proxy)["NetworkSettings"]["Networks"][
            fixture_network
        ]["IPAddress"]
        outsider = r"""
import json
import socket
import sys

try:
    sock = socket.create_connection((sys.argv[1], int(sys.argv[2])), timeout=10)
    sock.sendall(
        b"GET http://203.0.113.9:18008/ HTTP/1.1\r\n"
        b"Host: 203.0.113.9:18008\r\nConnection: close\r\n\r\n"
    )
    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    sock.close()
    head, _, body = data.partition(b"\r\n\r\n")
    print(json.dumps({
        "status": int(head.split(b" ", 2)[1]),
        "body": body.decode("utf-8", "replace"),
    }))
except OSError as exc:
    print(json.dumps({"status": None, "error": type(exc).__name__}))
"""
        completed = docker_engine.run(
            [
                "run",
                "--rm",
                "--network",
                fixture_network,
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
                SANDBOX_IMAGE,
                "-c",
                outsider,
                proxy_ip,
                "3128",
            ],
            timeout=90,
        )
        assert completed.returncode == 0, completed.stderr[-2000:]
        answer = json.loads(completed.stdout.strip().splitlines()[-1])
        assert answer["status"] == 403, answer
        assert "not on the sandbox's internal network" in answer["body"], answer
    finally:
        provider.destroy(handle, authority)


def test_egress_policy_refuses_metadata_and_private_networks_even_when_allowlisted() -> None:
    """The allowlist says which name; the address policy still says which address."""
    metadata = ProxyPolicy.from_config(["169.254.169.254:80"])
    with pytest.raises(EgressRefused, match="metadata"):
        metadata.check("169.254.169.254", 80)

    private = ProxyPolicy.from_config(["10.4.5.6:443"])
    with pytest.raises(EgressRefused, match="not a public address"):
        private.check("10.4.5.6", 443)

    allowed = ProxyPolicy.from_config([f"{FIXTURE_ADDRESS}:{FIXTURE_PORT}"])
    assert allowed.check(FIXTURE_ADDRESS, FIXTURE_PORT) == (FIXTURE_ADDRESS,)
    with pytest.raises(EgressRefused, match="allowlist"):
        allowed.check("203.0.113.10", FIXTURE_PORT)
    with pytest.raises(EgressRefused, match="infrastructure"):
        allowed.check(FIXTURE_ADDRESS, 22)

    # The sandbox image is deliberately dependency-free, so it carries a copy of
    # the address policy rather than importing `ravel.research` (whose package
    # exports pull in SQLAlchemy). These comparisons make the copy observable:
    # every address, name, and scheme below must be classified identically by
    # the host policy and the in-container policy.
    addresses = (
        "8.8.8.8",
        "203.0.113.9",
        "127.0.0.1",
        "169.254.169.254",
        "10.4.5.6",
        "100.64.0.1",
        "::1",
        "::ffff:169.254.169.254",
        "fd00:ec2::254",
        "not-an-address",
    )
    for address in addresses:
        assert sandbox_addressing.is_public(address) == addressing.is_public(address), address
    for name in (
        "example.com",
        "metadata.google.internal",
        "instance-data.ec2.internal",
        "metadata.google.internal.",
    ):
        assert sandbox_addressing.names_the_metadata_service(
            name
        ) == addressing.names_the_metadata_service(name), name

    def outcome(module: Any, url: str) -> tuple[str, str | None]:
        try:
            module.guard(url, resolver=lambda _host: ())
        except module.UnsafeURL as exc:
            return ("refused", exc.reason)
        return ("allowed", None)

    for url in (
        "file:///etc/passwd",
        "http://169.254.169.254/latest/meta-data/",
        "https://metadata.google.internal/",
        "http://127.0.0.1:5432/",
        "https://example.com/",
    ):
        assert outcome(sandbox_addressing, url) == outcome(addressing, url), url


def test_research_browser_renders_in_the_sandbox_and_registers_a_hash_checked_snapshot(
    deployment: Deployment,
    database: Any,
    project: Any,
    integration_settings: Any,
    monkeypatch: pytest.MonkeyPatch,
    docker_engine: Any,
) -> None:
    """The Research path runs Chromium there, returns the DOM, and registers it here."""
    project_id = project.project_id
    monkeypatch.setenv("RAVEL_PROJECT_ID", project_id)
    monkeypatch.setenv("RAVEL_ROLE", "RESEARCH")
    monkeypatch.setenv("RAVEL_BINDING_ID", "research-browser-test")
    monkeypatch.delenv("RAVEL_LEASE_EPOCH", raising=False)
    settings = Settings(
        env="test",
        research_browser_sandbox_provider="docker",
        sandbox_supervisor_url=deployment.base_url,
        sandbox_supervisor_token=SecretStr(deployment.token),
    )
    store = MemoryArtifactStore()
    navigator = BrowserNavigator(
        settings, policy=BrowserPolicy(timeout_ms=45_000, settle_ms=5_000)
    )
    try:
        with database.transaction() as session:
            gateway = ResearchSourceGateway(
                session,
                project_id,
                settings=integration_settings,
                store=store,
                browser=navigator,
            )
            retrieval = gateway.open(SourceRequest.of(FIXTURE_URL), rendered=True)
            assert retrieval.access_status is AccessStatus.OK, retrieval.note
            assert "rendered document" in retrieval.note
            body = retrieval.body
            assert body is not None
            assert "rendered-by-javascript" in body.decode("utf-8")
            assert "raw-bytes-before-script" not in body.decode("utf-8")
            assert retrieval.title == "T19 Sandbox Fixture"
            assert retrieval.final_url == FIXTURE_URL
            assert retrieval.status_code == 200
            digest, _ = hash_chunks([body])
            assert retrieval.content_hash == digest

            registration = gateway.register(
                SourceRequest.of(FIXTURE_URL),
                retrieval,
                actor_id=project.created_by,
            )
            assert registration.source.content_hash == retrieval.content_hash
            assert registration.source.was_read is True
            assert registration.source.artifact_ref is not None
            assert registration.snapshot_ref is not None

            artifacts = ArtifactRepository(session, project_id, store=store)
            versions = artifacts.versions(registration.source.artifact_ref)
            assert len(versions) == 1, versions
            version = versions[0]
            assert version.content_hash == retrieval.content_hash
            assert version.storage_key == registration.snapshot_ref
            assert store.get(version.storage_key) == retrieval.body
            assert artifacts.verify(version) is True

            stored = EvidenceSourceRepository(session, project_id).get(
                source_id=registration.source.source_id
            )
            assert stored.content_hash == retrieval.content_hash

        # The navigator holds this project's browser handle; closing it stops
        # the sandbox but leaves the handle and the workspace queryable. Other
        # browser tests' records remain in the same state directory — that is
        # what queryable means — so the lookup is scoped to this project and
        # binding rather than counting every browser handle the run created.
        records = [
            json.loads(path.read_text("utf-8"))
            for path in (deployment.state_dir / "handles").glob("*.json")
        ]
        browser_handles = [
            record
            for record in records
            if record.get("handle", {}).get("purpose") == "BROWSER"
            and record.get("handle", {}).get("project_id") == project_id
            and record.get("binding_id") == "research-browser-test"
        ]
        assert len(browser_handles) == 1, browser_handles
        container = str(browser_handles[0]["container"])
        assert docker_engine.container_running(container)
        navigator.close()
        assert handle_record(
            deployment, browser_handles[0]["handle"]["handle_id"]
        )["handle"]["state"] == "STOPPED"
        assert docker_engine.container_running(container) is False
    finally:
        navigator.close()


def test_browser_refuses_metadata_loopback_and_non_research_callers(
    deployment: Deployment,
) -> None:
    """A refusal is about the URL or the role, and neither starts Chromium."""
    project_id = deployment.project_id("browser-refusals")
    research = deployment.authority(project_id, role="RESEARCH")
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, research, purpose=SandboxPurpose.BROWSER)
    try:
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:8080/",
            "http://10.1.2.3/",
            "file:///etc/passwd",
        ):
            with pytest.raises(SandboxUnsafeTarget):
                provider.fetch(url=url, authority=research)

        worker = deployment.authority(project_id)
        worker_provider = deployment.provider(project_id)
        with pytest.raises(SandboxForbidden, match="Research"):
            worker_provider.fetch(url=FIXTURE_URL, authority=worker)
        with pytest.raises(SandboxForbidden, match="Research"):
            execute_command(
                deployment, handle, worker, ["/bin/echo", "not-here"], timeout=20
            )
    finally:
        provider.destroy(handle, research)


def test_the_research_navigator_refuses_without_a_bound_research_sandbox(
    deployment: Deployment,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider name is not a binding; a missing role fails closed, not to HTTP."""
    settings = Settings(
        env="test",
        research_browser_sandbox_provider="docker",
        sandbox_supervisor_url=deployment.base_url,
        sandbox_supervisor_token=SecretStr(deployment.token),
    )
    monkeypatch.setenv("RAVEL_PROJECT_ID", "t19-unbound")
    monkeypatch.setenv("RAVEL_ROLE", "EXPERIMENTAL_WORKER")
    with BrowserNavigator(settings) as navigator:
        with pytest.raises(BrowserUnavailable, match="Research role"):
            navigator.open(FIXTURE_URL)
        with pytest.raises(addressing.UnsafeURL):
            navigator.open("http://169.254.169.254/latest/meta-data/")

    unconfigured = Settings(env="test", research_browser_sandbox_provider=None)
    with BrowserNavigator(unconfigured) as navigator, pytest.raises(BrowserUnavailable):
        navigator.open(FIXTURE_URL)
