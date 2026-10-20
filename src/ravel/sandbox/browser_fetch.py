"""A rendered fetch that runs inside the browser sandbox.

This module is the in-container half of `BrowserNavigator`. The RAVEL process
never launches Chromium; it asks the supervisor for a browser sandbox, and the
supervisor runs this script with `docker exec`. What comes back is a plain JSON
record — rendered HTML, final URL, status, title, links, and the hash of the
bytes — so the caller can treat a sandboxed retrieval exactly like any other
retrieval and can verify the bytes it registers.

Two policy checks run here even though the proxy also enforces them:

- the URL scheme and the address policy, using the same `ravel.research.addressing`
  module the unsandboxed fetch paths use. This makes a refusal a clear
  `UnsafeURL` rather than a browser network error;
- a Playwright route handler over every request the page makes. A redirect, an
  image, or a script is refused before Chromium sends it. The proxy is still
  the authority on what leaves the network, because a route handler cannot pin
  the address a browser's own resolver chooses.

Chromium runs with `--no-sandbox`. That is not a weakening of the T19 boundary:
the container is the boundary — non-root, read-only root filesystem, all
capabilities dropped, `no-new-privileges`, the default seccomp profile, no
Docker socket, and an internal-only network whose sole peer is the proxy.
Chromium's setuid sandbox cannot initialize under exactly those restrictions,
and pretending otherwise would either remove the restrictions or leave the
browser unable to start.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
from typing import Any
from urllib.parse import urljoin, urlsplit

from ravel.sandbox import addressing

DEFAULT_MAX_BYTES = 8 * 1024 * 1024
DEFAULT_TIMEOUT_MS = 45_000
DEFAULT_SETTLE_MS = 10_000
MAX_LINKS = 200

_CHALLENGE_PHRASES = (
    "just a moment",
    "checking your browser",
    "enable javascript and cookies to continue",
    "attention required! | cloudflare",
    "verify you are human",
)


class FetchRefused(RuntimeError):
    """The page cannot be fetched for a reason with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _looks_like_a_challenge(title: str, html: str) -> bool:
    haystack = f"{title}\n{html[:4000]}".lower()
    return any(phrase in haystack for phrase in _CHALLENGE_PHRASES)


def _bounded_html(html: str, max_bytes: int) -> tuple[str, bool, str]:
    """Truncate at a UTF-8 boundary and hash exactly the bytes returned.

    A hash computed over bytes that differ from the bytes the caller receives
    would make registration fail for a reason nobody could see. Cutting on a
    character boundary and re-encoding makes `html.encode("utf-8")` the one
    spelling of the body, and the hash is over that.
    """
    raw = html.encode("utf-8", errors="replace")
    truncated = False
    if len(raw) > max_bytes:
        raw = raw[:max_bytes].decode("utf-8", errors="ignore").encode("utf-8")
        truncated = True
    return raw.decode("utf-8"), truncated, f"sha256:{hashlib.sha256(raw).hexdigest()}"


def fetch(
    url: str,
    *,
    proxy_server: str,
    timeout_ms: int,
    settle_ms: int,
    max_bytes: int,
    resolver: addressing.Resolver = addressing.resolve,
) -> dict[str, Any]:
    """Navigate with a real Chromium inside the sandbox and return the page."""
    try:
        addressing.guard(url, resolver=resolver)
    except addressing.UnsafeURL as exc:
        raise FetchRefused("unsafe_url", str(exc)) from exc
    if not proxy_server:
        raise FetchRefused(
            "no_proxy",
            "the browser sandbox has no controlled proxy; refusing to start Chromium "
            "without an egress filter",
        )

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise FetchRefused(
            "no_browser",
            f"playwright is not installed in the sandbox image: {exc}",
        ) from exc

    refusals: list[addressing.UnsafeURL] = []

    def guard_route(route: Any, request: Any) -> None:
        try:
            addressing.guard(request.url, resolver=resolver)
        except addressing.UnsafeURL as exc:
            refusals.append(exc)
            route.abort()
            return
        route.continue_()

    playwright = sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                f"--proxy-server={proxy_server}",
                # Disable Chromium's implicit bypass for loopback. There is no
                # route from this network except the proxy, and an implicit
                # bypass would be a second policy inside the browser.
                "--proxy-bypass-list=<-loopback>",
            ],
        )
        context = browser.new_context(service_workers="block")
        context.route("**/*", guard_route)
        page = context.new_page()
        try:
            response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        except Exception as exc:
            if refusals:
                raise FetchRefused("unsafe_url", str(refusals[0])) from exc
            raise FetchRefused(
                "navigation_failed",
                f"navigation did not produce a document: {type(exc).__name__}: {exc}",
            ) from exc
        # A page that keeps a connection open is still readable; waiting is an
        # optimisation, not a condition of success.
        with contextlib.suppress(Exception):
            page.wait_for_load_state("networkidle", timeout=settle_ms)
        html = page.content()
        title = (page.title() or "").strip()
        final_url = page.url
        status = response.status if response is not None else None
        links: list[str] = []
        for element in page.query_selector_all("a[href]"):
            href = element.get_attribute("href")
            if not href:
                continue
            absolute = _absolute(href, final_url)
            if absolute and absolute not in links:
                links.append(absolute)
            if len(links) >= MAX_LINKS:
                break
    finally:
        if browser is not None:
            with contextlib.suppress(Exception):
                browser.close()
        playwright.stop()

    body, truncated, digest = _bounded_html(html, max_bytes)
    title = title or _title_of(body)
    challenge = _looks_like_a_challenge(title, body)
    return {
        "ok": True,
        "requested_url": url,
        "final_url": final_url,
        "status_code": status,
        "title": title,
        "html": body,
        "content_hash": digest,
        "media_type": "text/html",
        "links": links,
        "truncated": truncated,
        "note": (
            "the page is a bot challenge rather than the source; RAVEL does not "
            "solve challenges to reach content"
            if challenge
            else "read from the rendered document inside a sandbox with proxy egress"
        ),
        "challenge": challenge,
    }


def _absolute(href: str, base: str) -> str | None:
    if href.startswith(("javascript:", "mailto:", "tel:", "#")):
        return None
    absolute = urljoin(base, href)
    parts = urlsplit(absolute)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return absolute.split("#")[0]


def _title_of(html: str) -> str:
    lowered = html.lower()
    start = lowered.find("<title")
    if start < 0:
        return ""
    open_end = lowered.find(">", start)
    close = lowered.find("</title>", open_end)
    if open_end < 0 or close < 0:
        return ""
    return " ".join(html[open_end + 1 : close].split())[:500]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="render one URL inside a sandbox")
    parser.add_argument("--url", required=True)
    parser.add_argument("--proxy-server", default="")
    parser.add_argument("--timeout-ms", type=int, default=DEFAULT_TIMEOUT_MS)
    parser.add_argument("--settle-ms", type=int, default=DEFAULT_SETTLE_MS)
    parser.add_argument("--max-bytes", type=int, default=DEFAULT_MAX_BYTES)
    args = parser.parse_args(argv)
    try:
        result = fetch(
            args.url,
            proxy_server=args.proxy_server,
            timeout_ms=args.timeout_ms,
            settle_ms=args.settle_ms,
            max_bytes=args.max_bytes,
        )
    except FetchRefused as exc:
        print(json.dumps({"ok": False, "error_code": exc.code, "error": exc.message}))
        return 1
    except addressing.UnsafeURL as exc:
        print(json.dumps({"ok": False, "error_code": "unsafe_url", "error": str(exc)}))
        return 1
    except Exception as exc:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error_code": "browser_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
