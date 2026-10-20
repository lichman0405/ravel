"""Where RAVEL is allowed to send a request, and how that is enforced.

The URL policy in `ravel.research.addressing` is asserted first as a table,
because it is a rule about addresses rather than about HTTP. The cases after it
are about the enforcement points:

- `_PinnedBackend` connects to the address that was checked, not to the name, so
  a resolver that answers twice with two different addresses reaches nothing the
  first answer did not permit;
- the transport checks every hop, so a redirect to a private address is refused
  before a socket is opened even though nobody wrote that address in the URL;
- the browser entry points refuse to start Playwright at all until T19 binds a
  sandbox provider, and refuse an unsafe URL as a refusal rather than as a
  navigation failure.

Two cases reach the real internet (httpbin.org) to prove the redirect defence
against a server that actually redirects; they retry a transport failure so a
momentary outage is distinguished from the refusal the test is about. The rest
are deterministic: a loopback listener proves no connection was made, and a
scripted httpcore pool proves the refused hop never reaches the network layer.
"""

from __future__ import annotations

import socket
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from urllib.parse import quote

import httpcore
import httpx
import pytest

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.research import addressing
from ravel.research.browser import (
    BOUND_SANDBOX_PROVIDERS,
    BrowserNavigator,
    BrowserUnavailable,
)
from ravel.research.fetching import Fetcher, FetchPolicy, _GuardedTransport, _PinnedBackend

#: A public address, used as what an honest resolver would answer. It belongs to
#: example.com and is never connected to by the deterministic cases: the point
#: of those cases is that the decision is made before a socket exists.
PUBLIC = "93.184.216.34"

#: A hostname RFC 2606 reserves so that it can never resolve.
NEVER_RESOLVES = "no-such-source.ravel.invalid"


def _ipv4_only(host: str) -> list[str]:
    """Real DNS, IPv4 answers only.

    The gate container may have no IPv6 route, and the pinned backend connects
    to the first validated address — which sorts IPv6 first. Filtering here
    keeps the external cases about the refusal rather than about the host's
    IPv6 routing table; every address connected to is still a real, resolved,
    public one.
    """
    return [address for address in addressing.resolve(host) if ":" not in address]


@contextmanager
def _loopback_listener() -> Iterator[socket.socket]:
    """A real listening socket on loopback, to prove nothing connects to it."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    try:
        yield listener
    finally:
        listener.close()


def _assert_nothing_connected(listener: socket.socket) -> None:
    """No connection is pending on a listener nothing was allowed to reach."""
    listener.setblocking(False)
    with pytest.raises(BlockingIOError):
        listener.accept()


# ── The policy itself ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "resolved", "fragment"),
    [
        pytest.param("file:///etc/passwd", [], "the scheme is 'file'", id="file-scheme"),
        pytest.param("ftp://example.com/x", [], "the scheme is 'ftp'", id="ftp-scheme"),
        pytest.param("data:text/plain,hi", [], "the scheme is 'data'", id="data-scheme"),
        pytest.param("http:///nothing", [], "names no host", id="no-host"),
        pytest.param(
            "http://169.254.169.254/latest/meta-data/",
            [],
            "not a public address",
            id="metadata-literal",
        ),
        pytest.param("http://127.0.0.1:7233/", [], "not a public address", id="loopback"),
        pytest.param("http://[::1]/", [], "not a public address", id="ipv6-loopback"),
        pytest.param(
            "http://[::ffff:169.254.169.254]/",
            [],
            "not a public address",
            id="mapped-metadata",
        ),
        pytest.param("http://10.12.0.4/", [], "not a public address", id="rfc1918-10"),
        pytest.param("http://192.168.1.9/", [], "not a public address", id="rfc1918-192"),
        pytest.param("http://172.20.0.1/", [], "not a public address", id="rfc1918-172"),
        pytest.param("http://100.64.0.1/", [], "not a public address", id="carrier-nat"),
        pytest.param("http://[fe80::1]/", [], "not a public address", id="link-local-v6"),
        pytest.param("http://[fc00::1]/", [], "not a public address", id="unique-local-v6"),
        pytest.param(
            "http://metadata.google.internal./computeMetadata/v1/",
            [PUBLIC],
            "instance metadata service",
            id="metadata-name-with-a-lying-resolver",
        ),
        pytest.param(
            "http://rebind.example/",
            [PUBLIC, "127.0.0.1"],
            "not a public address",
            id="one-private-among-public",
        ),
    ],
)
def test_the_address_policy_refuses_what_is_not_the_public_internet(
    url: str, resolved: Sequence[str], fragment: str
) -> None:
    """One rule per hazard, all of them before any request is made."""
    with pytest.raises(addressing.UnsafeURL) as refused:
        addressing.guard(url, resolver=lambda host: resolved)

    assert fragment in str(refused.value), str(refused.value)


@pytest.mark.parametrize(
    ("url", "resolved"),
    [
        pytest.param("http://example.com/", [PUBLIC], id="public-name"),
        pytest.param("https://example.com/path?q=1", [PUBLIC], id="https"),
        pytest.param("http://203.0.113.9/", [], id="documentation-literal"),
        pytest.param("http://198.18.0.7/", [], id="benchmark-literal"),
        pytest.param("http://[2001:db8::1]/", [], id="documentation-literal-v6"),
        pytest.param(
            "http://[2606:2800:220:1:248:1893:25c8:1946]/", [], id="public-literal-v6"
        ),
    ],
)
def test_the_address_policy_allows_a_public_source(url: str, resolved: Sequence[str]) -> None:
    """The refusals above are only meaningful if the ordinary case passes.

    The documentation and benchmarking ranges are in the allowed set on
    purpose: no service lives there, so refusing them protects nothing, and
    `SECURITY_NOTES.md` records the trade-off.
    """
    asked: list[str] = []

    def resolver(host: str) -> Sequence[str]:
        asked.append(host)
        return resolved

    addressing.guard(url, resolver=resolver)
    if resolved:
        assert asked == ["example.com"], "a name was not resolved"
    else:
        assert asked == [], "a literal address was sent to the resolver"


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.216.34", True),
        ("203.0.113.9", True),
        ("198.18.0.7", True),
        ("127.0.0.1", False),
        ("169.254.169.254", False),
        ("10.0.0.1", False),
        ("100.64.0.1", False),
        ("224.0.0.1", False),
        ("255.255.255.255", False),
        ("0.0.0.0", False),
        ("2606:2800:220:1:248:1893:25c8:1946", True),
        ("::1", False),
        ("fe80::1", False),
        ("fc00::1", False),
        ("::ffff:169.254.169.254", False),
        ("64:ff9b::7f00:1", False),
        ("not-an-address", False),
    ],
)
def test_is_public_is_a_question_about_the_address_not_the_name(
    address: str, public: bool
) -> None:
    """IPv4-mapped and NAT64 forms are unwrapped before the answer is given."""
    assert addressing.is_public(address) is public


def test_a_metadata_name_is_refused_even_when_dns_lies() -> None:
    """The name is checked before the resolver is asked.

    This is the case the address check cannot cover: a host whose DNS is
    intercepted answers with a placeholder for every name, so the answer is
    public and meaningless. The name is the only signal left.
    """
    asked: list[str] = []

    def resolver(host: str) -> Sequence[str]:
        asked.append(host)
        return [PUBLIC]

    for name in (
        "metadata.google.internal",
        "metadata.google.internal.",
        "sub.metadata.google.internal",
        "instance-data.ec2.internal",
        "metadata.azure.com",
    ):
        with pytest.raises(addressing.UnsafeURL, match="instance metadata service"):
            addressing.address_for(name, resolver)
    assert asked == []


# ── Enforcement: no connection is made ──────────────────────────────────────


def test_a_name_that_resolves_to_loopback_is_refused_before_a_socket_opens(
    gateway_settings: Settings,
) -> None:
    """A resolver answer that is private stops the request at the guard.

    The listener is real, so a bug that moved the check behind the connection
    would show up as a pending connection rather than as a passing assertion
    about a mock.
    """
    with _loopback_listener() as listener:
        port = listener.getsockname()[1]
        fetcher = Fetcher(gateway_settings, resolver=lambda host: ["127.0.0.1"])

        with pytest.raises(addressing.UnsafeURL) as refused:
            fetcher.get(f"http://internal.example:{port}/")

        assert "not a public address" in str(refused.value)
        _assert_nothing_connected(listener)


def test_a_redirect_target_is_refused_before_the_second_hop_is_sent(
    gateway_settings: Settings,
) -> None:
    """The transport checks redirects, deterministically and without a network.

    A scripted connection pool answers the first request with a 302 to a
    private address. The redirect is a second request through the transport, so
    the guard sees it; the pool's request count proves the second hop never
    reached the layer that would have opened a socket.
    """

    class RedirectingPool:
        def __init__(self, location: str) -> None:
            self.location = location
            self.requests: list[httpcore.Request] = []

        def handle_request(self, request: httpcore.Request) -> httpcore.Response:
            self.requests.append(request)
            return httpcore.Response(
                302,
                headers=[(b"location", self.location.encode())],
                content=b"",
            )

        def close(self) -> None:
            pass

    for location in (
        "http://127.0.0.1:9/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::1]/",
        "http://10.0.0.5/",
    ):
        transport = _GuardedTransport(lambda host: [PUBLIC])
        pool = RedirectingPool(location)
        transport._pool = pool  # type: ignore[attr-defined]
        with (
            httpx.Client(transport=transport, follow_redirects=True) as http,
            pytest.raises(addressing.UnsafeURL) as refused,
        ):
            http.get("http://public.example/")
        assert "not a public address" in str(refused.value)
        assert len(pool.requests) == 1, f"{location}: the refused hop was sent"


def test_dns_rebinding_is_refused_at_the_connection() -> None:
    """The second resolution is the one that is not trusted.

    The first answer (at the guard) is public; the second (at the socket) is
    loopback. Connecting to the *checked address* is what makes the change
    unable to matter: the pinned backend resolves again and refuses, rather than
    handing the name back to httpcore to resolve a third time.

    Both resolutions are made here, in the order a request makes them: the
    guard's, then the backend's.
    """
    answers = [PUBLIC, "127.0.0.1"]
    asked: list[str] = []

    def rebinding(host: str) -> Sequence[str]:
        asked.append(host)
        return [answers[min(len(asked), len(answers)) - 1]]

    addressing.guard("http://rebind.example/", resolver=rebinding)
    backend = _PinnedBackend(rebinding)
    with pytest.raises(addressing.UnsafeURL) as refused:
        backend.connect_tcp("rebind.example", 80)

    assert len(asked) == 2, f"expected a guard resolution and a connect resolution: {asked}"
    assert "127.0.0.1" in str(refused.value)


def test_the_backend_connects_to_the_validated_address_and_not_the_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host argument reaching the socket layer is an address, not a name.

    This is the assertion the defence rests on and it cannot be made by
    observing a successful request alone: a client that resolved the name a
    second time would succeed too. Monkeypatching the parent's `connect_tcp`
    makes the argument visible at the moment the socket would be opened.
    """
    seen: list[tuple[str, int]] = []

    def recording(
        self: httpcore.SyncBackend,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        seen.append((host, port))
        raise httpcore.ConnectError("stop after recording")

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", recording)
    backend = _PinnedBackend(lambda host: [PUBLIC])
    with pytest.raises(httpcore.ConnectError):
        backend.connect_tcp("pinned.example", 443)

    assert seen == [(PUBLIC, 443)]


def test_a_name_that_does_not_resolve_is_a_transport_failure_not_a_refusal(
    gateway_settings: Settings,
) -> None:
    """DNS failure is reported the way every other unreachable host is.

    `UnsafeURL` is a decision RAVEL makes; a name that does not resolve is a
    fact about the network. Conflating them would make a mistyped lead look like
    an attack, and would raise past research loops that are written to record a
    limited retrieval.
    """
    fetcher = Fetcher(gateway_settings, policy=FetchPolicy(timeout_seconds=5.0))
    retrieval = fetcher.get(f"http://{NEVER_RESOLVES}/")

    assert retrieval.access_status is AccessStatus.ACCESS_LIMITED
    assert retrieval.content_hash is None
    assert retrieval.body is None
    assert retrieval.excerpt == ""
    assert "ConnectError" in retrieval.note, retrieval.note


# ── The real internet still works, and a real redirect is still refused ────


def _external_get(fetcher: Fetcher, url: str, *, attempts: int = 3) -> Any:
    """Fetch a URL, retrying only the transient failures external hosts have.

    A refusal is never retried: `UnsafeURL` propagates on the first attempt.
    A retrieval that could not reach the host is retried, so an httpbin outage
    is reported as an outage rather than mistaken for the refusal a redirect
    test is looking for.
    """
    last: Any = None
    for attempt in range(attempts):
        last = fetcher.get(url)
        if last.access_status is AccessStatus.OK or attempt == attempts - 1:
            return last
        time.sleep(0.5)
    return last


def _expect_refusal(fetcher: Fetcher, url: str) -> addressing.UnsafeURL:
    """Fetch a URL that must be refused, distinguishing an outage from a refusal."""
    last: Any = None
    for _ in range(3):
        try:
            last = fetcher.get(url)
        except addressing.UnsafeURL as refused:
            return refused
        time.sleep(0.5)
    pytest.fail(f"{url} was not refused; the first hop reported {last.access_status}: {last.note}")


def test_a_real_server_redirect_not_the_first_hop_to_loopback(gateway_settings: Settings) -> None:
    """A public 302 to a private address is refused on the hop.

    The refusal must be about the redirect target: a DNS failure would also
    raise `UnsafeURL`, and the reason is asserted to name the private address
    rather than "no address to connect to".
    """
    target = "http://127.0.0.1:9/"
    url = f"https://httpbin.org/redirect-to?url={quote(target, safe='')}"
    fetcher = Fetcher(
        gateway_settings, FetchPolicy(timeout_seconds=20.0), resolver=_ipv4_only
    )

    message = str(_expect_refusal(fetcher, url))
    assert "not a public address" in message, message
    assert "no address to connect to" not in message, message


def test_a_real_server_redirect_to_the_metadata_service_is_refused(
    gateway_settings: Settings,
) -> None:
    """The same defence, aimed at the address that holds credentials."""
    target = "http://169.254.169.254/latest/meta-data/iam/security-credentials/"
    url = f"https://httpbin.org/redirect-to?url={quote(target, safe='')}"
    fetcher = Fetcher(
        gateway_settings, FetchPolicy(timeout_seconds=20.0), resolver=_ipv4_only
    )

    message = str(_expect_refusal(fetcher, url))
    assert "not a public address" in message, message


def test_a_real_public_fetch_connects_to_the_resolved_address(
    gateway_settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful fetch, with the connected host observed at the socket layer.

    Every host the backend was asked to connect to is an address; the name the
    URL used never reaches it. This is the end-to-end version of the pinned
    backend case above.
    """
    connected: list[str] = []
    original = httpcore.SyncBackend.connect_tcp

    def recording(
        self: httpcore.SyncBackend,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.NetworkStream:
        connected.append(host)
        return original(
            self,
            host,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    monkeypatch.setattr(httpcore.SyncBackend, "connect_tcp", recording)
    fetcher = Fetcher(
        gateway_settings, FetchPolicy(timeout_seconds=20.0), resolver=_ipv4_only
    )
    retrieval = _external_get(fetcher, "http://example.com/")

    assert retrieval.access_status is AccessStatus.OK, retrieval.note
    assert connected, "the transport never reached a socket"
    assert all(addressing.is_public(host) for host in connected), connected
    assert "example.com" not in connected


# ── The browser is fail-closed ─────────────────────────────────────────────


def test_this_build_binds_no_browser_sandbox_provider() -> None:
    """The gate has to be closed by the build, not by the fixture."""
    assert not BOUND_SANDBOX_PROVIDERS, "a provider was bound without T19's sandbox"
    assert isinstance(BOUND_SANDBOX_PROVIDERS, frozenset)


@pytest.mark.parametrize("provider", [None, "", "   ", "playwright", "docker", "chromium"])
def test_the_browser_refuses_to_start_without_a_bound_sandbox(
    gateway_settings: Settings, monkeypatch: pytest.MonkeyPatch, provider: str | None
) -> None:
    """No provider, or a name that is not implemented, closes the path.

    `_on_worker` is replaced with a failure, so this asserts the order as well
    as the message: the refusal happens before Playwright is imported, a thread
    is asked for, or a socket could be opened.
    """
    gateway_settings.research_browser_sandbox_provider = provider
    navigator = BrowserNavigator(gateway_settings, resolver=lambda host: [PUBLIC])
    monkeypatch.setattr(
        navigator,
        "_on_worker",
        lambda work: pytest.fail("the browser worker was asked to run"),
    )
    try:
        with pytest.raises(BrowserUnavailable, match="T19"):
            navigator.open("http://example.com/")
        with pytest.raises(BrowserUnavailable, match="T19"):
            navigator.links("http://example.com/")
    finally:
        navigator.close()


def test_an_unsafe_browser_url_is_reported_as_a_url_refusal(
    gateway_settings: Settings,
) -> None:
    """The URL decision does not depend on whether a browser exists.

    With no sandbox bound, an unsafe URL still raises `UnsafeURL` rather than
    `BrowserUnavailable`: the URL is wrong regardless of the browser, and an
    operator reading the failure has to be able to tell the two apart.
    """
    navigator = BrowserNavigator(gateway_settings, resolver=lambda host: ["127.0.0.1"])
    try:
        with pytest.raises(addressing.UnsafeURL, match="not a public address"):
            navigator.open("http://internal.example/")
        with pytest.raises(addressing.UnsafeURL, match="not a public address"):
            navigator.links("http://169.254.169.254/")
    finally:
        navigator.close()
