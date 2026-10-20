"""What may leave a browser sandbox.

A browser is an egress engine. It follows redirects, loads subresources, opens
websockets, and resolves names on its own; a per-navigation URL check cannot
enumerate what the page will ask for next. The controlled proxy is therefore the
only route off the browser's internal Docker network, and it decides every
connection on its own terms:

1. **The target must be on the operator's exact allowlist.** Entries are
   `host:port`, compared case-insensitively with a trailing dot removed. There
   is no suffix rule and no wildcard: an allowlist that admits `*.example.com`
   is an allowlist that admits whatever an attacker registers under it.
2. **The name must not denote an instance metadata service.** Checked by name as
   well as by address, because a host with intercepted DNS answers every name
   with a placeholder and only the name carries the signal.
3. **Every address the name resolves to must be public.** All of them, not the
   first: a name with one public and one private answer reaches both, and the
   proxy must not make the choice the resolver's ordering makes.
4. **The client must be on the project's internal network.** The proxy is also
   attached to an egress network so it can reach the internet; without a client
   check, anything else on that egress network could use it as an open proxy.
   The supervisor passes the internal subnet as configuration, and a peer
   outside it is refused before a request line is read.

The policy is pure and lives here so it can be tested without a socket. The
proxy server in `proxy.py` applies it; the browser helper applies the scheme and
metadata checks again inside the container, because defence in depth is cheaper
than deciding which of two checks is redundant.
"""

from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from ravel.sandbox import addressing

#: Ports that are never a research source even when allowlisted. An operator
#: can still name one explicitly, but the default policy refuses the ones that
#: are infrastructure in every deployment (SSH, PostgreSQL, Redis, Temporal,
#: Docker's own API, the metadata service's usual ports).
FORBIDDEN_PORTS: frozenset[int] = frozenset({22, 2375, 2376, 5432, 6379, 7233, 9000, 9001})

#: Literal addresses of the well-known instance metadata services. The name
#: check catches the DNS names; an operator who allowlists the address directly
#: would otherwise reach the service through the public-address check, which
#: refuses it but does not say why. Naming it makes the refusal actionable.
_METADATA_ADDRESSES: frozenset[str] = frozenset({"169.254.169.254", "fd00:ec2::254"})


class EgressRefused(RuntimeError):
    """A proxied connection the policy will not make."""

    def __init__(self, target: str, reason: str) -> None:
        super().__init__(f"refusing egress to {target}: {reason}")
        self.target = target
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Target:
    """A host and port a proxy request names."""

    host: str
    port: int

    @property
    def key(self) -> str:
        return f"{self.host.lower().rstrip('.')}:{self.port}"


def parse_target(value: str, *, default_port: int | None = None) -> Target:
    """Parse one allowlist entry or request target.

    Accepted forms are `host:port`, a bare host when `default_port` is given,
    and a bracketed IPv6 literal. A scheme is refused: an allowlist entry that
    says `https://example.com` is ambiguous about the port and invites a reader
    to believe TLS is enforced here, which it is not — this proxy tunnels bytes.
    """
    text = value.strip()
    if not text:
        raise EgressRefused(value, "the target is empty")
    if "://" in text:
        raise EgressRefused(value, "an allowlist entry is host:port, not a URL")
    if any(character in text for character in "/?#@"):
        raise EgressRefused(value, "the target contains a path, query, or userinfo")

    host: str
    port_text: str | None
    if text.startswith("["):
        closing = text.find("]")
        if closing < 0:
            raise EgressRefused(value, "an IPv6 target is missing its closing bracket")
        host = text[1:closing]
        rest = text[closing + 1 :]
        port_text = rest[1:] if rest.startswith(":") else None
        if rest and not rest.startswith(":"):
            raise EgressRefused(value, "unexpected text after an IPv6 literal")
    elif text.count(":") == 1:
        host, port_text = text.rsplit(":", 1)
    elif text.count(":") > 1:
        # A bare IPv6 literal. A port cannot be distinguished without brackets.
        host, port_text = text, None
    else:
        host, port_text = text, None

    if not host or not host.strip():
        raise EgressRefused(value, "the target names no host")
    if port_text is None:
        if default_port is None:
            raise EgressRefused(
                value, "the target must name a port; this proxy has no default port"
            )
        port = default_port
    else:
        try:
            port = int(port_text)
        except ValueError as exc:
            raise EgressRefused(value, "the port is not a number") from exc
    if not 1 <= port <= 65535:
        raise EgressRefused(value, "the port is outside 1..65535")
    return Target(host=host, port=port)


def parse_allowlist(entries: str | list[str] | tuple[str, ...]) -> tuple[Target, ...]:
    """An operator allowlist from a comma-separated string or a sequence.

    Duplicates are collapsed and order is preserved. An empty entry is refused
    rather than ignored: a trailing comma in a deployment file would otherwise
    silently permit nothing when the operator believed they had permitted
    something.
    """
    raw = entries.split(",") if isinstance(entries, str) else list(entries)
    targets: list[Target] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise EgressRefused(entry, "an allowlist may not contain an empty entry")
        target = parse_target(entry)
        if target.key not in seen:
            seen.add(target.key)
            targets.append(target)
    return tuple(targets)


def resolve_public(target: Target) -> tuple[str, ...]:
    """Resolve a target and refuse if any answer is not a public address.

    A literal address needs no DNS. A name that does not resolve is refused
    here rather than passed on: unlike the pre-flight check in `addressing`,
    the proxy is the component that must make the connection, and "the name did
    not resolve" at this point means it cannot make it. Refusing is the honest
    answer and the caller sees a proxy error, not a hung socket.

    Raises:
        EgressRefused: The name denotes metadata, does not resolve, or resolves
            to anything that is not public.
    """
    if addressing.names_the_metadata_service(target.host):
        raise EgressRefused(
            target.key,
            f"{target.host} is the instance metadata service; it holds the host's "
            "credentials and is not a research source",
        )
    try:
        ipaddress.ip_address(target.host)
    except ValueError:
        try:
            infos = socket.getaddrinfo(target.host, target.port, proto=socket.IPPROTO_TCP)
        except socket.gaierror as exc:
            raise EgressRefused(target.key, f"the name does not resolve: {exc}") from exc
        addresses = sorted({str(info[4][0]) for info in infos})
    else:
        addresses = (target.host,)
    if not addresses:
        raise EgressRefused(target.key, "the name resolved to no address")
    for address in addresses:
        if address in _METADATA_ADDRESSES:
            raise EgressRefused(
                target.key,
                f"{target.host} resolves to {address}, an instance metadata service "
                "address; it holds the host's credentials and is not a research source",
            )
        if not addressing.is_public(address):
            raise EgressRefused(
                target.key,
                f"{target.host} resolves to {address}, which is not a public address; "
                "a browser sandbox does not reach the deployment's own network",
            )
    return tuple(addresses)


@dataclass(frozen=True, slots=True)
class ProxyPolicy:
    """The allowlist and the deployment facts an egress decision depends on."""

    allowlist: tuple[Target, ...]
    client_cidr: str | None = None
    forbidden_ports: frozenset[int] = FORBIDDEN_PORTS

    @classmethod
    def from_config(
        cls, entries: str | list[str] | tuple[str, ...], *, client_cidr: str | None = None
    ) -> ProxyPolicy:
        return cls(allowlist=parse_allowlist(entries), client_cidr=client_cidr)

    def allows_client(self, peer: str) -> bool:
        """Whether a TCP peer is on the project's internal network."""
        if self.client_cidr is None:
            return True
        try:
            address = ipaddress.ip_address(peer)
            network = ipaddress.ip_network(self.client_cidr, strict=False)
        except ValueError:
            return False
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            address = mapped
        return address in network

    def target_for(self, host: str, port: int) -> Target:
        """Validate a requested host and port against the policy.

        The allowlist is the first check because it is the operator's decision
        and the one a refusal should name. The address policy is checked on
        every connection even for an allowlisted name: DNS can change after the
        allowlist was written, and the allowlist says which name, not which
        address.
        """
        target = Target(host=host, port=port)
        if target.port in self.forbidden_ports:
            raise EgressRefused(
                target.key,
                f"port {port} is infrastructure in every RAVEL deployment; a browser "
                "does not need it and the proxy will not carry it",
            )
        if target.key not in {entry.key for entry in self.allowlist}:
            raise EgressRefused(
                target.key,
                "the target is not on the operator's browser allowlist",
            )
        return target

    def check(self, host: str, port: int) -> tuple[str, ...]:
        """The addresses a permitted connection may use.

        Raises:
            EgressRefused: The target is not allowlisted, or resolves somewhere
                that is not public.
        """
        target = self.target_for(host, port)
        return resolve_public(target)

    def check_url(self, url: str) -> tuple[Target, tuple[str, ...]]:
        """The target and addresses for an absolute-form proxy request."""
        parts = urlsplit(url)
        if parts.scheme.lower() not in addressing.ALLOWED_SCHEMES:
            raise EgressRefused(url, f"scheme {parts.scheme!r} is not http or https")
        if not parts.hostname:
            raise EgressRefused(url, "the URL names no host")
        port = parts.port or (443 if parts.scheme.lower() == "https" else 80)
        addresses = self.check(parts.hostname, port)
        return Target(host=parts.hostname, port=port), addresses
