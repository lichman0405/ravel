"""The only way out of a browser sandbox.

A CONNECT/absolute-form HTTP proxy with an exact allowlist, meant to be the
single peer besides the browser on a per-project internal Docker network. It is
started by the sandbox supervisor with two facts in its environment: the
operator's allowlist and the internal network's subnet. Everything else it
needs it derives from the connection.

It speaks the minimum of HTTP proxy that Chromium uses:

- `CONNECT host:port HTTP/1.1` for TLS and for tunnels, answered with
  `200 Connection Established` and then relayed as bytes;
- absolute-form `GET http://host/path HTTP/1.1` for plain HTTP, rewritten to
  origin-form and relayed.

There is no caching, no header rewriting beyond what HTTP requires, and no
authentication at the HTTP layer. The client check is by TCP peer address: the
proxy also sits on an egress network, and without it anything else on that
network would have an open proxy. The supervisor passes the internal subnet.

Every refusal is a `403` with a short reason. A refused browser request shows
up in the page as a network error, which is what an egress filter should look
like to the page; the reason is also logged on the proxy's stderr, which is the
proxy container's log and is owned by the supervisor.

Run with `python3 -m ravel.sandbox.proxy`; configuration is environment-only so
the process can be started without arguments from a container image.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import select
import socket
import socketserver
import sys
from typing import cast

from ravel.sandbox.egress import EgressRefused, ProxyPolicy

logger = logging.getLogger("ravel.sandbox.proxy")

#: A browser request header is small. The limit is generous for cookies and
#: large referrers and small enough that one client cannot make the proxy hold
#: an arbitrary buffer.
MAX_HEADER_BYTES = 64 * 1024

#: How long an idle tunnel may sit before the proxy closes it. A page that
#: keeps a websocket open is not a document RAVEL is waiting for.
TUNNEL_IDLE_SECONDS = 60.0

#: How long to wait for an upstream TCP connection.
UPSTREAM_CONNECT_SECONDS = 15.0

#: Hop-by-hop headers that must not be forwarded by a proxy. `Connection` is
#: replaced with `close` so an upstream that would keep the socket open closes
#: it and the relay ends; a research GET has nothing to reuse it for.
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "proxy-connection",
        "proxy-authorization",
        "keep-alive",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)


class ProxyRequestError(RuntimeError):
    """The client's bytes are not an HTTP proxy request this server speaks."""


def _split_headers(blob: bytes) -> tuple[str, list[tuple[str, str]], bytes]:
    """The request line, header pairs, and any body bytes already read."""
    head, separator, remainder = blob.partition(b"\r\n\r\n")
    if not separator:
        raise ProxyRequestError("the request headers did not end with a blank line")
    try:
        text = head.decode("iso-8859-1")
    except UnicodeDecodeError as exc:  # iso-8859-1 decodes every byte; kept as a guard
        raise ProxyRequestError("the request headers are not decodable") from exc
    lines = text.split("\r\n")
    request_line = lines[0]
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        name, colon, value = line.partition(":")
        if not colon:
            raise ProxyRequestError("a header line has no colon")
        headers.append((name.strip(), value.strip()))
    return request_line, headers, remainder


def _safe_send(sock: socket.socket, payload: bytes) -> None:
    with contextlib.suppress(OSError):
        sock.sendall(payload)


def refuse(sock: socket.socket, reason: str) -> None:
    """Answer with a 403 that says only what the policy already says publicly."""
    body = f"RAVEL sandbox egress refused: {reason}\n".encode()
    _safe_send(
        sock,
        b"HTTP/1.1 403 Forbidden\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body,
    )


def _connect(addresses: tuple[str, ...], port: int) -> socket.socket:
    """Connect to the first validated address that accepts.

    The addresses have already passed `resolve_public`. Connecting by address
    rather than by name removes the second resolution: the name was checked,
    and this connects to what was checked.
    """
    last_error: OSError | None = None
    for address in addresses:
        try:
            return socket.create_connection(
                (address, port), timeout=UPSTREAM_CONNECT_SECONDS
            )
        except OSError as exc:
            last_error = exc
    raise OSError(f"could not connect to any validated address: {last_error}")


def _relay(client: socket.socket, upstream: socket.socket) -> None:
    """Copy bytes both ways until either side closes or the tunnel idles.

    Two directions in one loop rather than two threads: a research request and
    its response are small, and one loop makes the lifetime of the tunnel one
    object that can be closed once.
    """
    client.settimeout(TUNNEL_IDLE_SECONDS)
    upstream.settimeout(TUNNEL_IDLE_SECONDS)
    sockets = [client, upstream]
    try:
        while True:
            readable, _, exceptional = select.select(sockets, [], sockets, TUNNEL_IDLE_SECONDS)
            if exceptional or not readable:
                return
            for source in readable:
                data = source.recv(64 * 1024)
                if not data:
                    return
                destination = upstream if source is client else client
                destination.sendall(data)
    except OSError:
        return


class ProxyHandler(socketserver.BaseRequestHandler):
    """One client connection: read a request, decide, relay or refuse."""

    @property
    def proxy_server(self) -> ProxyServer:
        # `socketserver` types `self.server` as `BaseServer`; this handler is
        # only ever installed on a `ProxyServer`, and the cast states that
        # without widening the base class's attribute.
        return cast(ProxyServer, self.server)

    def handle(self) -> None:
        policy = self.proxy_server.policy
        peer = str(self.client_address[0])
        if not policy.allows_client(peer):
            logger.warning("refused proxy client %s outside %s", peer, policy.client_cidr)
            refuse(self.request, "this client is not on the sandbox's internal network")
            return

        self.request.settimeout(30.0)
        try:
            blob = self._read_headers()
            request_line, headers, body = _split_headers(blob)
            method, target, version = self._parse_request_line(request_line)
            if method.upper() == "CONNECT":
                self._connect_tunnel(target)
            else:
                self._forward_http(method, target, version, headers, body)
        except EgressRefused as exc:
            logger.info("refused egress to %s: %s", exc.target, exc.reason)
            refuse(self.request, exc.reason)
        except ProxyRequestError as exc:
            logger.info("refused a malformed proxy request from %s: %s", peer, exc)
            refuse(self.request, str(exc))
        except (OSError, ValueError) as exc:
            logger.info("proxy request from %s failed: %s", peer, type(exc).__name__)
            refuse(self.request, "the proxy could not complete the request")

    def _read_headers(self) -> bytes:
        buffer = bytearray()
        while b"\r\n\r\n" not in buffer:
            chunk = self.request.recv(8192)
            if not chunk:
                raise ProxyRequestError("the client closed before sending headers")
            buffer.extend(chunk)
            if len(buffer) > MAX_HEADER_BYTES:
                raise ProxyRequestError("the request headers are too large")
        return bytes(buffer)

    @staticmethod
    def _parse_request_line(line: str) -> tuple[str, str, str]:
        parts = line.split(" ")
        if len(parts) != 3:
            raise ProxyRequestError("the request line is not `METHOD TARGET VERSION`")
        method, target, version = parts
        if not method.isalpha():
            raise ProxyRequestError("the method is not alphabetic")
        if version not in {"HTTP/1.0", "HTTP/1.1"}:
            raise ProxyRequestError("only HTTP/1.0 and HTTP/1.1 are proxied")
        return method, target, version

    def _connect_tunnel(self, target: str) -> None:
        from ravel.sandbox.egress import parse_target

        parsed = parse_target(target)
        addresses = self.proxy_server.policy.check(parsed.host, parsed.port)
        upstream = _connect(addresses, parsed.port)
        try:
            _safe_send(self.request, b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self.request.settimeout(None)
            _relay(self.request, upstream)
        finally:
            upstream.close()

    def _forward_http(
        self,
        method: str,
        target: str,
        version: str,
        headers: list[tuple[str, str]],
        body: bytes,
    ) -> None:
        parsed, addresses = self.proxy_server.policy.check_url(target)
        upstream = _connect(addresses, parsed.port)
        try:
            # Origin-form request target: the upstream is an ordinary HTTP
            # server that knows nothing about this proxy.
            from urllib.parse import urlsplit

            split = urlsplit(target)
            path = split.path or "/"
            if split.query:
                path = f"{path}?{split.query}"
            lines = [f"{method} {path} {version}"]
            for name, value in headers:
                if name.lower() in _HOP_BY_HOP:
                    continue
                lines.append(f"{name}: {value}")
            lines.append("Connection: close")
            payload = "\r\n".join(lines).encode("iso-8859-1") + b"\r\n\r\n" + body
            upstream.sendall(payload)
            upstream.settimeout(None)
            _relay(self.request, upstream)
        finally:
            upstream.close()


class ProxyServer(socketserver.ThreadingTCPServer):
    """A threaded proxy bound to all interfaces inside its container."""

    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], policy: ProxyPolicy) -> None:
        self.policy = policy
        super().__init__(address, ProxyHandler)


def policy_from_environment(environ: dict[str, str] | None = None) -> ProxyPolicy:
    """Build the policy the supervisor configured, refusing an empty allowlist.

    An empty allowlist is not "allow nothing by accident": it is a
    configuration mistake, and a proxy that starts with one would look healthy
    while every request failed with the same refusal. Refusing to start makes
    the mistake visible where it was made.
    """
    env = os.environ if environ is None else environ
    allowlist = env.get("RAVEL_PROXY_ALLOWLIST", "")
    if not allowlist.strip():
        raise EgressRefused(
            "allowlist", "RAVEL_PROXY_ALLOWLIST is empty; a browser sandbox needs at "
            "least one operator-approved target"
        )
    return ProxyPolicy.from_config(
        allowlist, client_cidr=env.get("RAVEL_PROXY_CLIENT_CIDR") or None
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="RAVEL sandbox egress proxy")
    parser.add_argument("--port", type=int, default=int(os.environ.get("RAVEL_PROXY_PORT", "3128")))
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="print the parsed policy and exit, without binding a socket",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=os.environ.get("RAVEL_PROXY_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        policy = policy_from_environment()
    except EgressRefused as exc:
        logger.error("proxy configuration refused: %s", exc)
        return 2

    if args.self_check:
        print(
            json.dumps(
                {
                    "allowlist": [entry.key for entry in policy.allowlist],
                    "client_cidr": policy.client_cidr,
                    "port": args.port,
                }
            )
        )
        return 0

    server = ProxyServer(("0.0.0.0", args.port), policy)
    logger.info(
        "sandbox proxy listening on %s with %d allowlisted targets",
        args.port,
        len(policy.allowlist),
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
