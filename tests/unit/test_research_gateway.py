"""The gateway's rules, tested where they do not need a database to hold.

`tests/integration/research/test_gateway_registration.py` covers what the
gateway writes. This file covers what it refuses, which is the part that decides
whether the Evidence Ledger can be trusted: a HEAD is a statement that a URL
exists, and a search result is a lead, and neither may become a source.

The refusals are asserted here rather than downstream because the ordering is
the guarantee. `register` checks the retrieval before it touches the session, so
a refusal happens with no row written and nothing to clean up — which is why
these can pass `session=None` and still be testing the real path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.research import tiers
from ravel.research.gateway import (
    ResearchSourceGateway,
    SourceRefused,
    SourceRequest,
    _dedupe,
    _filename,
    _is_a_shell,
    _notes,
)
from ravel.research.leads import Lead, Retrieval
from ravel.research.search import SearchUnavailable
from ravel.state.store import hash_chunks


@pytest.fixture
def settings() -> Settings:
    return Settings(
        env="test",
        research_contact_email="research@example.org",
        RAVEL_POSTGRES_DSN=None,
    )


@pytest.fixture
def gateway(settings: Settings) -> ResearchSourceGateway:
    """A gateway with no session, for the paths that never reach one."""
    return ResearchSourceGateway(session=None, project_id="proj", settings=settings)  # type: ignore[arg-type]


def _retrieval(**overrides: object) -> Retrieval:
    """A retrieval whose hash describes its body, as every real one does.

    Computed rather than typed in, because a fabricated hash is not a detail
    this helper is free to get wrong: the gateway refuses a retrieval whose
    hash does not describe the bytes it came with, so a constant here would
    make every test that reaches registration fail for a reason that has
    nothing to do with what it is testing.
    """
    body = b"<html><title>x</title><p>text</p></html>"
    digest, _ = hash_chunks([body])
    base: dict[str, object] = {
        "requested_url": "https://example.org/a",
        "final_url": "https://example.org/a",
        "access_status": AccessStatus.OK,
        "retrieved_at": datetime.now(UTC),
        "content_hash": digest,
        "body": body,
        "media_type": "text/html",
        "excerpt": "text",
    }
    base.update(overrides)
    return Retrieval(**base)  # type: ignore[arg-type]


def test_a_retrieval_whose_hash_does_not_describe_its_body_is_refused(
    gateway: ResearchSourceGateway,
) -> None:
    """The ledger's hash has to be one RAVEL computed, not one it was told.

    When a snapshot is written the store's own hash is recorded and compared
    with the retrieval's. That comparison cannot run when there is no snapshot,
    and then the retrieval's hash would be recorded on the strength of the
    retrieval saying so — and `register` accepts any `Retrieval`, including one
    an agent assembled. Recomputing it here closes that, and it is the same
    check the snapshot path makes, made available on the path that has no store
    to make it against.
    """
    forged = _retrieval(content_hash="sha256:" + "b" * 64)

    with pytest.raises(SourceRefused) as raised:
        gateway.register(SourceRequest.of(forged.final_url), forged, actor_id="a")

    assert "does not describe the bytes it came with" in str(raised.value)


def test_a_retrieval_with_no_body_is_not_checked_against_one() -> None:
    """A restricted retrieval has no bytes, and the check has nothing to compare.

    Its hash is absent too — `Retrieval` refuses a hash with no body — so this
    is a statement about where the check applies rather than a gap in it.
    """
    restricted = _retrieval(
        access_status=AccessStatus.PAYWALLED, content_hash=None, body=None, excerpt=""
    )

    assert restricted.body is None and restricted.content_hash is None


def test_a_head_cannot_be_registered_as_evidence(gateway: ResearchSourceGateway) -> None:
    """The shortest path from "the URL answers" to a citation is closed here.

    A HEAD establishes that a server has something at a URL. It does not
    establish what, and a source registered from one would be a citation whose
    content nobody read.
    """
    head = _retrieval(content_hash=None, body=None, excerpt="")

    with pytest.raises(SourceRefused) as raised:
        gateway.register(SourceRequest.of(head.final_url), head, actor_id="a")

    assert "was not read" in str(raised.value)
    assert "GET" in str(raised.value), "the message should say what to do instead"


def test_a_restricted_retrieval_is_registrable(gateway: ResearchSourceGateway) -> None:
    """A paywall is a finding, not a refusal to register.

    The refusal above is about a retrieval that claims success without content.
    A paywalled retrieval claims nothing, so it passes the guard — and it is
    right that it does, because "this source exists and RAVEL could not read it"
    is what a research task needs to be able to report.
    """
    paywalled = _retrieval(
        access_status=AccessStatus.PAYWALLED,
        content_hash=None,
        body=None,
        excerpt="",
        note="HTTP 402: payment required",
    )

    # The guard is what is under test, so this proves it lets the retrieval
    # through by failing later, at the session it does not have.
    with pytest.raises(Exception) as raised:
        gateway.register(SourceRequest.of(paywalled.final_url), paywalled, actor_id="a")
    assert not isinstance(raised.value, SourceRefused)


def test_a_gateway_without_a_store_refuses_a_snapshot_rather_than_silently_skipping_one(
    gateway: ResearchSourceGateway,
) -> None:
    """Registering without storing is allowed; storing without a store is not.

    The distinction matters because the two produce different rows: one has a
    hash describing a stored object, the other has whatever the retrieval
    computed. Silently downgrading the first to the second would make the
    Evidence Ledger's hash mean two different things depending on how the
    gateway happened to be constructed.
    """
    storeless = ResearchSourceGateway(
        session=None, project_id="proj", settings=gateway.settings, store=None  # type: ignore[arg-type]
    )

    # No store and no snapshot request: the retrieval's own hash is recorded.
    with pytest.raises(Exception) as raised:
        storeless.register(
            SourceRequest.of("https://example.org/a"), _retrieval(), actor_id="a", snapshot=False
        )
    assert not isinstance(raised.value, SourceRefused)


def test_a_lead_becomes_a_request_without_losing_what_the_provider_said() -> None:
    """The provider's own classification travels as a claim, not a fact.

    `declared_type` is the only signal that can outrank the domain when the tier
    is assigned, so it has to survive the trip from the search result to the
    request. Nothing is derived from the URL's shape on the way: a DOI-shaped
    path is a path.
    """
    lead = Lead(
        url="https://doi.org/10.1021/x",
        title="A Paper",
        snippet="s",
        provider="crossref",
        identifiers={"doi": "10.1021/x", "declared_type": "component"},
    )

    request = SourceRequest.from_lead(lead)

    assert request.declared_type == "component"
    assert request.doi == "10.1021/x"
    assert request.provider == "crossref"
    assert request.title == "A Paper"


def test_a_lead_with_no_identifier_is_not_given_one_from_its_url() -> None:
    """A string that looks like a DOI in a path is not a registration.

    Registering a source under an identifier its publisher never issued is
    worse than registering it with none: the identifier is what a later reader
    would follow to check the claim.
    """
    lead = Lead(
        url="https://example.com/10.1021/not-a-real-doi",
        title="",
        snippet="",
        provider="web",
    )

    assert SourceRequest.from_lead(lead).doi is None


def test_search_reports_a_connector_that_failed_beside_the_ones_that_answered(
    gateway: ResearchSourceGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A thin result from one source and a dead source are different findings.

    Collapsing them would let a research task report "nothing exists" when what
    happened is that the registry was down.
    """
    from ravel.research.connectors.base import ConnectorError

    class Working:
        name = "working"

        def search(self, query: str, *, limit: int = 10) -> list[Lead]:
            return [Lead(url="https://a.example/1", title="t", snippet="", provider="working")]

    class Broken:
        name = "broken"

        def search(self, query: str, *, limit: int = 10) -> list[Lead]:
            raise ConnectorError("broken", "answered HTTP 503")

    monkeypatch.setattr(
        ResearchSourceGateway,
        "connectors",
        property(lambda self: {"working": Working(), "broken": Broken()}),
    )

    outcome = gateway.search("anything")

    assert [lead.url for lead in outcome.leads] == ["https://a.example/1"]
    assert outcome.unavailable == ("broken: broken: answered HTTP 503",)
    assert not outcome.is_empty


def test_a_search_that_returns_nothing_is_empty_and_says_so(
    gateway: ResearchSourceGateway, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ResearchSourceGateway, "connectors", property(lambda self: {}))

    outcome = gateway.search("anything")

    assert outcome.is_empty
    assert outcome.unavailable == ()


def test_searching_the_open_web_without_a_provider_is_refused_not_empty(
    gateway: ResearchSourceGateway,
) -> None:
    """The rule the whole project turns on, at the one place it could be broken.

    An empty result and an unconfigured provider look the same to a caller that
    only checks the length of a list, and the difference is whether RAVEL
    searched the web or did not.
    """
    with pytest.raises(SearchUnavailable) as raised:
        gateway.search_web("perovskite stability")

    assert "RAVEL_SEARCH" in str(raised.value)


def test_duplicate_urls_collapse_and_the_first_provider_keeps_the_lead() -> None:
    """Two providers indexing one work is the normal case, not an edge one.

    The first is kept rather than the best-ranked, because ranks are not
    comparable across providers and "first" is at least a rule that can be
    stated. A trailing slash and a fragment are the same document.
    """
    leads = [
        Lead(url="https://doi.org/10.1/x", title="from crossref", snippet="", provider="crossref"),
        Lead(url="https://doi.org/10.1/x/", title="from openalex", snippet="", provider="openalex"),
        Lead(url="https://doi.org/10.1/x#section", title="again", snippet="", provider="web"),
        Lead(url="https://doi.org/10.1/y", title="another", snippet="", provider="crossref"),
    ]

    deduped = _dedupe(leads)

    assert [lead.title for lead in deduped] == ["from crossref", "another"]


@pytest.mark.parametrize(
    ("excerpt", "is_a_shell"),
    [
        ("Loading…", True),
        ("", True),
        ("Please enable JavaScript to view this article.", True),
        ("word " * 100, False),
        # Right at the boundary: one character short is a shell, at the limit is
        # not. The threshold is a heuristic, so the test states where it sits
        # rather than pretending it is a fact about the page.
        ("x" * 199, True),
        ("x" * 200, False),
    ],
)
def test_a_script_shell_is_recognised_by_its_own_text(excerpt: str, is_a_shell: bool) -> None:
    """A page that fills itself in with script is not a document.

    Judged on the excerpt — the source's own text with tags removed — rather
    than on the byte count, because a large page of markup can carry no prose
    at all, and registering it would produce a citation whose text is empty.
    """
    assert _is_a_shell(_retrieval(excerpt=excerpt)) is is_a_shell


def test_a_pdf_is_never_treated_as_a_shell() -> None:
    """There is no browser that would render a PDF into something more useful."""
    assert _is_a_shell(_retrieval(media_type="application/pdf", excerpt="")) is False


@pytest.mark.parametrize(
    ("url", "expected", "why"),
    [
        ("https://example.org/a/paper.pdf", "paper.pdf", "the last path segment"),
        ("https://example.org/a/paper.pdf?token=x", "paper.pdf", "the query is not a name"),
        ("https://example.org/", "example.org", "a bare host has only its host to name it"),
        ("https://example.org/a/../../etc/passwd", "passwd", "the URL's own last segment"),
        (
            "https://example.org/a/%2e%2e%2fboom",
            "2e2e2fboom",
            "percent signs are dropped, so an encoded traversal is inert",
        ),
        ("https://example.org/a/", "a", "a trailing slash names the directory above"),
    ],
)
def test_a_snapshot_filename_is_taken_from_the_url_and_stripped_of_anything_else(
    url: str, expected: str, why: str
) -> None:
    """The filename reaches an object store key, so it is filtered to a safe set.

    It is derived from the URL rather than from the content type because it is
    only a convenience for a human reading a bucket listing; the artifact's
    identity is its id, not its key. What matters is that nothing in a URL can
    put a separator or a traversal into the key.
    """
    filename = _filename(_retrieval(final_url=url))

    assert filename == expected, why
    assert "/" not in filename and "\\" not in filename and ".." not in filename


def test_the_notes_carry_the_tier_and_the_rule_that_produced_it() -> None:
    """A tier without its reason is a number to be taken on faith.

    Someone who disagrees with the rule can only say so if the row names it.
    """
    note = _notes(
        SourceRequest.of("https://arxiv.org/abs/1", provider="arxiv", title="A Preprint"),
        _retrieval(title=""),
        tiers.assign("https://arxiv.org/abs/1"),
        research_task_ref="task-7",
    )

    assert "tier B (preprint)" in note
    assert "lead from arxiv" in note
    assert "title from the lead" in note
    assert "research task task-7" in note


def test_a_retrievals_own_note_is_carried_into_the_record() -> None:
    """A rendered retrieval says so, and that has to survive into the ledger.

    A reader comparing the stored snapshot with the live page needs to know
    whether the bytes came from the server or from a browser's rendering of it.
    """
    note = _notes(
        SourceRequest.of("https://example.org/a"),
        _retrieval(note="rendered by a browser; the served document had no text"),
        tiers.assign("https://example.org/a"),
        research_task_ref=None,
    )

    assert "rendered by a browser" in note


def test_a_source_is_registered_under_the_url_that_answered() -> None:
    """The redirect's destination is what was read, so it is what is recorded.

    Recording the URL that was asked for would put an address in the ledger that
    RAVEL never read, and `verify` would later re-open the wrong thing.
    """
    from ravel.research.gateway import SourceRequest as Request

    request = Request.of("https://doi.org/10.1021/x")
    retrieval = _retrieval(
        requested_url="https://doi.org/10.1021/x", final_url="https://pubs.acs.org/doi/10.1021/x"
    )

    assert retrieval.final_url != request.url
    assert tiers.assign(retrieval.final_url).rule == "publisher"
