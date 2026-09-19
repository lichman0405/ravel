"""A03 and A04: research that really went out, and evidence that says where from.

A03 is the one item in this matrix that a substitution could satisfy perfectly
and prove nothing about. Everywhere else, a mock leaves a record of the same
shape the real thing does; here the record's only value is that a service
somewhere answered. So the test does what a mock cannot survive: it searches
through a real Research session, takes an identifier out of what came back, and
asks the service *itself* — over a connection this process opened — whether that
identifier is real.

A04 is the ledger's side of the same claim. A row is written only from a
retrieval that happened, and it carries the tier RAVEL assigned, when the bytes
were read, the hash of those bytes, and whether they were readable at all. The
hash is checked by hashing the snapshot in the object store here, in this file,
rather than by comparing two values RAVEL reported.

Both skip without a contact address. That is not a gap in the suite: RAVEL will
not fetch anonymously, and a placeholder address would be a claim about who is
making the request.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from tests.dsh.mcp_probe import ProbeResult, ToolCall, probe
from tests.integration.conftest import Prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.config import Settings
from ravel.domain.enums import AccessStatus, EvidenceSourceTier
from ravel.domain.evidence import EvidenceSource
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.research import EvidenceSourceRepository
from ravel.state.store import ArtifactStore

pytestmark = [
    pytest.mark.acceptance,
    pytest.mark.live,
    pytest.mark.timeout(600),
]

#: A query with a well-indexed literature behind it, in the two domains A03
#: names: a bibliographic database and a chemical one.
QUERY = "niobium doped titanium dioxide conductivity"

#: Crossref's own API. The test asks it the same question RAVEL did, so that a
#: lead is confirmed by the service rather than by RAVEL's account of it.
CROSSREF_WORKS = "https://api.crossref.org/works/"

#: A source RAVEL can actually read. arXiv is open access by definition, which
#: is what A04 needs: its assertions are about bytes that were obtained, and a
#: publisher that refused an anonymous client would turn them into tests of the
#: refusal path instead. That path has its own tests, on this same URL space.
READABLE_URL = "https://arxiv.org/abs/1606.00335"

#: How stale a `retrieved_at` may be and still be this run's. Generous, because
#: the assertion is that it is a real reading time rather than one copied from
#: a record.
FRESHNESS = timedelta(minutes=15)


def _payload(result: ProbeResult, index: int, what: str) -> dict[str, Any]:
    """One call's structured payload, with the ways it can be absent ruled out.

    Raising here rather than three lines later is what makes a live failure
    legible: a run that fails because a service was slow says so, instead of
    failing on a subscript of `None`.
    """
    call: ToolCall = result.calls[index]
    assert not call.failed, f"{what} failed: {call.error}"
    assert call.payload is not None, f"{what} returned no structured payload"
    return call.payload


def _normalise(text: str) -> str:
    """Letters and digits only, so two spellings of one title can be compared."""
    return "".join(character for character in text.lower() if character.isalnum())


def _stored_bytes(store: ArtifactStore, snapshot_ref: str) -> bytes:
    """The snapshot, read back out of the object store."""
    return store.get(snapshot_ref)


def _digest(data: bytes) -> str:
    """The hash RAVEL records for a body, written out independently here.

    Not imported from RAVEL, for the reason every check like this is written
    out: a helper shared with the code under test would agree with it about a
    mistake.
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


async def _search(
    environment: RoleEnvironment, project: Project, sources: list[str]
) -> dict[str, Any]:
    """One `search_sources` call from a real Research session."""
    result = await probe(
        environment.for_project(project, AgentRole.RESEARCH),
        calls=(("search_sources", {"query": QUERY, "sources": sources, "limit": 5}),),
    )
    return _payload(result, 0, "search_sources")


def _registering(node_id: str) -> Callable[[tuple[ToolCall, ...]], dict[str, Any]]:
    """`register_source`'s arguments, once `open_source` has answered.

    The reference is read off the earlier reply rather than constructed, and it
    has to be: the bytes it names live in the tool server's memory and nowhere
    else, which is what makes registering a statement about a retrieval this
    process performed.
    """

    def arguments(outcomes: tuple[ToolCall, ...]) -> dict[str, Any]:
        opened = outcomes[0]
        assert not opened.failed, f"open_source failed: {opened.error}"
        assert opened.payload is not None, "open_source returned nothing structured"
        return {"retrieval_ref": opened.payload["retrieval_ref"], "node_id": node_id}

    return arguments


# ── A03 ─────────────────────────────────────────────────────────────────────


async def test_a03_a_research_session_searches_live_services_and_its_leads_are_real(
    live_role_environment: RoleEnvironment,
    live_settings: Settings,
    project: Project,
) -> None:
    """A03: "Research Agent performs live real Web/API/database research. No mock
    search."

    The second sentence is the whole test. A mocked search would return leads
    with the right shape and titles invented to look right, and every assertion
    about *shape* would pass. So the assertion is not about shape: the session
    searches Crossref and PubChem for real, and then this test takes a DOI out
    of what came back and asks Crossref — over its own connection, with its own
    request — whether that record exists and whether it is the one the lead
    named. A fabricated identifier cannot survive that, and neither can a lead
    whose title was made up.

    The connectors are named rather than left to default so that a service being
    down is a failure of the item and not a silent shrinking of it: the item
    says research is performed, and a search that reached nothing performed
    nothing. Which services could not answer is part of the result RAVEL
    returns, and it is asserted here rather than read past.
    """
    environment = live_role_environment
    searched = await _search(environment, project, ["crossref", "pubchem"])

    assert searched["available_connectors"], "no connector is configured"
    assert not searched["unavailable"], (
        f"a service did not answer, so this run did not search it: "
        f"{searched['unavailable']}"
    )
    assert "crossref" in searched["available_connectors"]

    leads = searched["leads"]
    assert leads, f"Crossref returned nothing for {QUERY!r}"

    # A lead is a pointer. Every one of them says so, which is what keeps a
    # search from being mistaken for evidence by anything downstream.
    assert all(lead["is_evidence"] is False for lead in leads)

    with_doi = [lead for lead in leads if lead["identifiers"].get("doi")]
    assert with_doi, (
        "no lead carried a DOI the provider supplied; RAVEL does not construct "
        f"identifiers, so this is the provider's answer: {leads[0]}"
    )
    lead = with_doi[0]
    doi = str(lead["identifiers"]["doi"])

    contact = live_settings.research_contact_email
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        recorded = await client.get(
            f"{CROSSREF_WORKS}{doi}",
            headers={"User-Agent": f"RAVEL-acceptance/0.1 (mailto:{contact})"},
        )
    assert recorded.status_code == 200, (
        f"Crossref does not know {doi}, which RAVEL returned as a lead: "
        f"{recorded.status_code}"
    )

    message = recorded.json()["message"]
    assert str(message["DOI"]).lower() == doi.lower(), (
        "the service answered about a different record than the lead named"
    )

    # And the title, which is the part a mock would have invented. Compared
    # normalized and by prefix, because a provider truncates and RAVEL may
    # strip markup — neither of which is a difference about what the work is
    # called.
    from_crossref = _normalise(message["title"][0])
    from_lead = _normalise(str(lead["title"]))
    assert from_crossref and from_lead, f"a title was empty: {lead['title']!r}"
    shorter, longer = sorted((from_crossref, from_lead), key=len)
    assert shorter[:40] in longer, (
        f"the lead says {lead['title']!r} and Crossref says "
        f"{message['title'][0]!r} for the same DOI"
    )


# ── A04 ─────────────────────────────────────────────────────────────────────


async def test_a04_a_source_enters_the_ledger_only_after_it_was_read(
    live_role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    artifact_store: ArtifactStore,
) -> None:
    """A04: "Evidence is registered only after original source verification;
    tier, retrieval timestamp, hash/ref, access status present."

    The order is the first claim, and it is checked by stopping in the middle:
    one session searches and opens a real source, and the ledger is read before
    anything registers it and found empty. A search result is a pointer and a
    reading is not yet a source — if either wrote a row, an unverified claim
    could be cited by anything downstream, and no later assertion would notice.

    The four fields are the second claim, and each is checked against something
    outside the row that reports it. The *hash* against the bytes in the object
    store, hashed here; the *timestamp* against this process's clock; the
    *tier* against the rule RAVEL states for it, with the rule itself recorded
    so a reader who disagrees can see what to argue with; the *access status*
    against what the fetch actually came back with.
    """
    node_id = research_task.node_id

    # One session searches and reads. Neither is registering.
    reading = await probe(
        live_role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("search_sources", {"query": QUERY, "sources": ["crossref"], "limit": 3}),
            ("open_source", {"url": READABLE_URL}),
        ),
    )
    searched = _payload(reading, 0, "search_sources")
    opened = _payload(reading, 1, "open_source")

    assert searched["leads"], "nothing was searched for, so the second half is unproven"
    assert opened["access_status"] == AccessStatus.OK.value, opened["note"]
    assert opened["content_hash"] is not None, "a source RAVEL read has a hash"
    assert opened["excerpt"], "a source RAVEL read has text, or nothing was read"

    with database.read_only() as session:
        ledger = EvidenceSourceRepository(session, project.project_id).all()
    assert ledger == [], (
        f"searching and opening wrote {len(ledger)} row(s) into the ledger; "
        "registration is a separate act, and a row written before it is a source "
        "nobody decided to keep"
    )

    # A second session registers what it opened. Registering is deliberate, and
    # it names the retrieval rather than a URL, so what enters the ledger is
    # bytes this process read.
    result = await probe(
        live_role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("open_source", {"url": READABLE_URL}),
            ("register_source", _registering(node_id)),
        ),
    )
    reopened = _payload(result, 0, "open_source")
    registered = _payload(result, 1, "register_source")

    source = registered["source"]
    assert isinstance(source, dict)
    assert registered["was_read"] is True

    assert source["url"] == reopened["final_url"], (
        "the ledger names the URL that was requested rather than the one the "
        "bytes came from"
    )
    assert source["content_hash"] == reopened["content_hash"]
    assert source["access_status"] == AccessStatus.OK.value
    # Constructed rather than compared as a string, so a tier that is not one of
    # the tiers raises here instead of passing as a word.
    tier = EvidenceSourceTier(registered["tier"]["tier"])
    assert registered["tier"]["rule"], (
        "a tier with no rule is a rating a reader cannot disagree with"
    )
    assert registered["tier"]["detail"]

    snapshot_ref = registered["snapshot_ref"]
    assert snapshot_ref is not None, "no snapshot was stored, so nothing is checkable"
    assert _digest(_stored_bytes(artifact_store, str(snapshot_ref))) == source["content_hash"], (
        "the hash in the ledger is not the hash of the bytes in the store"
    )

    retrieved_at = datetime.fromisoformat(str(source["retrieved_at"]))
    assert abs(datetime.now(UTC) - retrieved_at) < FRESHNESS, (
        f"{retrieved_at} is not when this run read the source"
    )

    with database.read_only() as session:
        written: EvidenceSource | None = EvidenceSourceRepository(
            session, project.project_id
        ).get(source_id=str(source["source_id"]))
        rows = EvidenceSourceRepository(session, project.project_id).all()

    assert written is not None, "the registration was reported but not written"
    assert len(rows) == 1, (
        f"registering one source wrote {len(rows)} rows; the ledger is a record "
        "of what was read, and a second row is a second reading nobody performed"
    )
    assert written.content_hash == source["content_hash"]
    assert written.snapshot_ref == snapshot_ref
    assert written.tier is tier
    assert written.was_read
