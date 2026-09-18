"""Real sources, opened for real, recorded as what they were.

Every test here reaches the open Internet. Nothing is mocked, and the module
will not run at all without a configured contact address, because RAVEL refuses
to fetch anonymously.

The claim being tested is narrow and load-bearing: **what the Evidence Ledger
says about a source is checkable against the source.** A registered row carries
a URL that answers, a timestamp from the moment bytes were read, and a hash of
bytes that are still in the object store. This suite re-opens the URL and
re-reads the stored object to check each of those, so a row that was written
from anything other than a real retrieval fails here.

Marked `live` and excluded from the default run: the default suite has to pass
on a machine with no network.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from ravel.domain.enums import AccessStatus
from ravel.domain.evidence import EvidenceSource
from ravel.research.gateway import (
    ResearchSourceGateway,
    SourceRefused,
    SourceRequest,
)
from ravel.research.search import SearchUnavailable
from ravel.state.repositories.research import EvidenceSourceRepository
from ravel.state.store import ArtifactStore

pytestmark = pytest.mark.live

#: A query with a stable, well-indexed literature behind it.
QUERY = "perovskite solar cell stability"

#: A source RAVEL can actually read. arXiv is open access by definition, which
#: makes it the right subject for the tests about reading and snapshotting:
#: they need a source whose bytes are really obtainable, and a publisher that
#: chose to block anonymous clients would turn them into tests of the paywall
#: path instead. Note that this is not a convenience — a readable source is a
#: different thing from a restricted one, and both have to be covered.
READABLE_URL = "https://arxiv.org/abs/1606.00335"

#: A DOI for supporting information. RAVEL reaches it through doi.org, which
#: redirects to the publisher, so the record's own declared type is what keeps
#: it from being rated as highly as the article it accompanies.
SUPPORTING_INFORMATION_DOI = "10.1021/acsaem.5c01035.s001"

#: A DOI for a peer-reviewed article, used to exercise the real search ->
#: lead -> open -> register path against a publisher that may or may not serve
#: an anonymous client.
ARTICLE_DOI = "10.1002/adfm.201808843"

#: A ScienceDirect article. Elsevier answers anonymous clients with 403, which
#: is the real restricted case: the work exists and RAVEL may not have it.
PAYWALLED_URL = "https://www.sciencedirect.com/science/article/pii/S0092867415000189"

#: How stale a `retrieved_at` may be and still be this run's. Generous, because
#: the assertion is that it is a real timestamp from now rather than one
#: fabricated or copied from a record.
FRESHNESS = timedelta(minutes=10)


def _sources(gateway: ResearchSourceGateway) -> list[EvidenceSource]:
    return EvidenceSourceRepository(gateway.session, gateway.project_id).all()


def _digest(data: bytes) -> str:
    """The hash RAVEL records for a body.

    Written out here rather than imported from RAVEL: a check that used the
    same helper the code under test uses would agree with it about a mistake.
    The `sha256:` prefix is asserted because it is the convention the artifact
    store writes, and a source's hash has to be comparable with its snapshot's.
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def test_searching_returns_leads_and_writes_no_evidence(
    gateway: ResearchSourceGateway,
) -> None:
    outcome = gateway.search(QUERY, sources=("crossref",), limit=5)

    assert outcome.leads, "Crossref returned nothing for a well-covered query"
    for lead in outcome.leads:
        assert lead.url.startswith(("http://", "https://"))
        assert lead.provider == "crossref"
    # The whole point of the lead/evidence split: a search result is not a
    # source until something was opened.
    assert _sources(gateway) == []


def test_an_opened_lead_yields_a_retrieval_of_real_bytes(
    gateway: ResearchSourceGateway,
) -> None:
    lead = gateway.lookup("1606.00335", source="arxiv")
    assert lead is not None, "arXiv does not know 1606.00335"

    retrieval = gateway.open_lead(lead)

    assert retrieval.was_read, f"could not read {lead.url}: {retrieval.note}"
    assert retrieval.content_hash is not None
    assert retrieval.size_bytes and retrieval.size_bytes > 0
    assert retrieval.body, "the body was not kept, so there is nothing to snapshot"
    # The hash describes the bytes that were read, not some earlier copy of them.
    assert retrieval.content_hash == _digest(retrieval.body)
    assert abs(datetime.now(UTC) - retrieval.retrieved_at) < FRESHNESS


def test_a_registered_source_hashes_the_bytes_that_are_in_the_store(
    gateway: ResearchSourceGateway,
    artifact_store: ArtifactStore,
) -> None:
    """The gate. Everything else in the ledger rests on this being true."""
    registration = gateway.obtain(
        SourceRequest.of(READABLE_URL, declared_type="posted-content"),
        actor_id="live-research-suite",
    )

    source = registration.source
    assert source.was_read

    # Read the snapshot back out of the object store and hash it independently.
    assert source.snapshot_ref is not None, "no snapshot was stored"
    stored = artifact_store.get(source.snapshot_ref)
    assert stored == registration.retrieval.body
    assert _digest(stored) == source.content_hash

    # And the same row as it exists in the database, not just the object we
    # happened to hold: the assertion is about what was written.
    persisted = EvidenceSourceRepository(gateway.session, gateway.project_id).get(
        source_id=source.source_id
    )
    assert persisted is not None
    assert persisted.content_hash == source.content_hash
    assert persisted.artifact_ref == source.artifact_ref
    assert persisted.access_status is AccessStatus.OK


def test_every_registered_source_names_a_url_that_answers(
    gateway: ResearchSourceGateway,
) -> None:
    """A source's URL is retrievable, checked by retrieving it."""
    gateway.obtain(SourceRequest.of(READABLE_URL), actor_id="live-research-suite")
    gateway.obtain(
        SourceRequest.of("https://pubchem.ncbi.nlm.nih.gov/compound/2244"),
        actor_id="live-research-suite",
    )
    sources = _sources(gateway)
    assert len(sources) == 2

    for source in sources:
        assert source.url.startswith("https://")
        assert source.retrieved_at is not None
        # Re-open it. This is a live request to the registered URL, and a 404
        # here means the row names something that is not there.
        again = gateway.fetcher_for.head(source.url)
        assert again.status_code is not None and again.status_code < 400, (
            f"{source.url} no longer answers: {again.status_code} {again.note}"
        )


def test_a_source_ravel_could_not_read_is_recorded_as_restricted(
    gateway: ResearchSourceGateway,
) -> None:
    """A paywall is a finding, and it carries no hash.

    If Elsevier ever serves this to an anonymous client the restriction cannot
    be exercised, and the test says so rather than asserting something weaker.
    """
    retrieval = gateway.open(PAYWALLED_URL)
    if retrieval.was_read:
        pytest.skip("this publisher served the article anonymously; nothing to restrict")

    registration = gateway.obtain(
        SourceRequest.of(PAYWALLED_URL), actor_id="live-research-suite"
    )
    source = registration.source

    assert source.access_status is not AccessStatus.OK
    assert source.content_hash is None, "a source RAVEL could not read has no hash"
    assert source.snapshot_ref is None
    assert source.was_read is False
    assert source.retrieved_at is None
    assert retrieval.note, "a restricted retrieval must say why"


def test_a_source_found_by_searching_is_recorded_as_what_it_turned_out_to_be(
    gateway: ResearchSourceGateway,
) -> None:
    """The whole path, end to end, over a source RAVEL did not choose.

    What the source turns out to be is not knowable in advance — a publisher
    may serve an anonymous client or refuse it — so the assertion is that the
    row matches the retrieval. A source that was read carries bytes and a hash;
    a source that was refused carries neither and says why. What is forbidden
    is a row that claims more than the retrieval obtained.
    """
    outcome = gateway.search(QUERY, sources=("crossref",), limit=5)
    assert outcome.leads

    registration = gateway.obtain(
        SourceRequest.from_lead(outcome.leads[0]), actor_id="live-research-suite"
    )
    source = registration.source
    retrieval = registration.retrieval

    assert source.url == retrieval.final_url
    assert source.access_status is retrieval.access_status
    if retrieval.was_read:
        assert source.content_hash == retrieval.content_hash
        assert source.snapshot_ref is not None, "a read source must have its bytes stored"
        assert source.retrieved_at is not None
    else:
        assert source.content_hash is None
        assert source.snapshot_ref is None
        assert source.retrieved_at is None
        assert retrieval.note, "a refusal has to say what refused it"
    # Recorded either way: a paywalled source is a finding, not a gap.
    assert _sources(gateway) == [source]


def test_a_head_is_not_evidence(gateway: ResearchSourceGateway) -> None:
    """Establishing that a URL answers is not reading it."""
    head = gateway.fetcher_for.head(READABLE_URL)
    assert head.was_read, "the URL should answer a HEAD"
    assert head.content_hash is None

    with pytest.raises(SourceRefused, match="was not read"):
        gateway.register(
            SourceRequest.of(head.final_url), head, actor_id="live-research-suite"
        )


def test_verify_confirms_an_unchanged_source(gateway: ResearchSourceGateway) -> None:
    """Verification re-opens the source and compares hashes.

    The comparison is only meaningful for a source that was read: a hash
    recorded against bytes RAVEL obtained can be checked against the bytes it
    obtains now, and a mismatch is a real finding about the source.
    """
    registration = gateway.obtain(SourceRequest.of(READABLE_URL), actor_id="live-research-suite")
    assert registration.was_read, "this test needs a source RAVEL can read"

    verification = gateway.verify(registration.source)

    assert verification.matches, verification.detail
    assert verification.observed_hash == verification.recorded_hash
    assert verification.recorded_hash == registration.source.content_hash


def test_a_records_own_declared_type_decides_its_tier(
    gateway: ResearchSourceGateway,
) -> None:
    """A tier with no reason is a number to be taken on faith.

    Both requests go through the real path — a connector's record becomes a
    lead, the lead becomes a request — because that is the only path on which
    the record's declared type survives. The domain alone cannot tell these two
    apart: doi.org redirects both to the publisher, where the host rule would
    rate supporting information exactly as highly as the article it belongs to.
    """
    article = gateway.lookup("10.1002/adfm.201808843")
    supporting = gateway.lookup(SUPPORTING_INFORMATION_DOI)
    assert article is not None and supporting is not None

    article_registration = gateway.obtain(
        SourceRequest.from_lead(article), actor_id="live-research-suite"
    )
    supporting_registration = gateway.obtain(
        SourceRequest.from_lead(supporting), actor_id="live-research-suite"
    )

    assert article_registration.tier.rule == "crossref:journal-article"
    assert article_registration.tier.tier.value == "A"
    assert supporting_registration.tier.rule == "crossref:component"
    assert supporting_registration.tier.tier.value == "B"
    # The reason travels with the source, so a decision resting on it can be
    # reviewed by someone who disagrees with the rule.
    assert supporting_registration.tier.rule in supporting_registration.source.notes
    assert "supporting information" in supporting_registration.source.notes


def test_the_other_connectors_reach_their_real_services(
    gateway: ResearchSourceGateway,
) -> None:
    """OpenAlex, arXiv and PubChem each answer with a real record.

    One test rather than three, because the assertion is the same for each and
    three live round trips to three services would triple the suite's runtime
    for no extra coverage.
    """
    openalex = gateway.search(QUERY, sources=("openalex",), limit=3)
    assert openalex.leads, "OpenAlex returned nothing"
    assert openalex.unavailable == ()

    arxiv = gateway.lookup("1606.00335", source="arxiv")
    assert arxiv is not None, "arXiv does not know 1606.00335"
    assert arxiv.identifiers["declared_type"] == "posted-content"
    assert arxiv.url.startswith("https://")

    aspirin = gateway.lookup("aspirin", source="pubchem")
    assert aspirin is not None, "PubChem does not know aspirin"
    assert aspirin.identifiers["pubchem_cid"] == "2244"
    assert "C9H8O4" in aspirin.snippet


def test_web_search_is_refused_rather_than_invented(
    gateway: ResearchSourceGateway,
) -> None:
    """With no provider configured, RAVEL says so.

    This test asserts the same thing whether or not a key is present: either a
    real provider answers with real results, or the absence of one is reported.
    What it forbids is the third outcome — an empty or plausible-looking list
    produced without asking anyone.
    """
    try:
        outcome = gateway.search_web(QUERY, limit=3)
    except SearchUnavailable as exc:
        assert "RAVEL_SEARCH_PROVIDER" in str(exc) or "RAVEL_SEARCH_API_KEY" in str(exc)
        return

    assert outcome.leads, "a configured provider returned nothing at all"
    for lead in outcome.leads:
        assert lead.url.startswith(("http://", "https://"))
        assert lead.provider in ("brave", "tavily", "serper", "exa")
