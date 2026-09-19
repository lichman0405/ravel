"""Opening a URL, and reporting honestly what happened.

The single most important property of this module is that it never produces
text it did not receive. A paywall, a login wall, a rate limit, a legal block
and a network failure are five different facts about the world, and each is
recorded as itself. The failure mode this module exists to prevent is the
plausible one: an agent that cannot read a paper and writes down what it is
sure the paper says. RAVEL's answer is that the retrieval carries no body, no
hash, and no excerpt, and the source is registered as restricted.

Two conventions in here are not arbitrary:

- **The User-Agent names RAVEL and a contact address.** Crossref and OpenAlex
  both route identified traffic to a faster, more reliable pool and ask for
  exactly this; an anonymous client is the one that gets throttled. RAVEL
  therefore refuses to fetch without a contact address configured rather than
  quietly joining the anonymous pool.
- **Size is capped while streaming, not after.** A content-length header is a
  claim, and a source that streams more than it declared is exactly the case a
  cap exists for.
"""

from __future__ import annotations

import collections.abc
import logging
import typing
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime

import httpcore
import httpx

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.research import addressing
from ravel.research.leads import Retrieval
from ravel.state.store import hash_chunks

logger = logging.getLogger(__name__)

#: Bodies larger than this are not held in memory. A paper PDF is a few
#: megabytes; anything past this is either a dataset or a mistake, and either
#: way it belongs in the artifact store rather than in a record.
MAX_INLINE_BYTES = 8 * 1024 * 1024

#: How much of a body is kept as a verbatim excerpt. Enough to see that the
#: page is about what the lead claimed; short enough that the excerpt is never
#: mistaken for the source.
EXCERPT_CHARS = 600

#: Statuses that mean "the thing exists but you may not have it". Mapped to the
#: domain's own vocabulary rather than collapsed into a generic failure, because
#: "this paper is behind a paywall" and "this server is down" call for different
#: responses from a research agent.
_AUTH_STATUSES = frozenset({401, 403})
_PAYMENT_STATUSES = frozenset({402})
_LEGAL_STATUSES = frozenset({451})
_THROTTLE_STATUSES = frozenset({429, 503})

#: What RAVEL will look inside for a title. Anything else — a PDF, a dataset,
#: an image — has no title RAVEL can read, so it reports none.
_MARKUP_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml")


class FetchError(RuntimeError):
    """A retrieval could not be attempted at all.

    Distinct from a `Retrieval` whose `access_status` is not OK: this is a
    fault in RAVEL's configuration or in the request itself, not a fact about
    the source. A URL RAVEL cannot even ask for is a bug; a URL that answered
    403 is a finding.
    """


#: The httpcore failures that mean "the request could not be made" rather
#: than "the source said something". httpx translates these into its own
#: hierarchy when a request goes through `httpx.HTTPTransport`; the transport
#: here is built on httpcore directly — see `_GuardedTransport` — so the list
#: is written out. Every one of them is a fact about RAVEL's ability to reach
#: the network, which is the case `_request` reports as a limited retrieval.
_UNREACHABLE = (
    httpcore.NetworkError,
    httpcore.TimeoutException,
    httpcore.ProtocolError,
    httpcore.ProxyError,
    httpcore.UnsupportedProtocol,
)


class _PinnedBackend(httpcore.SyncBackend):
    """A network backend that connects to the address RAVEL checked.

    This is where the check becomes a connection rather than a prediction.
    `addressing.address_for` resolves a name and refuses it unless every
    address is public; the request that follows resolves the name *again*, and
    a name whose answers change between the two reaches an address nothing
    checked. Connecting to the validated address removes the second resolution:
    the only DNS lookup in the path is the one whose answer was checked.

    The name is not lost. httpcore still knows the host and port the request is
    for, and uses them for the `Host` header, for the TLS server name, and for
    certificate verification — so what is sent is identical to what would have
    been sent, and only the address it was sent to was chosen here rather than
    by the resolver.

    Only `connect_tcp` is overridden. Reading, writing, and closing are the
    parent's, because they are about the socket that now exists rather than
    about which one it should be.
    """

    def __init__(self, resolver: addressing.Resolver) -> None:
        self._resolver = resolver

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.NetworkStream:
        """Connect to the host, at an address that was checked first."""
        addresses = addressing.address_for(host, self._resolver)
        if not addresses:  # pragma: no cover - connect_tcp is never given a bare name
            raise addressing.UnsafeURL(host, "no address to connect to")
        return super().connect_tcp(
            addresses[0],
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )


class _GuardedTransport(httpx.BaseTransport):
    """An HTTP transport that refuses to carry a request RAVEL may not make.

    Built on `httpcore.ConnectionPool` directly rather than on
    `httpx.HTTPTransport`, because that is the only way to give the pool a
    network backend: `httpx.HTTPTransport` builds its own and does not accept
    one. What is given up by not using it is the translation of `httpcore`
    errors into `httpx` ones, which `_UNREACHABLE` writes out instead — the
    same set of failures, reported the same way.

    Two things are refused here, and they overlap on purpose:

    - at `connect_tcp`, an address that is not public, which is the check that
      actually binds because it is the address the socket goes to;
    - at `handle_request`, the scheme, the host, and the metadata-service
      names — the rules that are about the URL rather than about where it
      leads, and that a connection-level check would not see.

    Checking per request rather than once before the call is also what makes
    redirects safe: httpx follows a redirect by sending another request through
    the transport, so a hop to a link-local address is checked even though
    nobody wrote it in the original URL.

    `addressing.UnsafeURL` propagates from here rather than being converted into
    a restricted `Retrieval`, because a refused request is a decision RAVEL made
    and not a fact about a source. Recording it as a source would write the
    attacker's probe into the Evidence Ledger.
    """

    def __init__(self, resolver: addressing.Resolver) -> None:
        self._resolver = resolver
        self._pool = httpcore.ConnectionPool(
            ssl_context=httpx.create_ssl_context(),
            network_backend=_PinnedBackend(resolver),
            # No keep-alive. A pooled connection is keyed by the address it was
            # opened to, and two hosts behind one address would then share a
            # connection whose TLS session was established for the first of
            # them. RAVEL fetches a handful of URLs per call, so a handshake
            # each is a price worth paying for not having to reason about it.
            max_connections=10,
            max_keepalive_connections=0,
        )

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        addressing.guard(str(request.url), resolver=self._resolver)
        response = self._pool.handle_request(
            httpcore.Request(
                method=request.method,
                url=httpcore.URL(
                    scheme=request.url.raw_scheme,
                    host=request.url.raw_host,
                    port=request.url.port,
                    target=request.url.raw_path,
                ),
                headers=request.headers.raw,
                content=request.stream,
                extensions=request.extensions,
            )
        )
        stream = response.stream
        # httpcore types this as either, because the same class serves the
        # async pool. A synchronous pool handing back an async stream would be
        # a fault in httpcore, and saying so is better than a type: ignore that
        # would hide the day it happens.
        assert isinstance(stream, collections.abc.Iterable), (
            "a synchronous connection pool answered with an asynchronous stream"
        )
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=_PoolStream(stream),
            extensions=response.extensions,
            request=request,
        )

    def close(self) -> None:
        self._pool.close()


class _PoolStream(httpx.SyncByteStream):
    """httpcore's response stream, as an httpx one.

    Two lines of adaptation, and the reason `httpx.HTTPTransport` is worth not
    using is that this is all it costs.
    """

    def __init__(self, stream: typing.Iterable[bytes]) -> None:
        self._stream = stream

    def __iter__(self) -> typing.Iterator[bytes]:
        yield from self._stream

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if close is not None:
            close()


@dataclass(frozen=True, slots=True)
class FetchPolicy:
    """How much a fetcher is allowed to do."""

    timeout_seconds: float = 30.0
    max_bytes: int = MAX_INLINE_BYTES
    follow_redirects: bool = True
    #: Whether to keep the bytes in the returned record. A caller that intends
    #: to stream them into the artifact store does not need a second copy.
    keep_body: bool = True


class Fetcher:
    """Real HTTP, with the provenance RAVEL needs recorded on the way through."""

    def __init__(
        self,
        settings: Settings,
        policy: FetchPolicy | None = None,
        *,
        resolver: addressing.Resolver = addressing.resolve,
    ) -> None:
        self.settings = settings
        self.policy = policy or FetchPolicy()
        self.user_agent = user_agent(settings)
        # Injectable so that a test of what RAVEL will fetch does not need DNS,
        # and so that the policy is exercised on the addresses that matter
        # rather than on whatever a name happens to resolve to today.
        self.resolver = resolver

    def get(self, url: str) -> Retrieval:
        """Open a URL and report what came back.

        Never raises for a source RAVEL simply could not read: that is a
        `Retrieval` with a non-OK status. Raises only when the request could
        not be made or the transport failed in a way that is RAVEL's problem.

        Raises:
            addressing.UnsafeURL: The URL is one RAVEL will not request at all.
        """
        return self._request("GET", url)

    def head(self, url: str) -> Retrieval:
        """Ask about a URL without downloading it.

        Used to check whether a source is still reachable without paying for
        the whole body. `content_hash` is None on the result even when the
        source is OK, because nothing was read.
        """
        return self._request("HEAD", url)

    def _request(self, method: str, url: str) -> Retrieval:
        try:
            with httpx.Client(
                timeout=self.policy.timeout_seconds,
                follow_redirects=self.policy.follow_redirects,
                headers={"User-Agent": self.user_agent, "Accept": "*/*"},
                # The guard sits at the transport rather than in front of the
                # call because a redirect is a second request to a URL nobody
                # chose either: a public address that redirects to a link-local
                # one is the same attack with one extra hop. Every hop goes
                # through the transport, so every hop is checked.
                transport=_GuardedTransport(self.resolver),
            ) as client, client.stream(method, url) as response:
                return self._from_response(response, url, read_body=method == "GET")
        except (httpx.HTTPError, *_UNREACHABLE) as exc:
            # The request never produced a response. That is RAVEL being unable
            # to reach the network, not a statement about the source, so it is
            # reported as a limited retrieval with the transport's own words.
            return Retrieval(
                requested_url=url,
                final_url=url,
                access_status=AccessStatus.ACCESS_LIMITED,
                retrieved_at=datetime.now(UTC),
                note=f"{type(exc).__name__}: {exc}"[:500],
            )

    def _from_response(
        self, response: httpx.Response, requested_url: str, *, read_body: bool
    ) -> Retrieval:
        final_url = str(response.url)
        # `retrieved_at` is stamped as the response is being read, which is the
        # moment the bytes exist. Not when the request was sent: a source that
        # took thirty seconds to answer was read thirty seconds later, and the
        # record should say so.
        media_type = response.headers.get("content-type", "").split(";")[0].strip() or None
        base = {
            "requested_url": requested_url,
            "final_url": final_url,
            "retrieved_at": datetime.now(UTC),
            "status_code": response.status_code,
            "media_type": media_type,
        }

        restricted = _restriction_for(response)
        if restricted is not None:
            status, note = restricted
            return Retrieval(
                **base,
                access_status=status,
                note=note,
                # A restricted response may still have a title, and recording
                # it is not the same as reading the source: it is what the
                # server said about a page it would not show.
                title=title_of(b"".join(_capped(response.iter_bytes(), 64 * 1024)), media_type),
            )

        if response.status_code >= 400:
            return Retrieval(
                **base,
                access_status=AccessStatus.ACCESS_LIMITED,
                note=f"HTTP {response.status_code}",
            )

        chunks = list(_capped(response.iter_bytes(), self.policy.max_bytes))
        body = b"".join(chunks)
        # Hashed by the artifact store's own helper, so that a source's hash and
        # the hash of its stored snapshot are the same string. Two hash
        # conventions in one ledger would make the comparison that verification
        # rests on fail for a reason that has nothing to do with the source.
        digest, _ = hash_chunks([body])

        if not read_body:
            # A HEAD tells us the source is there and what it is; it does not
            # tell us what it says, so it cannot become evidence.
            return Retrieval(
                **base,
                access_status=AccessStatus.OK,
                size_bytes=len(body) or _int_header(response, "content-length"),
                title=title_of(body, media_type),
            )

        return Retrieval(
            **base,
            access_status=AccessStatus.OK,
            content_hash=digest,
            size_bytes=len(body),
            title=title_of(body, media_type),
            body=body if self.policy.keep_body else None,
            excerpt=excerpt_of(body, media_type),
        )


def user_agent(settings: Settings) -> str:
    """The User-Agent RAVEL identifies itself with.

    Crossref, OpenAlex, and the NCBI services all ask for a contact address in
    the agent string and give identified clients better service. RAVEL is a
    research tool that makes many requests, so it says who it is.

    Raises:
        FetchError: No contact address is configured. Refusing here is
            deliberate: an anonymous client is a worse citizen against every
            one of these services, and the fix is a one-line configuration.
    """
    contact = (settings.research_contact_email or "").strip()
    if not contact:
        raise FetchError(
            "RAVEL_RESEARCH_CONTACT_EMAIL is unset. Crossref, OpenAlex and NCBI ask "
            "identified clients to supply a contact address and give anonymous ones "
            "worse service; set it rather than fetching anonymously."
        )
    return f"RAVEL/0.1 (research source gateway; mailto:{contact})"


def _restriction_for(response: httpx.Response) -> tuple[AccessStatus, str] | None:
    """Whether a response means "you may not have this", and which kind.

    Ordered by how specific the signal is. A 403 carrying a paywall marker is a
    paywall; a bare 403 is an authentication problem. The distinction reaches
    the Evidence Ledger, where it tells a later reader whether buying access,
    logging in, or giving up is the right response.
    """
    status = response.status_code
    if status in _LEGAL_STATUSES:
        return AccessStatus.POLICY_BLOCKED, "HTTP 451: unavailable for legal reasons"
    if status in _PAYMENT_STATUSES:
        return AccessStatus.PAYWALLED, "HTTP 402: payment required"
    if status in _THROTTLE_STATUSES:
        return AccessStatus.ACCESS_LIMITED, f"HTTP {status}: rate limited"
    if status in _AUTH_STATUSES:
        if _looks_paywalled(response):
            return AccessStatus.PAYWALLED, f"HTTP {status}: paywall"
        return AccessStatus.AUTH_REQUIRED, f"HTTP {status}: authentication required"
    return None


def _looks_paywalled(response: httpx.Response) -> bool:
    """Whether a refusal page says "buy this" rather than "log in".

    Read from headers only, so it costs nothing and cannot accidentally consume
    a body that the caller may still want.
    """
    marker = " ".join(
        [
            response.headers.get("x-paywall", ""),
            response.headers.get("www-authenticate", ""),
            response.headers.get("location", ""),
            response.headers.get("x-frame-options", ""),
        ]
    ).lower()
    return any(word in marker for word in ("paywall", "subscription", "purchase", "access-denied"))


def _capped(chunks: Iterator[bytes], limit: int) -> Iterator[bytes]:
    """Yield at most `limit` bytes, stopping the transfer once it is reached.

    The cap is enforced while streaming rather than from `content-length`,
    because that header is the server's claim about itself. A response that
    declares one size and sends another is exactly the case a cap is for.
    """
    total = 0
    for chunk in chunks:
        if total + len(chunk) > limit:
            yield chunk[: limit - total]
            return
        total += len(chunk)
        yield chunk


def _int_header(response: httpx.Response, name: str) -> int | None:
    raw = response.headers.get(name)
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def title_of(body: bytes, media_type: str | None) -> str:
    """A source's own title, taken from the source.

    Only HTML and XML are parsed, and only for a title. Everything else — a
    PDF, a CSV, an image — has no title RAVEL can read without inventing one,
    so it gets none, and the gateway falls back to what the lead claimed while
    recording that the claim came from the lead.
    """
    if not body or media_type not in _MARKUP_TYPES:
        return ""
    head = body[: 256 * 1024].decode("utf-8", errors="replace")
    lowered = head.lower()
    for opener, closer in (("<title>", "</title>"), ("<dc:title>", "</dc:title>")):
        start = lowered.find(opener)
        if start == -1:
            continue
        end = lowered.find(closer, start)
        if end == -1:
            continue
        return " ".join(head[start + len(opener) : end].split())[:500]
    return ""


def excerpt_of(body: bytes, media_type: str | None) -> str:
    """A short verbatim passage of what was read.

    Verbatim is the whole point: an excerpt that a later reader can search for
    in the stored snapshot is checkable, and a summary is not. Tags are
    stripped only to make the text readable, never rewritten or completed.
    """
    if not body or media_type not in ("text/html", "application/xhtml+xml"):
        return ""
    text = body[: 512 * 1024].decode("utf-8", errors="replace")
    for tag in ("script", "style", "noscript"):
        while (start := text.find(f"<{tag}")) != -1:
            end = text.find(f"</{tag}>", start)
            text = text[:start] + (" " if end == -1 else text[end + len(tag) + 3 :])
    stripped = " ".join(strip_tags(text).split())
    return stripped[:EXCERPT_CHARS]


def strip_tags(text: str) -> str:
    out: list[str] = []
    inside = False
    for character in text:
        if character == "<":
            inside = True
        elif character == ">":
            inside = False
            out.append(" ")
        elif not inside:
            out.append(character)
    return "".join(out)
