"""Real research retrieval, and what an unreadable source is recorded as.

The cases here reach the public internet on purpose. T07's fourth acceptance
criterion is that tightening egress did not close authorized public research,
and a fake would not show that: the property is that a real server's bytes
arrive, hashed and titled, and that a real refusal is recorded as the specific
refusal it was.

Two sources are used. `example.com` answers a stable HTML document with a
stable title; Crossref's public API answers JSON and asks for a contact address
in the User-Agent, which `Fetcher` sends. The restricted-status cases use
httpbin's `/status/n` endpoints, which return that status with an empty body:
RAVEL must record the status and no content, because a retrieval that invented
text for a 403 would be worse than one that failed.

These tests retry a transport failure and never retry a refusal or a status.
An outage is reported as an outage; it is not turned into the fact the test is
about.
"""

from __future__ import annotations

import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.research import addressing
from ravel.research.fetching import Fetcher, FetchPolicy, _restriction_for
from ravel.research.leads import Retrieval

#: A page whose title has been the same for decades.
HTML_PAGE = "https://example.com/"

#: Crossref's public, no-key-needed works endpoint. It asks identified clients
#: to send a contact address, which the fixture configuration supplies.
CROSSREF_API = "https://api.crossref.org/works?rows=1"

#: A server that will return the status in its path, for the restricted cases.
STATUS_PAGE = "https://httpbin.org/status/{code}"


def _ipv4_only(host: str) -> list[str]:
    """Real DNS, IPv4 answers only.

    The gate container may have no IPv6 route, and the pinned backend connects
    to the first validated address — which sorts IPv6 first. Filtering here
    keeps the test about public retrieval rather than about the host's IPv6
    routing table; the address that is connected to is still a real, resolved,
    public address.
    """
    return [address for address in addressing.resolve(host) if ":" not in address]


def _fetcher(settings: Settings, **policy: Any) -> Fetcher:
    return Fetcher(
        settings,
        FetchPolicy(timeout_seconds=25.0, **policy),
        resolver=_ipv4_only,
    )


#: Notes that mean RAVEL could not reach the host at all, as opposed to the
#: host answering. Only these are retried: a 403 from httpbin is an answer.
_TRANSPORT_FAILURES = (
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "gaierror",
)


def _unreachable(retrieval: Retrieval) -> bool:
    return any(marker in retrieval.note for marker in _TRANSPORT_FAILURES)


def _read(fetcher: Fetcher, url: str, *, attempts: int = 3) -> Retrieval:
    """Fetch a public URL, retrying a transport failure but never a refusal."""
    retrieval: Retrieval | None = None
    for attempt in range(attempts):
        retrieval = fetcher.get(url)
        if not _unreachable(retrieval) or attempt == attempts - 1:
            return retrieval
        time.sleep(0.75)
    assert retrieval is not None
    return retrieval


def _assert_read_but_unwritten(retrieval: Retrieval, status: int, fragment: str) -> None:
    """A restricted answer: the status is recorded, and no content is invented."""
    assert retrieval.access_status is not AccessStatus.OK
    assert retrieval.status_code == status
    assert fragment in retrieval.note, retrieval.note
    assert retrieval.content_hash is None, "a restricted source was hashed as if read"
    assert retrieval.body is None, "a restricted source was given a body"
    assert retrieval.excerpt == "", "a restricted source was given an excerpt"
    assert retrieval.was_read is False
    assert retrieval.retrieved_at.tzinfo is not None


# ── Authorized public research still works ─────────────────────────────────


def test_a_real_public_page_is_read_with_its_own_title_and_hash(
    gateway_settings: Settings,
) -> None:
    """The bytes are the server's, and the hash describes exactly those bytes.

    The digest is computed independently here rather than trusted from the
    record: a fetcher that hashed something other than what it returned would
    pass a test that read its own hash back.
    """
    retrieval = _read(_fetcher(gateway_settings), HTML_PAGE)

    assert retrieval.access_status is AccessStatus.OK, retrieval.note
    assert retrieval.status_code == 200
    assert retrieval.media_type == "text/html"
    assert retrieval.title == "Example Domain", retrieval.title
    assert retrieval.body, "an OK retrieval carried no body"
    assert retrieval.excerpt and "Example Domain" in retrieval.excerpt

    digest = hashlib.sha256(retrieval.body).hexdigest()
    assert retrieval.content_hash == f"sha256:{digest}"
    assert retrieval.size_bytes == len(retrieval.body)
    assert retrieval.was_read is True
    assert retrieval.requested_url == HTML_PAGE
    assert retrieval.final_url.startswith("https://example.com")


def test_a_real_research_api_is_read_as_the_json_it_sent(
    gateway_settings: Settings,
) -> None:
    """Crossref, reached by a real request and parsed from the bytes returned."""
    retrieval = _read(_fetcher(gateway_settings), CROSSREF_API)

    assert retrieval.access_status is AccessStatus.OK, retrieval.note
    assert retrieval.status_code == 200
    assert retrieval.media_type == "application/json", retrieval.media_type
    assert retrieval.body is not None

    payload = json.loads(retrieval.body)
    assert payload["status"] == "ok"
    assert payload["message"]["items"], "Crossref returned no items"
    assert retrieval.content_hash == f"sha256:{hashlib.sha256(retrieval.body).hexdigest()}"
    # A JSON document has no title RAVEL can read, and none is invented.
    assert retrieval.title == ""


def test_a_real_public_redirect_is_followed_and_the_final_url_recorded(
    gateway_settings: Settings,
) -> None:
    """Redirect following still works for public targets.

    The redirect defence is about *where* a hop goes, not about refusing
    redirects. A fetcher that stopped following them would pass every refusal
    test and break every publisher URL that canonicalizes itself.
    """
    requested = "https://httpbin.org/redirect/1"
    retrieval = _read(_fetcher(gateway_settings), requested)

    assert retrieval.access_status is AccessStatus.OK, retrieval.note
    assert retrieval.status_code == 200
    assert retrieval.final_url != requested, "the redirect was not followed"
    assert retrieval.final_url.endswith("/get"), retrieval.final_url


@pytest.mark.parametrize(
    ("code", "status", "fragment"),
    [
        (401, AccessStatus.AUTH_REQUIRED, "HTTP 401"),
        (403, AccessStatus.AUTH_REQUIRED, "HTTP 403"),
        (402, AccessStatus.PAYWALLED, "payment required"),
        (429, AccessStatus.ACCESS_LIMITED, "rate limited"),
        (451, AccessStatus.POLICY_BLOCKED, "legal"),
        (503, AccessStatus.ACCESS_LIMITED, "rate limited"),
        (404, AccessStatus.ACCESS_LIMITED, "HTTP 404"),
    ],
)
def test_a_refused_source_is_recorded_as_that_refusal_and_nothing_else(
    gateway_settings: Settings, code: int, status: AccessStatus, fragment: str
) -> None:
    """Each status keeps its own meaning, and none produces content.

    The distinction reaches the Evidence Ledger: a paywall calls for buying
    access, an auth wall for logging in, and a legal block for giving up. A
    fetch that collapsed them into a generic failure — or worse, into a body
    written from memory — would make the ledger a worse record than no record.
    """
    retrieval = _read(_fetcher(gateway_settings), STATUS_PAGE.format(code=code))
    _assert_read_but_unwritten(retrieval, code, fragment)


def test_a_paywall_header_turns_an_auth_refusal_into_a_paywall() -> None:
    """The header is the more specific signal, and the mapping says so.

    A real endpoint that returns both is uncommon enough that httpbin does not
    serve one; the mapping is asserted directly, beside the real statuses above
    rather than instead of them.
    """
    response = httpx.Response(403, headers={"x-paywall": "subscription"})
    assert _restriction_for(response) == (AccessStatus.PAYWALLED, "HTTP 403: paywall")

    bare = httpx.Response(403)
    assert _restriction_for(bare) == (
        AccessStatus.AUTH_REQUIRED,
        "HTTP 403: authentication required",
    )


def test_a_retrieval_cannot_carry_content_unless_it_was_read(
    gateway_settings: Settings,
) -> None:
    """The invariant is structural, not a convention the fetcher remembers.

    `Retrieval` refuses the record outright, so a future code path that tried
    to attach a plausible body to a restricted source fails at construction.
    """
    with pytest.raises(ValueError):
        Retrieval(
            requested_url=HTML_PAGE,
            final_url=HTML_PAGE,
            access_status=AccessStatus.ACCESS_LIMITED,
            retrieved_at=datetime.now(UTC),
            content_hash="sha256:" + "0" * 64,
            body=b"text that the server did not send",
        )
