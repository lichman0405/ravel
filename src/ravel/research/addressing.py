"""Which addresses RAVEL is willing to talk to.

RAVEL fetches URLs it did not choose. A lead comes from a search provider, a
link comes from a page, a request is assembled by an agent — and every one of
those is a string that arrived from outside. Handing such a string to an HTTP
client is a request made on behalf of whoever wrote it, from inside the network
RAVEL runs in. On a cloud host that network contains an instance metadata
service, and on any host it contains the database, the object store, and the
scheduler, none of which are research sources.

The concrete failures this prevents:

- `http://169.254.169.254/latest/meta-data/iam/security-credentials/` — the
  instance metadata service, which answers with credentials on the major clouds.
  A retrieval of it would be hashed, stored as a snapshot, and citable.
- `http://localhost:7233/...` and friends — Temporal, and behind it PostgreSQL
  and MinIO. A fetch is a request RAVEL authenticates to nothing, but
  reachability alone is information, and some of these services answer.
- `file:///etc/passwd` — a browser will read it, and a `Retrieval` carrying the
  bytes would be indistinguishable from a retrieved source.

Two things are checked, because either alone is insufficient. The **scheme** must
be http or https, which is what a research source is addressed by. Every
**resolved address** for the host must be public — resolving rather than
matching hostnames, because `metadata.google.internal` is not a suspicious
string but resolves somewhere that is. A short list of **names** is refused
outright as well, for hosts whose DNS answers with a placeholder for every name
and so makes the address say nothing.

**A check in front of the request is not enough on its own**, because the
request resolves the name a second time and an authoritative server that
answers differently the second time reaches an address this module never saw.
So `address_for` returns the addresses it validated, and the HTTP client
connects to one of *them* rather than to the name — see `_PinnedBackend` in
`ravel.research.fetching`. The name is still what the request is for: it is
sent as `Host` and as the TLS server name, so the certificate is verified
against the name and virtual hosts still work. What is removed is the second
resolution, which was the only part nobody had checked.

The same trick is not available for `ravel.research.browser`, which drives
Playwright: its connections are made inside a browser process this code does
not own. A page navigated to by a browser is guarded before and after the fact
rather than pinned, and that gap is in `KNOWN_LIMITATIONS.md`.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from collections.abc import Callable, Sequence
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

#: The schemes a research source can be addressed by. Everything else — `file`,
#: `ftp`, `data`, a browser's own `about:` — is not a source RAVEL reads, and
#: `file` in particular would let a URL name a local path.
ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Networks that are not the public internet and that plausibly serve something.
#: Listed explicitly rather than derived from `ipaddress`'s flags, because the
#: flags do not cover everything that matters — `100.64.0.0/10` is neither
#: private nor reserved by Python's reckoning, and it is carrier-grade NAT,
#: which is somebody's internal network.
#:
#: What is *not* here matters as much as what is. The documentation and
#: benchmarking ranges — `192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`,
#: `2001:db8::/32` — are absent, because no service lives on them. So is
#: `198.18.0.0/15`, which is the benchmarking range most commonly used as a DNS
#: placeholder by software that intercepts name resolution and forwards the
#: connection itself. Blocking that range would make RAVEL unable to reach the
#: open Internet on such a host while protecting nothing, since a connection to
#: it is answered by the interceptor rather than by anything internal. That
#: trade-off, and what it costs, is written up in `SECURITY_NOTES.md`.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in (
        "0.0.0.0/8",  # "this network"
        "10.0.0.0/8",  # RFC 1918
        "100.64.0.0/10",  # RFC 6598 carrier-grade NAT
        "127.0.0.0/8",  # loopback
        "169.254.0.0/16",  # link-local, where metadata services live
        "172.16.0.0/12",  # RFC 1918
        "192.0.0.0/24",  # IETF protocol assignments
        "192.168.0.0/16",  # RFC 1918
        "224.0.0.0/4",  # multicast
        "240.0.0.0/4",  # reserved, including 255.255.255.255
        "::/128",  # unspecified
        "::1/128",  # loopback
        "::ffff:0:0/96",  # IPv4-mapped: unwrapped and checked as IPv4
        "64:ff9b::/96",  # NAT64: unwrapped and checked as IPv4
        "100::/64",  # discard-only
        "fc00::/7",  # unique local
        "fe80::/10",  # link-local
        "ff00::/8",  # multicast
    )
)

#: Names whose only purpose is to denote the instance metadata service.
#:
#: The address check is the general defence and catches these whenever the name
#: resolves honestly. This list exists for the case where it does not: a host
#: whose DNS is intercepted answers with a placeholder address for *every* name,
#: so the address says nothing about where the connection goes, and the name is
#: the only signal left. Checked by suffix, so `metadata.google.internal.` and a
#: subdomain of it are both refused.
_METADATA_NAMES = (
    "metadata.google.internal",
    "metadata.goog",
    "metadata.azure.com",
    "metadata.tencentyun.com",
    "metadata.oraclecloud.com",
    "metadata.aliyun.com",
    "instance-data",
    "instance-data.ec2.internal",
)

#: Takes a hostname, returns the addresses it resolves to. Injectable so that a
#: test of the policy does not need DNS, and so that the policy is exercised on
#: the addresses that matter rather than on whatever a name resolves to today.
Resolver = Callable[[str], Sequence[str]]


class UnsafeURL(RuntimeError):
    """A URL RAVEL will not fetch, and the reason.

    Raised rather than returned as a restricted `Retrieval`, and the difference
    is deliberate: "RAVEL may not ask for this" is a decision RAVEL made, not a
    fact about the source. A retrieval would put the URL in the Evidence Ledger
    as a source that was attempted, which is exactly the record an attacker
    probing the internal network would want written.

    A sibling of `FetchError` rather than a subclass of it: that one means RAVEL
    could not fetch, this one means RAVEL would not.
    """

    def __init__(self, url: str, reason: str) -> None:
        super().__init__(f"refusing to fetch {url}: {reason}")
        self.url = url
        self.reason = reason


def resolve(host: str) -> Sequence[str]:
    """Every address a hostname resolves to.

    All of them are returned rather than the first, because a name with one
    public and one private address is a name that reaches both, and checking
    only the address the connection happens to use would make the answer depend
    on the resolver's ordering.

    A name that does not resolve returns nothing, which is not a refusal: the
    request will fail on its own and the failure will be reported as the
    transport error it is. Refusing here would report a DNS outage as a
    security decision.
    """
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        logger.debug("could not resolve %s while checking it: %s", host, exc)
        return ()
    return sorted({str(info[4][0]) for info in infos})


def is_public(address: str) -> bool:
    """Whether an address is on the public internet.

    An address that cannot be parsed is not public. It cannot be checked, and
    the safe answer to "may I connect to this" for a string nobody can interpret
    is no.
    """
    try:
        parsed = ipaddress.ip_address(address)
    except ValueError:
        return False
    # `::ffff:169.254.169.254` is a link-local IPv4 address wearing an IPv6
    # form, and it is exactly the shape that slips past a check written for one
    # family. Unwrapped first so that only IPv4 rules apply to it.
    mapped = getattr(parsed, "ipv4_mapped", None)
    if mapped is not None:
        parsed = mapped
    if parsed.is_loopback or parsed.is_link_local or parsed.is_multicast:
        return False
    if parsed.is_unspecified or parsed.is_reserved:
        return False
    return not any(parsed in network for network in _BLOCKED_NETWORKS)


def names_the_metadata_service(host: str) -> bool:
    """Whether a hostname is one of the well-known names for instance metadata."""
    name = host.lower().rstrip(".")
    return any(name == known or name.endswith(f".{known}") for known in _METADATA_NAMES)


def address_for(host: str, resolver: Resolver = resolve) -> tuple[str, ...]:
    """The addresses a host may be reached at, all of them checked first.

    Returned rather than a single choice, because the caller connects to one
    and the check is about all of them: a name with one public and one private
    address is a name that reaches both, and which one the connection lands on
    would otherwise be the resolver's decision.

    Raises:
        UnsafeURL: No host was given, the host names the instance metadata
            service, or any address it resolves to is not public.
    """
    if not host:
        raise UnsafeURL(host, "the URL names no host")

    # By name as well as by address. On a host whose DNS is intercepted every
    # name answers with a placeholder address, so the address check cannot tell
    # the metadata service from a journal — and the name still can.
    if names_the_metadata_service(host):
        raise UnsafeURL(
            host,
            f"{host} is the instance metadata service; it holds the host's "
            "credentials and is not a research source",
        )

    # Checked before resolving: a literal address needs no DNS, and asking a
    # resolver about `127.0.0.1` is a request that does not need to be made.
    try:
        ipaddress.ip_address(host)
    except ValueError:
        addresses: tuple[str, ...] = tuple(resolver(host))
    else:
        addresses = (host,)

    for address in addresses:
        if not is_public(address):
            raise UnsafeURL(
                host,
                f"{host} resolves to {address}, which is not a public address; "
                "RAVEL reads research sources and does not make requests to its "
                "own network",
            )
    return addresses


def guard(url: str, *, resolver: Resolver = resolve) -> None:
    """Refuse a URL RAVEL must not fetch.

    Raises:
        UnsafeURL: The scheme is not http or https, the URL names no host, the
            host names the instance metadata service, or the host resolves to an
            address that is not on the public internet.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise UnsafeURL(
            url,
            f"the scheme is {scheme or 'missing'!r}; RAVEL fetches http and https "
            "only, because a research source is addressed by a URL and a local "
            "path is not a source",
        )
    address_for(parts.hostname or "", resolver)

