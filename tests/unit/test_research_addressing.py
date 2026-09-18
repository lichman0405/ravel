"""Which URLs RAVEL refuses to request, and why it has to.

RAVEL fetches strings that arrived from outside: a lead from a search provider,
a link from a page, a URL an agent assembled. Handing one to an HTTP client is a
request made on behalf of whoever wrote it, from inside the network RAVEL runs
in. On a cloud host that network contains an instance metadata service, and on
any host it contains PostgreSQL, MinIO and Temporal.

These tests are written against the addresses that make that concrete rather
than against a rule. The resolver is injected everywhere, so each case states
the address it is about and the suite never depends on DNS — which also means a
name that resolves differently tomorrow cannot quietly change what is tested.

The last test is the one that matters most: a check that only looks at the first
address a name resolves to is a check that a name with two addresses defeats.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ravel.config import Settings
from ravel.research import addressing
from ravel.research.addressing import UnsafeURL
from ravel.research.browser import BrowserNavigator
from ravel.research.fetching import Fetcher

PUBLIC = "93.184.216.34"


def resolver_for(*addresses: str):
    """A resolver that answers with exactly these addresses."""
    return lambda host: list(addresses)


def refuses(url: str, *addresses: str) -> str:
    """The reason `url` is refused, when every host resolves to `addresses`."""
    with pytest.raises(UnsafeURL) as raised:
        addressing.guard(url, resolver=resolver_for(*addresses))
    return raised.value.reason


# --------------------------------------------------------------------------
# The addresses this exists for
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "what"),
    [
        ("169.254.169.254", "the instance metadata service on every major cloud"),
        ("169.254.0.23", "the same range, which is where Tencent's lives"),
        ("127.0.0.1", "loopback"),
        ("127.1.2.3", "loopback, away from the usual spelling"),
        ("0.0.0.0", "the unspecified address"),
        ("10.1.2.3", "RFC 1918"),
        ("172.20.0.5", "RFC 1918, in the middle of its range"),
        ("192.168.1.1", "RFC 1918"),
        ("100.64.0.1", "carrier-grade NAT, which ipaddress does not call private"),
        ("224.0.0.1", "multicast"),
        ("255.255.255.255", "broadcast"),
        ("::1", "IPv6 loopback"),
        ("fe80::1", "IPv6 link-local"),
        ("fd00::1", "IPv6 unique local"),
        ("::ffff:169.254.169.254", "the metadata address in IPv6 clothing"),
        ("::ffff:127.0.0.1", "loopback in IPv6 clothing"),
        ("64:ff9b::7f00:1", "loopback reached through NAT64"),
    ],
)
def test_an_address_inside_the_network_is_refused(address: str, what: str) -> None:
    """Every one of these is a host RAVEL runs beside, not a research source."""
    assert not addressing.is_public(address), what
    assert address in refuses("https://example.org/x", address)


@pytest.mark.parametrize(
    "address", [PUBLIC, "1.1.1.1", "142.250.185.78", "2606:4700:4700::1111", "2001:4860:4860::8888"]
)
def test_a_public_address_is_allowed(address: str) -> None:
    """The guard is a policy about addresses, not a ban on fetching."""
    assert addressing.is_public(address)
    addressing.guard("https://example.org/x", resolver=resolver_for(address))


def test_an_address_that_cannot_be_parsed_is_not_public() -> None:
    """ "May I connect to this" for a string nobody can interpret is no.

    The alternative is a check that silently passes whatever it does not
    understand, which is the shape of most bypasses.
    """
    for nonsense in ("", "not-an-address", "999.1.1.1", "1.2.3.4.5", "localhost"):
        assert not addressing.is_public(nonsense)


@pytest.mark.parametrize(
    "address",
    [
        "198.18.56.203",
        "198.19.255.1",
        "192.0.2.10",
        "203.0.113.10",
        "2001:db8::1",
    ],
)
def test_a_range_that_holds_no_service_is_not_treated_as_internal(address: str) -> None:
    """Reserved space is not the same as internal space.

    These ranges host nothing, so refusing them protects nothing — and one of
    them is what software that intercepts DNS hands out as a placeholder for
    every name it resolves. Blocking that range would leave RAVEL unable to
    reach the open Internet on such a host, which is a real deployment this
    project runs on. `SECURITY_NOTES.md` records what that costs.
    """
    assert addressing.is_public(address)
    addressing.guard("https://example.org/x", resolver=resolver_for(address))


@pytest.mark.parametrize(
    "host",
    [
        "metadata.google.internal",
        "metadata.google.internal.",  # the fully qualified spelling
        "Metadata.Goog",  # case is not a defence
        "metadata.azure.com",
        "instance-data",
        "sub.metadata.goog",
    ],
)
def test_the_metadata_service_is_refused_by_name(host: str) -> None:
    """The check that still works when the address check cannot.

    On a host whose DNS is intercepted, *every* name answers with a placeholder
    address, so the metadata service is indistinguishable from a journal by
    address alone. The name is what is left, and these names have no other
    purpose than to denote it.
    """
    reason = refuses(f"https://{host}/latest/meta-data/", "198.18.56.250")

    assert "instance metadata service" in reason


def test_a_name_that_merely_contains_a_metadata_word_is_not_refused() -> None:
    """Matched on the name, not on a substring of it.

    `metadatastudies.example.org` is a journal. A check that refused it would
    cost a real source to catch nothing.
    """
    addressing.guard("https://metadatastudies.example.org/x", resolver=resolver_for(PUBLIC))
    addressing.guard("https://example.org/metadata.goog", resolver=resolver_for(PUBLIC))


# --------------------------------------------------------------------------
# The shapes an attacker chooses
# --------------------------------------------------------------------------


def test_a_public_url_that_redirects_into_the_network_is_refused() -> None:
    """The bypass a check made once, in front of the client, would miss.

    `https://example.org/x` is a fine URL to fetch. What the server answers —
    `302` to the metadata service — is not, and nobody wrote that address in the
    request RAVEL made. The guard sits at the transport, so the hop is checked
    even though the URL was not.
    """
    fetcher = Fetcher(
        Settings(env="test", research_contact_email="r@example.org", RAVEL_POSTGRES_DSN=None),
        resolver=resolver_for(PUBLIC),
    )

    with respx.mock:
        respx.get("https://example.org/x").mock(
            return_value=httpx.Response(
                302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
            )
        )
        with pytest.raises(UnsafeURL) as raised:
            fetcher.get("https://example.org/x")

    assert "169.254.169.254" in raised.value.reason


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("file:///etc/passwd", "file"),
        ("data:text/html,<h1>invented</h1>", "data"),
        ("ftp://example.org/paper.pdf", "ftp"),
        ("gopher://example.org/", "gopher"),
        ("javascript:alert(1)", "javascript"),
    ],
)
def test_a_scheme_that_is_not_http_is_refused(url: str, expected: str) -> None:
    """A `file://` URL is the one that would read the host's disk.

    Playwright will navigate to it and return the contents as a document, which
    would then be hashed, stored, and citable exactly like a retrieved source.
    The scheme check is what stops that, and it stops `data:` and the rest on
    the way past.
    """
    reason = refuses(url, PUBLIC)

    assert expected in reason
    assert "http and https only" in reason


def test_a_url_with_no_host_is_refused() -> None:
    assert "names no host" in refuses("https:///path", PUBLIC)


def test_a_name_with_one_public_and_one_private_address_is_refused() -> None:
    """The case a first-address check gets wrong.

    A name that answers with both is a name that reaches both, and which one
    the connection uses is the resolver's choice rather than RAVEL's. Checking
    all of them is what makes the answer not depend on that.
    """
    reason = refuses("https://mixed.example.org/x", PUBLIC, "127.0.0.1")

    assert "127.0.0.1" in reason


def test_a_name_that_does_not_resolve_is_not_a_refusal() -> None:
    """A DNS failure is a transport failure, and is reported as one.

    Refusing here would turn an outage into a security decision, and the caller
    would record "RAVEL would not fetch this" about a source that was merely
    unreachable.
    """
    addressing.guard("https://gone.example.org/x", resolver=lambda host: [])
    assert addressing.resolve("this-name-does-not-exist.invalid") == ()


def test_a_literal_address_is_checked_without_being_resolved() -> None:
    """A literal needs no DNS, so none is attempted.

    The resolver raises if it is called, which is how the test knows the literal
    path was taken rather than that the answer happened to be the same.
    """

    def never_called(host: str) -> list[str]:
        raise AssertionError(f"the resolver was asked about {host!r}")

    addressing.guard("https://93.184.216.34/x", resolver=never_called)
    with pytest.raises(UnsafeURL):
        addressing.guard("https://169.254.169.254/x", resolver=never_called)


def test_a_port_does_not_confuse_the_host_check() -> None:
    """`http://localhost:7233` is Temporal, and the port is not the host."""
    assert "localhost" in refuses("http://localhost:7233/", "127.0.0.1")


def test_credentials_in_a_url_do_not_change_which_host_is_checked() -> None:
    """`https://example.org@169.254.169.254/` names one host, and it is not the first.

    A check that read the authority by splitting on `@` the wrong way round
    would validate `example.org` and then connect to the metadata service.
    """
    reason = refuses("https://example.org@169.254.169.254/x", "169.254.169.254")

    assert "169.254.169.254" in reason


# --------------------------------------------------------------------------
# The guards on the two paths that make requests
# --------------------------------------------------------------------------


def test_the_fetcher_refuses_before_it_makes_a_request() -> None:
    """No request is attempted, so nothing about the refusal is observable.

    respx asserts that everything mocked was called; nothing is mocked here, so
    a request of any kind would fail the test rather than be silently answered.
    """
    fetcher = Fetcher(
        Settings(env="test", research_contact_email="r@example.org", RAVEL_POSTGRES_DSN=None),
        resolver=resolver_for("169.254.169.254"),
    )

    with respx.mock, pytest.raises(UnsafeURL):
        fetcher.get("http://metadata.example.org/latest/meta-data/")


def test_a_refusal_is_not_recorded_as_a_source() -> None:
    """The distinction between "could not read" and "would not request".

    A restricted `Retrieval` becomes an `EvidenceSource`. Recording a refused
    URL that way would write the attacker's probe into the Evidence Ledger as a
    source RAVEL tried to read — which is the record the probe was for.
    """
    fetcher = Fetcher(
        Settings(env="test", research_contact_email="r@example.org", RAVEL_POSTGRES_DSN=None),
        resolver=resolver_for("127.0.0.1"),
    )

    with pytest.raises(UnsafeURL):
        fetcher.get("http://localhost:7233/namespaces")


def test_the_browser_refuses_the_same_urls_without_starting_a_browser() -> None:
    """The policy is one policy, applied on both paths.

    Chromium cannot be launched in this environment, so if the guard ran after
    the browser was asked for, this would raise `BrowserUnavailable` instead of
    `UnsafeURL` — which is exactly the difference the test asserts.
    """
    navigator = BrowserNavigator(
        Settings(env="test", research_contact_email="r@example.org", RAVEL_POSTGRES_DSN=None),
        resolver=resolver_for(PUBLIC),
    )
    try:
        with pytest.raises(UnsafeURL):
            navigator.open("file:///etc/passwd")
        with pytest.raises(UnsafeURL):
            navigator.links("http://169.254.169.254/")
    finally:
        navigator.close()
