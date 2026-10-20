"""Reading a page the way a browser reads it, for the pages HTTP cannot render.

Some sources serve nothing useful to a plain GET: a landing page whose body
arrives empty and gets filled in by script, a supplementary file behind a
click, a PDF viewer that only exists once JavaScript has run. For those, RAVEL
navigates with a real browser and reads the rendered document.

Three properties matter more than the navigation itself:

- **A browser retrieval is still a retrieval.** It produces the same
  `Retrieval` type as `Fetcher`, with the same hash, timestamp, and access
  status, so nothing downstream has to know which path read the page. The one
  difference is recorded in `note`: that the text came from a rendered DOM
  rather than from the bytes the server sent.
- **Chromium runs in a container, not in this process.** T19's sandbox
  supervisor owns the Docker socket and starts a per-project browser sandbox on
  an internal network behind a filtering proxy. This module only speaks HTTP to
  it, so an agent process gets no Docker access and imports no Playwright. A
  synchronous call still runs on a worker thread because the navigator's callers
  are async and a slow render must not block their loop; the thread no longer
  runs a browser, only the supervisor request and the `Retrieval` construction.
- **A missing browser is reported, not worked around.** When the sandbox cannot
  start, or the supervisor is unreachable, the navigator raises
  `BrowserUnavailable` and the caller records that the source could not be read
  in a browser. It does not fall back to an unrendered fetch and present it as
  the page.

A browser makes requests RAVEL never asked for. A page can redirect to an
address RAVEL refuses, and it can carry an image, a script, or a fetch whose URL
points at the host's own network. Chromium issues all of those itself, so the
check on the URL handed to `open` is not enough. The sandbox's proxy refuses
every request that is not on the operator's exact allowlist, and the browser
request route checks private and metadata addresses again. See `addressing` for
what RAVEL's own policy is; the proxy is stricter and is the one that sees the
socket.

**Binding is a deployment fact, not a name.** A provider name in configuration
does not create isolation. `_require_sandbox` refuses unless the name has an
adapter in this build, the supervisor URL and token are set, and the trusted
environment names a project and the Research role; `_binding` then uses a
handle only after the supervisor's capabilities attestation includes every
isolation flag. `BOUND_SANDBOX_PROVIDERS` stays empty because T07 asserts this
build does not *statically* claim a bound provider — T19's binding is the live,
attested supervisor path above.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, TypeVar

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.research import addressing
from ravel.research.fetching import MAX_INLINE_BYTES, excerpt_of, title_of
from ravel.research.leads import Retrieval
from ravel.sandbox.authority import SandboxAuthority, normalize_role
from ravel.sandbox.models import (
    SandboxError,
    SandboxUnsafeTarget,
    SandboxValidationError,
)
from ravel.sandbox.provider import DockerSandboxProvider
from ravel.state.store import hash_chunks

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: Navigation is slower than a GET: a browser has to start, parse, and run
#: scripts before the document settles.
DEFAULT_TIMEOUT_MS = 45_000

#: How long to wait for the network to go quiet after the document loads. Pages
#: that fetch their own content need this; pages that do not pay only the
#: timeout when nothing is pending.
SETTLE_TIMEOUT_MS = 10_000

#: Sandbox providers this build can confine Chromium with, *statically*. The
#: set stays empty after T19 on purpose: a provider is not bound by naming it,
#: and T07 asserts that this build does not claim one. T19's binding is a
#: deployment configuration plus a live supervisor whose handle attests the
#: isolation before any request is sent — see `_binding`. An unknown name is
#: refused, so a typo closes the path rather than opening it.
BOUND_SANDBOX_PROVIDERS: frozenset[str] = frozenset()

#: Provider names with an implemented adapter. Configuration alone does not
#: make one usable: `_binding` also requires the supervisor's URL, its token,
#: the trusted project and role environment, and the supervisor's own
#: capabilities attestation. A name in this set with none of the rest still
#: raises `BrowserUnavailable`.
IMPLEMENTED_SANDBOX_PROVIDERS: frozenset[str] = frozenset({"docker"})

#: Environment names the tool server is launched with. They are trusted input:
#: the composition writes them, not the model, and the provider is bound to the
#: project this process was started for.
_ENV_PROJECT = "RAVEL_PROJECT_ID"
_ENV_ROLE = "RAVEL_ROLE"
_ENV_BINDING = "RAVEL_BINDING_ID"
_ENV_EPOCH = "RAVEL_LEASE_EPOCH"


class BrowserUnavailable(RuntimeError):
    """A browser could not be started.

    Raised rather than returning an empty retrieval: "RAVEL has no browser" and
    "this page is empty" are different facts, and a research record that
    conflates them is wrong in the direction that matters.
    """


@dataclass(frozen=True, slots=True)
class BrowserPolicy:
    """How much navigating a navigator is allowed to do."""

    timeout_ms: int = DEFAULT_TIMEOUT_MS
    settle_ms: int = SETTLE_TIMEOUT_MS
    max_bytes: int = MAX_INLINE_BYTES
    #: Whether a page that answered with a bot challenge is reported as
    #: access-limited rather than read. RAVEL reads what a page says; it does
    #: not solve challenges to get there.
    respect_challenges: bool = True


@dataclass(frozen=True, slots=True)
class _Binding:
    """A configured provider and the authority this process may use with it."""

    provider: DockerSandboxProvider
    authority: SandboxAuthority


def _configured_provider(settings: Settings) -> str:
    """The provider name, or a refusal that keeps the path closed.

    Raises:
        BrowserUnavailable: No provider is configured, or the name has no
            adapter in this build.
    """
    provider = (settings.research_browser_sandbox_provider or "").strip().casefold()
    if not provider:
        raise BrowserUnavailable(
            "browser retrieval is disabled: RAVEL has no sandbox around Chromium, "
            "so starting one on this host would make requests RAVEL cannot pin or "
            "filter. Controlled HTTP/API research remains available; T19 binds a "
            "sandbox provider and re-certifies browser egress"
        )
    if provider not in IMPLEMENTED_SANDBOX_PROVIDERS:
        raise BrowserUnavailable(
            f"browser retrieval is disabled: {provider!r} is not a sandbox provider "
            "implemented by this build. Configuring a name does not create the "
            "isolation; T19 has to bind one"
        )
    return provider


def _require_sandbox(settings: Settings) -> None:
    """Refuse to request a rendered fetch unless T19's sandbox is configured.

    Checked in the public entry points, after the URL has been refused or
    accepted by `addressing` and before any work is handed to the worker. This
    function deliberately does no I/O: an unsafe URL is a decision about the
    URL whether or not a supervisor is reachable, while a safe URL without the
    deployment coordinates is a browser that must not be asked for.

    The provider name is configuration, not an implementation. T19 implements
    `docker`, but a name alone is not a binding: the supervisor URL, its token,
    the trusted project and role, and the supervisor's attested capabilities
    are all required. Every refusal retains "T19" so an operator can tell this
    gate from a URL policy refusal.

    Raises:
        BrowserUnavailable: The provider is absent or unimplemented, or the
            deployment coordinates or trusted project scope are missing/wrong.
    """
    _configured_provider(settings)
    if not settings.sandbox_supervisor_url:
        raise BrowserUnavailable(
            "browser retrieval is disabled: the docker sandbox provider is named, "
            "but RAVEL_SANDBOX_SUPERVISOR_URL is unset. T19 requires a separate "
            "supervisor that owns Docker; this process must not hold the socket"
        )
    if settings.sandbox_supervisor_token is None or not (
        settings.sandbox_supervisor_token.get_secret_value().strip()
    ):
        raise BrowserUnavailable(
            "browser retrieval is disabled: RAVEL_SANDBOX_SUPERVISOR_TOKEN is unset, "
            "and T19's supervisor refuses every request without its bearer token"
        )
    project = os.environ.get(_ENV_PROJECT, "").strip()
    if not project:
        raise BrowserUnavailable(
            "browser retrieval is disabled: RAVEL_PROJECT_ID is unset, so this "
            "process has no trusted project to bind the T19 sandbox to"
        )
    role = normalize_role(os.environ.get(_ENV_ROLE, ""))
    if role != "RESEARCH":
        raise BrowserUnavailable(
            "browser retrieval is disabled: only the Research role may hold a "
            f"browser sandbox, and this process's RAVEL_ROLE is {role or 'unset'!r}. "
            "T19 separates browser egress to Research"
        )


def _binding(settings: Settings) -> _Binding:
    """Build the project-bound provider once the configuration has passed.

    The token travels in an Authorization header and never in a URL, so it is
    not logged by a proxy's request line. The authority's project comes from
    `RAVEL_PROJECT_ID`, which the composition writes; a model cannot supply it,
    and the provider refuses an authority naming another project.
    """
    _require_sandbox(settings)
    assert settings.sandbox_supervisor_url is not None
    assert settings.sandbox_supervisor_token is not None
    project = os.environ[_ENV_PROJECT].strip()
    binding_id = os.environ.get(_ENV_BINDING, "").strip() or f"research-browser:{project}"
    epoch: int | None = None
    raw_epoch = os.environ.get(_ENV_EPOCH, "").strip()
    if raw_epoch:
        try:
            epoch = int(raw_epoch)
        except ValueError as exc:
            raise BrowserUnavailable(
                f"browser retrieval is disabled: RAVEL_LEASE_EPOCH={raw_epoch!r} is "
                "not an integer, so T19 cannot bind the lease epoch"
            ) from exc
    try:
        authority = SandboxAuthority(
            project_id=project,
            binding_id=binding_id,
            agent_role="RESEARCH",
            lease_epoch=epoch,
        )
        provider = DockerSandboxProvider(
            base_url=settings.sandbox_supervisor_url,
            token=settings.sandbox_supervisor_token.get_secret_value(),
            project_id=project,
        )
    except SandboxValidationError as exc:
        raise BrowserUnavailable(
            f"browser retrieval is disabled: T19's sandbox configuration is invalid "
            f"({exc})"
        ) from exc
    return _Binding(provider=provider, authority=authority)


class BrowserNavigator:
    """A real Chromium, on a thread of its own, reporting `Retrieval`s."""

    def __init__(
        self,
        settings: Settings,
        policy: BrowserPolicy | None = None,
        *,
        resolver: addressing.Resolver = addressing.resolve,
    ) -> None:
        self.settings = settings
        self.policy = policy or BrowserPolicy()
        self.resolver = resolver
        # One worker, started lazily. Every RAVEL caller is async and a sandbox
        # render can take tens of seconds; the dedicated thread keeps that off
        # the caller's loop. It runs no browser: Chromium is in the sandbox.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ravel-browser")
        self._binding_cache: _Binding | None = None
        self._closed = False

    def open(self, url: str) -> Retrieval:
        """Navigate to a URL and read the rendered document.

        Raises:
            BrowserUnavailable: No compliant sandbox provider is bound, or
                Chromium could not be started, or the navigation failed.
            addressing.UnsafeURL: The URL, or a URL the page navigated to
                itself, is one RAVEL will not navigate to. The URL RAVEL was
                given is refused from the calling thread, before the browser is
                asked for anything, because the answer does not depend on the
                browser. A redirect is refused on the worker thread, by the
                route handler that checks every request the page makes.
        """
        addressing.guard(url, resolver=self.resolver)
        _require_sandbox(self.settings)
        return self._on_worker(lambda: self._open(url))

    def links(self, url: str, *, limit: int = 50) -> list[str]:
        """The links on a rendered page, absolute and de-duplicated.

        This is how a research task walks from a landing page to the
        supplementary material without guessing at URLs. Order is document
        order, so the caller can prefer what the page itself put first.

        Raises:
            BrowserUnavailable: No compliant sandbox provider is bound, so the
                browser is not started.
            addressing.UnsafeURL: The URL, or a URL the page navigated to
                itself, is one RAVEL will not navigate to.
        """
        addressing.guard(url, resolver=self.resolver)
        _require_sandbox(self.settings)
        return self._on_worker(lambda: self._links(url, limit))

    def close(self) -> None:
        self._worker.shutdown(wait=True)
        self._closed = True
        binding = self._binding_cache
        self._binding_cache = None
        if binding is not None:
            try:
                binding.provider.close_browser(binding.authority)
            except SandboxError:
                # Teardown runs after the caller's result, and a supervisor
                # that is already gone is not a reason to replace that result
                # with a shutdown error. The handle record remains queryable.
                logger.debug("browser sandbox teardown failed", exc_info=True)

    def __enter__(self) -> BrowserNavigator:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # ── everything below runs on the worker thread ────────────────────────

    def _on_worker(self, work: Callable[[], T]) -> T:
        """Run browser work somewhere Playwright's sync API is allowed to run.

        Raises:
            BrowserUnavailable: The navigator was already closed, or the work
                failed. The original reason is preserved as `__cause__`.
        """
        if self._closed:
            raise BrowserUnavailable("this navigator has been closed")
        future = self._worker.submit(work)
        try:
            return future.result()
        except addressing.UnsafeURL:
            raise
        except SandboxUnsafeTarget as exc:
            # The supervisor refused this URL before Chromium requested it.
            # Report it as the URL decision it is, not as a navigation failure:
            # "RAVEL would not fetch this" and "the page did not load" are
            # different facts, and the second invites a retry.
            raise addressing.UnsafeURL(exc.url, exc.reason) from None
        except BrowserUnavailable:
            raise
        # A supervisor that is unreachable, a browser image that cannot start,
        # a timeout, and a refused capability all mean the same thing to a
        # caller — the rendered fetch did not produce a document — so they are
        # translated into that, with the typed cause preserved.
        except Exception as exc:
            raise BrowserUnavailable(f"navigation failed: {type(exc).__name__}: {exc}") from exc

    def _open(self, url: str) -> Retrieval:
        binding = self._binding()
        page = binding.provider.fetch(
            url=url,
            authority=binding.authority,
            timeout_ms=self.policy.timeout_ms,
            settle_ms=self.policy.settle_ms,
            max_bytes=self.policy.max_bytes,
        )
        body = page.body
        title = page.title.strip() or title_of(body, "text/html")
        challenge = self.policy.respect_challenges and _looks_like_a_challenge(
            title, page.html
        )
        return self._retrieval(
            requested_url=url,
            final_url=page.final_url,
            status=page.status_code,
            body=body,
            title=title,
            challenge=challenge,
        )

    def _links(self, url: str, limit: int) -> list[str]:
        binding = self._binding()
        page = binding.provider.fetch(
            url=url,
            authority=binding.authority,
            timeout_ms=self.policy.timeout_ms,
            settle_ms=self.policy.settle_ms,
            max_bytes=self.policy.max_bytes,
        )
        seen: list[str] = []
        for href in page.links:
            absolute = _absolute(href, page.final_url)
            if absolute and absolute not in seen:
                seen.append(absolute)
            if len(seen) >= limit:
                break
        return seen

    def _binding(self) -> _Binding:
        """The configured provider, built once per navigator.

        Built on the worker thread so the construction and every request happen
        on the same thread. No Playwright is imported here: Chromium lives in
        the sandbox image, and this process only sees the JSON record the
        supervisor returns.
        """
        if self._binding_cache is None:
            self._binding_cache = _binding(self.settings)
        return self._binding_cache

    def _retrieval(
        self,
        *,
        requested_url: str,
        final_url: str,
        status: int | None,
        body: bytes,
        title: str,
        challenge: bool,
    ) -> Retrieval:
        """A `Retrieval` from a rendered page.

        The body is the rendered DOM, so its hash is the hash of what the
        browser built rather than of what the server sent. The note says so,
        because a later verification that re-fetched over HTTP would get
        different bytes and must not read that difference as the source having
        changed.
        """
        # The artifact store's helper, for the same reason the fetcher uses it:
        # one hash convention, so a rendered source and a fetched one are
        # comparable with each other and with their stored snapshots.
        digest, _ = hash_chunks([body])
        note = "read from the rendered document (browser navigation)"
        if challenge:
            return Retrieval(
                requested_url=requested_url,
                final_url=final_url,
                access_status=AccessStatus.ACCESS_LIMITED,
                retrieved_at=datetime.now(UTC),
                status_code=status,
                media_type="text/html",
                title=title,
                note=(
                    "the page is a bot challenge rather than the source; RAVEL does "
                    "not solve challenges to reach content"
                ),
            )
        return Retrieval(
            requested_url=requested_url,
            final_url=final_url,
            access_status=AccessStatus.OK,
            retrieved_at=datetime.now(UTC),
            status_code=status,
            media_type="text/html",
            content_hash=digest,
            size_bytes=len(body),
            title=title,
            body=body[: self.policy.max_bytes],
            excerpt=excerpt_of(body[: 512 * 1024], "text/html"),
            note=note,
        )

    def _session(self) -> _Session:
        """A route-guard session, for testing the policy without Chromium.

        The production path never calls this: it renders inside the T19
        sandbox. The guard is nevertheless the same code that decides which
        page-chosen requests are allowed, and it is worth exercising where
        Chromium cannot run.
        """
        return _Session(self)


class _Session:
    """The browser-request policy, usable with fakes.

    RAVEL does not launch a browser in the application process any more; the
    sandbox owns Chromium. This class therefore refuses to start a browser of
    its own. It can still install the route guard on a browser object that a
    caller already has — a test that supplies one, for instance — and it is
    that small piece of setup the route-policy tests inspect. Acquiring a
    browser is outside this class, so no configuration or request can turn it
    into a local Chromium path.
    """

    def __init__(self, outer: BrowserNavigator, *, browser: Any = None) -> None:
        self.outer = outer
        self.response: Any = None
        #: The first navigation this session was told not to make, if any.
        #: `goto` re-raises it so a redirected navigation reports a refusal
        #: rather than a generic Playwright failure.
        self.refused: addressing.UnsafeURL | None = None
        self._browser = browser
        self._context: Any = None
        self._page: Any = None

    @property
    def page(self) -> Any:
        return self._page

    @property
    def status(self) -> int | None:
        """The HTTP status of the last navigation, when there was one."""
        return self.response.status if self.response is not None else None

    def goto(self, url: str, timeout_ms: int) -> None:
        """Navigate, reporting a refused navigation as the refusal it is.

        Raises:
            addressing.UnsafeURL: The navigation was aborted by the guard.
        """
        try:
            self.response = self._page.goto(
                url, wait_until="domcontentloaded", timeout=timeout_ms
            )
        except Exception:
            if self.refused is not None:
                raise self.refused from None
            raise

    def __enter__(self) -> _Session:
        if self._browser is None:
            raise BrowserUnavailable(
                "Chromium is started only inside the T19 sandbox; this application "
                "process has no browser to enter"
            )
        # `service_workers="block"` because a worker's requests do not pass
        # through `context.route`; the route handler below would never see them.
        # The sandbox's browser uses the same setting; the proxy is the second,
        # independent decision point in that path.
        self._context = self._browser.new_context(service_workers="block")
        # Registered before the first page exists, so there is no window in
        # which a navigation could be made unguarded. This setup is also what
        # the route-policy tests inspect; it never happens unless the caller
        # supplied the browser.
        self._context.route("**/*", self._guard_route)
        self._page = self._context.new_page()
        return self

    def _guard_route(self, route: Any, request: Any) -> None:
        """Refuse a request the browser is about to make, or let it through.

        The URLs this sees were chosen by the page, not by RAVEL: a redirect to
        somewhere private, an `<img>` pointing at the metadata service, a
        script fetching the cluster's API. The sandbox proxy makes the same
        decision at the socket, which is where it cannot be bypassed.
        """
        try:
            addressing.guard(request.url, resolver=self.outer.resolver)
        except addressing.UnsafeURL as exc:
            if request.is_navigation_request():
                self.refused = exc
            logger.info("refused a browser request: %s", exc)
            route.abort()
            return
        route.continue_()

    def __exit__(self, *_: object) -> None:
        # The context belongs to the caller's browser; closing it here would
        # close a page the caller may still be using. The production path does
        # not use this class at all.
        return None


def _looks_like_a_challenge(title: str, html: str) -> bool:
    """Whether a page is an interstitial rather than the thing it stands for.

    Matched narrowly, on phrases that only appear on challenge pages. A false
    positive costs a source that has to be read another way; a false negative
    would register a challenge page as the source itself, which is the failure
    this exists to prevent.
    """
    haystack = f"{title}\n{html[:4000]}".lower()
    return any(
        phrase in haystack
        for phrase in (
            "just a moment",
            "checking your browser",
            "enable javascript and cookies to continue",
            "attention required! | cloudflare",
            "verify you are human",
        )
    )


def _absolute(href: str, base: str) -> str | None:
    """An absolute http(s) URL, or None for anything else.

    `javascript:`, `mailto:` and fragment links are dropped here rather than at
    the call site: a research task walking links should never be handed one it
    cannot open.
    """
    from urllib.parse import urljoin, urlsplit

    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    absolute = urljoin(base, href)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return absolute.split("#")[0]
