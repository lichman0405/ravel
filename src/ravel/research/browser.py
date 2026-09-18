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
- **Playwright runs on its own thread.** Its synchronous API refuses to start
  inside a running event loop, and RAVEL's callers are async. The navigator
  therefore owns a single worker thread and every call goes through it, so the
  API is safe to call from either kind of caller and no caller has to know
  which kind it is.
- **A missing browser is reported, not worked around.** Chromium needs system
  libraries that a minimal container does not have. When it cannot start, the
  navigator says exactly which command installs them, and the caller records
  that the source could not be read in a browser. It does not fall back to an
  unrendered fetch and present it as the page.
"""

from __future__ import annotations

import logging
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

_INSTALL_HINT = (
    "run `.venv/bin/python -m playwright install-deps chromium` (and "
    "`.venv/bin/python -m playwright install chromium` for the browser itself); "
    "on a host without passwordless sudo an administrator has to run it"
)


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
        self.headless = settings.playwright_headless
        self.resolver = resolver
        # One worker, started lazily. Playwright's sync API asserts it is not
        # called from a thread with a running loop, and every RAVEL caller is
        # async; running the whole session on a dedicated thread is what makes
        # this callable from them without leaking that detail outward.
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ravel-browser")
        self._closed = False

    def open(self, url: str) -> Retrieval:
        """Navigate to a URL and read the rendered document.

        Raises:
            BrowserUnavailable: Chromium could not be started, or the
                navigation failed.
            addressing.UnsafeURL: The URL is one RAVEL will not navigate to.
                Raised from the calling thread, before the browser is asked for
                anything, because the answer does not depend on the browser.
        """
        addressing.guard(url, resolver=self.resolver)
        return self._on_worker(lambda: self._open(url))

    def links(self, url: str, *, limit: int = 50) -> list[str]:
        """The links on a rendered page, absolute and de-duplicated.

        This is how a research task walks from a landing page to the
        supplementary material without guessing at URLs. Order is document
        order, so the caller can prefer what the page itself put first.

        Raises:
            addressing.UnsafeURL: The URL is one RAVEL will not navigate to.
        """
        addressing.guard(url, resolver=self.resolver)
        return self._on_worker(lambda: self._links(url, limit))

    def close(self) -> None:
        self._worker.shutdown(wait=True)
        self._closed = True

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
        except BrowserUnavailable:
            raise
        # Playwright raises its own exception types, and which one it raises
        # for a timeout varies by version. They all mean the same thing to a
        # caller — the navigation did not produce a document — so they are
        # translated into that, with the original preserved as the cause.
        except Exception as exc:
            raise BrowserUnavailable(f"navigation failed: {type(exc).__name__}: {exc}") from exc

    def _open(self, url: str) -> Retrieval:
        with self._session() as view:
            view.goto(url, self.policy.timeout_ms)
            try:
                view.page.wait_for_load_state("networkidle", timeout=self.policy.settle_ms)
            # A page that keeps a connection open — an analytics beacon, a
            # websocket — never goes idle, and it is still readable. Waiting is
            # an optimisation here, so failing to wait is not a failure.
            except Exception:
                logger.debug("page did not reach network idle: %s", url)

            html = view.page.content()
            body = html.encode("utf-8", errors="replace")
            title = (view.page.title() or "").strip() or title_of(body, "text/html")
            return self._retrieval(
                requested_url=url,
                final_url=view.page.url,
                status=view.status,
                body=body,
                title=title,
                challenge=self.policy.respect_challenges
                and _looks_like_a_challenge(title, html),
            )

    def _links(self, url: str, limit: int) -> list[str]:
        with self._session() as view:
            view.goto(url, self.policy.timeout_ms)
            seen: list[str] = []
            for element in view.page.query_selector_all("a[href]"):
                href = element.get_attribute("href")
                if not href:
                    continue
                absolute = _absolute(href, view.page.url)
                if absolute and absolute not in seen:
                    seen.append(absolute)
                if len(seen) >= limit:
                    break
            return seen

    def _session(self) -> _Session:
        return _Session(self)

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


class _Session:
    """One browser, its context, its page, and the last navigation's response.

    Held together because their lifetimes are identical: a page is only valid
    while its context and browser are, and a caller that closed one early would
    get a confusing error from Playwright rather than from RAVEL.
    """

    def __init__(self, outer: BrowserNavigator) -> None:
        self.outer = outer
        self.response: Any = None

    @property
    def page(self) -> Any:
        return self._page

    @property
    def status(self) -> int | None:
        """The HTTP status of the last navigation, when there was one.

        Playwright reports no response for a `data:` or `about:` document, and
        a rendered page with no status is still a page; None is the honest
        answer rather than a fabricated 200.
        """
        return self.response.status if self.response is not None else None

    def goto(self, url: str, timeout_ms: int) -> None:
        self.response = self._page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

    def __enter__(self) -> _Session:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                f"the playwright package is not installed: {exc}. Install it with "
                "`uv pip install playwright`, then " + _INSTALL_HINT
            ) from exc

        self._playwright = sync_playwright().start()
        try:
            self._browser = self._playwright.chromium.launch(headless=self.outer.headless)
        # The failure this catches is usually not Playwright's: Chromium exits
        # at startup when a shared library is missing, and the exception then
        # carries the loader's message rather than anything about browsers. The
        # message is kept and the install command appended, because that is
        # what makes this error actionable on a minimal host.
        except Exception as exc:
            self._playwright.stop()
            raise BrowserUnavailable(
                f"Chromium could not be launched: {type(exc).__name__}: {exc}. "
                f"To install its system libraries, {_INSTALL_HINT}"
            ) from exc
        self._context = self._browser.new_context(user_agent=self._user_agent())
        self._page = self._context.new_page()
        return self

    def __exit__(self, *_: object) -> None:
        for closer in (self._context.close, self._browser.close, self._playwright.stop):
            try:
                closer()
            # Teardown runs while an exception may be in flight. A failure to
            # close the browser must not replace the result the caller was
            # about to receive with a shutdown error.
            except Exception:
                logger.debug("browser teardown step failed", exc_info=True)

    def _user_agent(self) -> str:
        from ravel.research.fetching import FetchError, user_agent

        try:
            return user_agent(self.outer.settings)
        except FetchError:
            return "RAVEL/0.1 (research source gateway)"


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
