"""What RAVEL records when it opens something and cannot have it.

The live suite can only exercise the restriction cases the open Internet
happens to serve on the day it runs. The mapping from a status code to a
recorded reason is what a research task acts on — buy access, authenticate, wait,
give up — so it is tested here against responses constructed for each case.

The rule the whole module turns on: **a record may not claim content it did not
receive.** Most of these tests are ways of trying to make it.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.research.fetching import (
    Fetcher,
    FetchError,
    FetchPolicy,
    user_agent,
)
from ravel.research.leads import Retrieval

URL = "https://example.org/paper"

PAGE = b"""<html><head><title>A Study of Things</title>
<style>body { color: red }</style></head>
<body><script>var x = 1;</script>
<p>The measured value was 4.2 units at room temperature.</p></body></html>"""

HTML = {"content-type": "text/html; charset=utf-8"}

#: What every hostname in this file resolves to, so that the address guard —
#: which really does run on every request — is answered without DNS. The guard
#: itself is covered in `test_research_addressing.py`.
PUBLIC_ADDRESS = "93.184.216.34"


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        research_contact_email="research@example.org",
        RAVEL_POSTGRES_DSN=None,
    )


@pytest.fixture
def fetcher(settings: Settings) -> Fetcher:
    """A fetcher whose hostnames resolve to a public address.

    The address guard is real and runs on every request, so a test that used the
    real resolver would depend on DNS for `example.org` — slow, network-bound,
    and testing nothing. What the guard does with each address is covered in
    `test_research_addressing.py`; here it is told the answer.
    """
    return Fetcher(settings, resolver=lambda host: ["93.184.216.34"])


@respx.mock
def test_a_readable_page_yields_bytes_a_hash_and_a_time(fetcher: Fetcher) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=PAGE, headers={
        "content-type": "text/html; charset=utf-8"
    }))

    retrieval = fetcher.get(URL)

    assert retrieval.was_read
    assert retrieval.content_hash is not None
    assert retrieval.content_hash.startswith("sha256:")
    assert retrieval.size_bytes == len(PAGE)
    assert retrieval.media_type == "text/html"
    assert retrieval.retrieved_at is not None


@respx.mock
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, AccessStatus.AUTH_REQUIRED),
        (403, AccessStatus.AUTH_REQUIRED),
        (402, AccessStatus.PAYWALLED),
        (451, AccessStatus.POLICY_BLOCKED),
        (429, AccessStatus.ACCESS_LIMITED),
        (503, AccessStatus.ACCESS_LIMITED),
        (404, AccessStatus.ACCESS_LIMITED),
        (500, AccessStatus.ACCESS_LIMITED),
    ],
)
def test_each_refusal_is_recorded_as_itself(
    fetcher: Fetcher, status: int, expected: AccessStatus
) -> None:
    """Five different facts about the world, kept apart.

    Collapsing them into "failed" would lose the only information that says
    what to do next: a 402 is worth paying for, a 429 is worth retrying, and a
    451 is worth giving up on.
    """
    respx.get(URL).mock(return_value=httpx.Response(status))

    retrieval = fetcher.get(URL)

    assert retrieval.access_status is expected
    assert retrieval.note
    assert retrieval.content_hash is None
    assert retrieval.body is None
    assert retrieval.excerpt == ""


@respx.mock
def test_a_403_that_says_paywall_is_recorded_as_a_paywall(fetcher: Fetcher) -> None:
    """The more specific signal wins.

    A bare 403 is an authentication problem; a 403 that names a subscription is
    a paywall, and the difference decides whether a lab buys access or fixes a
    credential.
    """
    respx.get(URL).mock(
        return_value=httpx.Response(403, headers={"www-authenticate": "Paywall realm=x"})
    )

    assert fetcher.get(URL).access_status is AccessStatus.PAYWALLED


@respx.mock
def test_a_restricted_page_may_still_report_its_title(fetcher: Fetcher) -> None:
    """What the server said about a page it would not show is not the page.

    Recording the title is allowed because it is something RAVEL actually
    received; the body, the excerpt and the hash are not, and stay absent.
    """
    respx.get(URL).mock(
        return_value=httpx.Response(
            403, content=PAGE, headers={"content-type": "text/html"}
        )
    )

    retrieval = fetcher.get(URL)

    assert retrieval.title == "A Study of Things"
    assert retrieval.content_hash is None
    assert retrieval.excerpt == ""


def test_a_retrieval_cannot_carry_content_it_did_not_receive() -> None:
    """The rule is enforced by the type, not by the code that builds it.

    Three separate ways to claim content on a refused retrieval, each refused
    at construction. A paywalled source with a hash would assert RAVEL read
    bytes it could not reach.
    """
    from datetime import UTC, datetime

    base = {
        "requested_url": URL,
        "final_url": URL,
        "retrieved_at": datetime.now(UTC),
    }
    for claim in (
        {"content_hash": "sha256:" + "0" * 64},
        {"body": b"pretend bytes"},
        {"excerpt": "a sentence nobody read"},
    ):
        with pytest.raises(ValueError):
            Retrieval(**base, access_status=AccessStatus.PAYWALLED, **claim)  # type: ignore[arg-type]


def test_a_read_source_must_say_when_it_was_read() -> None:
    """An OK retrieval with no timestamp is a source with no moment of reading."""
    from ravel.domain.evidence import EvidenceSource

    with pytest.raises(ValueError, match="retrieved_at"):
        EvidenceSource(project_id="p", url=URL, access_status=AccessStatus.OK)


@respx.mock
def test_a_head_establishes_the_url_answers_without_reading_it(fetcher: Fetcher) -> None:
    """A HEAD is a fact about availability, not about content."""
    respx.head(URL).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/html", "content-length": "1234"}
        )
    )

    retrieval = fetcher.head(URL)

    assert retrieval.was_read
    assert retrieval.content_hash is None, "nothing was hashed, so nothing may claim to be"
    assert retrieval.body is None


@respx.mock
def test_a_body_larger_than_the_cap_is_truncated_while_streaming(settings: Settings) -> None:
    """The cap is enforced on the stream, not from the declared length.

    `content-length` is the server's claim about itself. Here the server
    declares a small body and sends a large one, which is exactly the case the
    cap exists for.
    """
    fetcher = Fetcher(
        settings, FetchPolicy(max_bytes=16), resolver=lambda host: [PUBLIC_ADDRESS]
    )
    respx.get(URL).mock(
        return_value=httpx.Response(
            200,
            content=b"x" * 4096,
            headers={"content-type": "text/plain", "content-length": "8"},
        )
    )

    retrieval = fetcher.get(URL)

    assert retrieval.size_bytes == 16
    assert retrieval.body == b"x" * 16
    # The hash is of what was kept, so it describes the stored bytes.
    assert retrieval.content_hash is not None


@respx.mock
def test_a_transport_failure_is_a_limited_retrieval_and_not_an_exception(
    fetcher: Fetcher,
) -> None:
    """"RAVEL could not reach the network" is recorded, not raised.

    It is not a statement about the source, so it must not stop a research task
    that has other sources to try — but it must also not look like a source
    that answered.
    """
    respx.get(URL).mock(side_effect=httpx.ConnectError("no route to host"))

    retrieval = fetcher.get(URL)

    assert retrieval.access_status is AccessStatus.ACCESS_LIMITED
    assert "ConnectError" in retrieval.note
    assert retrieval.content_hash is None


@respx.mock
def test_the_title_comes_from_the_source_and_never_from_the_url(fetcher: Fetcher) -> None:
    respx.get(URL).mock(return_value=httpx.Response(200, content=PAGE, headers=HTML))

    assert fetcher.get(URL).title == "A Study of Things"


@respx.mock
def test_a_page_that_declares_no_type_is_not_guessed_to_be_html(fetcher: Fetcher) -> None:
    """RAVEL parses markup it was told is markup, and nothing else.

    Bytes that might be HTML and might be a JPEG are not a page RAVEL may quote
    from. The bytes are still hashed and stored — the retrieval is honest about
    what it received — but no title and no excerpt are extracted from them.
    """
    respx.get(URL).mock(return_value=httpx.Response(200, content=PAGE))

    retrieval = fetcher.get(URL)

    assert retrieval.content_hash is not None
    assert retrieval.title == ""
    assert retrieval.excerpt == ""


@respx.mock
def test_a_source_with_no_readable_title_reports_none(fetcher: Fetcher) -> None:
    """A PDF has no title RAVEL can read, so it has none.

    Inventing one from the filename would put a string in a citation that the
    source never contained.
    """
    respx.get(URL).mock(
        return_value=httpx.Response(
            200, content=b"%PDF-1.4 binary", headers={"content-type": "application/pdf"}
        )
    )

    assert fetcher.get(URL).title == ""


@respx.mock
def test_the_excerpt_is_verbatim_and_has_the_scripting_removed(fetcher: Fetcher) -> None:
    """Short, checkable, and findable in the stored snapshot."""
    respx.get(URL).mock(return_value=httpx.Response(200, content=PAGE, headers=HTML))

    retrieval = fetcher.get(URL)

    assert "The measured value was 4.2 units" in retrieval.excerpt
    assert "var x = 1" not in retrieval.excerpt
    assert "color: red" not in retrieval.excerpt


@respx.mock
def test_a_redirect_records_both_the_url_asked_for_and_the_one_that_answered(
    fetcher: Fetcher,
) -> None:
    """How a DOI resolves to a publisher, kept visible afterwards."""
    final = "https://publisher.example.org/article/1"
    respx.get(URL).mock(return_value=httpx.Response(302, headers={"location": final}))
    respx.get(final).mock(return_value=httpx.Response(200, content=PAGE))

    retrieval = fetcher.get(URL)

    assert retrieval.requested_url == URL
    assert retrieval.final_url == final


def test_ravel_refuses_to_fetch_anonymously() -> None:
    """A one-line configuration stands between RAVEL and the throttled pool.

    Refusing is deliberate: every service this module talks to asks for a
    contact address from clients that make many requests, and fetching without
    one is a worse citizen than not fetching.
    """
    with pytest.raises(FetchError, match="RAVEL_RESEARCH_CONTACT_EMAIL"):
        Fetcher(Settings(env="test", research_contact_email=None, RAVEL_POSTGRES_DSN=None))


def test_the_user_agent_names_ravel_and_a_contact(settings: Settings) -> None:
    agent = user_agent(settings)
    assert agent.startswith("RAVEL/")
    assert "research@example.org" in agent
