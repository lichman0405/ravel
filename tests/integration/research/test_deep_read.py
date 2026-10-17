"""Reading a registered source back, through the server a session actually gets.

`tests/unit/test_deepread.py` settles what the reader does with bytes. This
settles the question the reader exists for, and it is a question about the
stack rather than about the parser: given a row in the Evidence Ledger in a
real PostgreSQL and an object in a real MinIO, can a Research session read the
document again — and is what it reads the thing the row describes?

Everything here runs through `python -m ravel.mcp.server` over real MCP stdio,
the way the harness launches one. That matters for the two properties that are
about wiring rather than about code: that the bytes come out of the *snapshot*
and are hashed again before anybody sees them, and that the three reading tools
are a territory no other role's server registers. A test that called the
handlers in-process would prove the functions work and say nothing about
whether a session can reach them.

Nothing here touches the network. Sources are registered through the gateway
with bytes this module wrote, which is the one path into the ledger and the
path that hashes what it is given; the reading is then the real read path, in a
different process, out of the real object store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from tests.documents import a_pdf, a_scanned_pdf
from tests.dsh.mcp_probe import ToolCall, probe
from tests.integration.conftest import Prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.config import Settings
from ravel.domain.clock import json_iso
from ravel.domain.enums import AccessStatus, EvidenceSourceTier
from ravel.domain.evidence import EvidenceSource
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.research.gateway import ResearchSourceGateway, SourceRequest
from ravel.research.leads import Retrieval
from ravel.state.database import Database
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
)
from ravel.state.store import ArtifactStore, hash_chunks

URL = "https://example.org/articles/catalyst-stability"

#: A page longer than one read and shorter than the cap, so that both a bounded
#: region and the whole document can be asked for in the same test — which is
#: what makes "the regions join up" a comparison rather than an assurance.
#: Written so that every part of it is recognisably itself: the sentence carries
#: its own number.
LONG_PAGE = (
    "<html><head><title>Catalyst Stability</title></head><body>"
    + "".join(f"<p>Sentence {number} of the methods section.</p>" for number in range(1, 120))
    + "</body></html>"
).encode()

METHODS_PAGE = b"""<html><head><title>Catalyst Stability</title></head><body>
<p>The retention was measured by X-ray diffraction.</p>
<p>The exchange-correlation functional was PBE, with a plane-wave cutoff of 520 eV.</p>
</body></html>"""


@dataclass(frozen=True, slots=True)
class Stored:
    """A source registered in the ledger, and what its bytes say."""

    source: EvidenceSource
    body: bytes

    @property
    def source_id(self) -> str:
        return self.source.source_id


def a_registered_source(
    database: Database,
    project: Project,
    settings: Settings,
    store: ArtifactStore,
    *,
    body: bytes = METHODS_PAGE,
    media_type: str = "text/html",
    url: str = URL,
    snapshot: bool = True,
    node_id: str | None = None,
) -> Stored:
    """Register bytes in the ledger the only way anything is registered.

    Through `ResearchSourceGateway.register`, which recomputes the hash from
    the body it is handed and refuses a retrieval whose hash does not describe
    its bytes. A fixture that wrote the row itself would let a test assert
    against a row no reader could have produced.
    """
    digest, _ = hash_chunks([body])
    retrieval = Retrieval(
        requested_url=url,
        final_url=url,
        access_status=AccessStatus.OK,
        retrieved_at=datetime(2026, 9, 22, 9, 30, tzinfo=UTC),
        content_hash=digest,
        size_bytes=len(body),
        media_type=media_type,
        body=body,
    )
    with database.transaction() as session:
        registration = ResearchSourceGateway(
            session=session,
            project_id=project.project_id,
            settings=settings,
            store=store,
        ).register(
            SourceRequest(url=url, node_id=node_id),
            retrieval,
            actor_id=AgentRole.RESEARCH.value,
            node_id=node_id,
            snapshot=snapshot,
        )
        source = EvidenceSourceRepository(session, project.project_id).get(
            source_id=registration.source.source_id
        )
    return Stored(source=source, body=body)


def payload(call: ToolCall) -> dict[str, object]:
    """One call's structured result, with the ways it can be absent ruled out."""
    assert not call.failed, f"{call.tool} failed: {call.error}"
    assert call.payload is not None, f"{call.tool} returned no structured payload"
    return call.payload


def provenance(call: ToolCall) -> dict[str, object]:
    """The `source` block one of the reading tools returned."""
    source = payload(call)["source"]
    assert isinstance(source, dict), f"{call.tool} returned no source block"
    return source


# ── The read itself ─────────────────────────────────────────────────────────


async def test_a_registered_source_is_read_back_out_of_its_snapshot(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """The whole point of the item, in one case.

    A source RAVEL stored in full can be read again — by a different process,
    out of the object store, months after the URL changed — and what comes back
    is the document. The provenance block is the ledger row's own fields, so
    the text can be cited back to the row it came from.
    """
    stored = a_registered_source(
        database, project, integration_settings, artifact_store, body=METHODS_PAGE
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id}),),
    )

    read = payload(result.calls[0])
    text = str(read["text"])
    assert "The retention was measured by X-ray diffraction." in text
    assert "The exchange-correlation functional was PBE" in text, (
        "the paper's own sentence about its method, which is the thing an excerpt "
        "could never carry"
    )
    assert read["format"] == "html"
    assert read["unit"] == "chars"
    assert read["truncated"] is False
    assert read["total_chars"] == len(text), "a document read to its end is all of it"
    assert read["next"] is None, "and there is no next region"

    # Every field, so that a provenance block that grew a field nobody meant it
    # to have is a failing test rather than a larger result.
    assert provenance(result.calls[0]) == {
        "source_ref": stored.source_id,
        "source_id": stored.source_id,
        "read_from": "snapshot",
        "url": URL,
        "title": stored.source.title,
        "media_type": "text/html",
        "content_hash": stored.source.content_hash,
        "retrieved_at": json_iso(stored.source.retrieved_at),  # type: ignore[arg-type]
        "snapshot_ref": stored.source.snapshot_ref,
        "access_status": "OK",
        "tier": stored.source.tier.value if stored.source.tier else None,
        "hash_verified": True,
    }


async def test_the_bytes_a_read_returns_are_the_bytes_the_row_describes(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """Checked here by reading the object out of MinIO and hashing it.

    The provenance block says the hash was verified, and a claim like that is
    worth nothing on its own: this compares the stored object with the row, and
    the row with what the session was told, without asking RAVEL either time.
    """
    stored = a_registered_source(database, project, integration_settings, artifact_store)
    assert stored.source.snapshot_ref is not None

    digest, _ = hash_chunks([artifact_store.get(stored.source.snapshot_ref)])
    assert digest == stored.source.content_hash

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id}),),
    )
    assert provenance(result.calls[0])["content_hash"] == digest


async def test_a_read_is_a_bounded_region_and_says_where_the_next_one_is(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """A long document is read a region at a time, and the regions join up.

    The second call uses exactly the arguments the first returned, which is
    what makes deep reading a loop a session can drive without losing its place
    in the document — and it is the property that keeps a session's context the
    size the session chose.
    """
    stored = a_registered_source(
        database, project, integration_settings, artifact_store, body=LONG_PAGE
    )

    whole = payload(
        (
            await probe(
                role_environment.for_project(project, AgentRole.RESEARCH),
                calls=(("read_source", {"source_ref": stored.source_id, "length": 24000}),),
            )
        ).calls[0]
    )

    first = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id, "length": 400}),),
    )
    region = payload(first.calls[0])
    assert len(str(region["text"])) == 400
    assert region["start"] == 0 and region["end"] == 400
    assert region["truncated"] is True
    assert region["next"] == {"start": 400, "length": 400}, (
        "the result carries the arguments of the next read, so a session does not "
        "have to work out where it got to"
    )

    second = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id, **region["next"]}),),  # type: ignore[arg-type]
    )
    continued = payload(second.calls[0])
    assert continued["start"] == region["end"], "the next region begins where the last ended"
    assert continued["total_chars"] == whole["total_chars"], (
        "and it is a region of the same document, in characters"
    )
    assert str(region["text"]) + str(continued["text"]) == str(whole["text"])[:800], (
        "two bounded reads are the first eight hundred characters of the document, "
        "which is what makes a deep read a reading of the paper rather than a "
        "sampling of it"
    )


async def test_a_read_never_returns_more_than_the_cap(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """Asking for a megabyte is refused rather than quietly narrowed.

    A caller that asked for the whole paper and got six thousand characters
    would believe it had read the paper.
    """
    stored = a_registered_source(
        database, project, integration_settings, artifact_store, body=LONG_PAGE
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id, "length": 1_000_000}),),
    )

    call = result.calls[0]
    assert call.failed
    assert "at most 24000 characters" in (call.error or "")


# ── Searching inside a source ───────────────────────────────────────────────


async def test_a_search_finds_the_offset_a_read_then_uses(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """The two tools are one move: find the phrase, read the passage it is in.

    Through the server, because the offsets are the server's: a search that
    reported an offset into a different rendering than the read returns would
    be a search whose results cannot be checked, and that is exactly the
    failure a snippet-based research flow has.
    """
    stored = a_registered_source(database, project, integration_settings, artifact_store)

    found = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("search_source", {"source_ref": stored.source_id, "query": "plane-wave cutoff"}),),
    )
    match = payload(found.calls[0])["matches"][0]  # type: ignore[index]
    assert isinstance(match, dict)
    assert "plane-wave cutoff of 520 eV" in str(match["context"])
    assert payload(found.calls[0])["total_matches"] == 1
    assert payload(found.calls[0])["unit"] == "chars"

    read = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            (
                "read_source",
                {"source_ref": stored.source_id, "start": match["offset"], "length": 60},
            ),
        ),
    )
    assert str(payload(read.calls[0])["text"]).startswith("plane-wave cutoff of 520 eV")


async def test_a_search_that_finds_nothing_says_so_about_the_source(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """A miss is a fact about this document, and the result says which document.

    The failure mode is a session that searched one source, found nothing, and
    reports that the literature does not say it. The provenance block is what
    makes that misreading visible, because it names the source the search was
    of.
    """
    stored = a_registered_source(database, project, integration_settings, artifact_store)

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("search_source", {"source_ref": stored.source_id, "query": "graphene oxide"}),),
    )

    found = payload(result.calls[0])
    assert found["matches"] == []
    assert found["total_matches"] == 0
    assert provenance(result.calls[0])["source_id"] == stored.source_id


# ── What cannot be read, and why ────────────────────────────────────────────


async def test_a_source_stored_without_a_snapshot_cannot_be_read(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """Registering without storing is allowed; reading it back is not possible.

    The refusal names the reason and the way forward, because "there is no
    stored copy" is a fact about the registration and the session can fix it by
    opening the URL again.
    """
    stored = a_registered_source(
        database, project, integration_settings, artifact_store, snapshot=False
    )
    assert stored.source.snapshot_ref is None

    described = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("source_metadata", {"source_ref": stored.source_id}),),
    )
    metadata = payload(described.calls[0])
    assert metadata["readable"] is False
    assert metadata["bytes_available"] is False
    assert "no stored copy" in str(metadata["read_refusal"])

    read = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id}),),
    )
    assert read.calls[0].failed
    assert "open_source" in (read.calls[0].error or ""), (
        "a refusal has to say how to get a reading that works"
    )


async def test_a_source_that_was_never_read_has_nothing_to_read(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
) -> None:
    """A paywall is a finding, and it stays a finding when somebody asks again.

    `read_source` must not produce text for a source RAVEL could not obtain:
    the temptation this whole design is built against is a session that fills
    in what a paper it cannot open probably says.
    """
    with database.transaction() as session:
        EvidenceSourceRepository(session, project.project_id).record(
            EvidenceSource(
                project_id=project.project_id,
                url="https://pubs.acs.org/doi/10.1021/locked",
                access_status=AccessStatus.PAYWALLED,
                tier=EvidenceSourceTier.A,
            )
        )
        source = EvidenceSourceRepository(session, project.project_id).readable()
    assert source == [], "a paywalled source is not one RAVEL read"
    with database.read_only() as session:
        (locked,) = EvidenceSourceRepository(session, project.project_id).all()

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": locked.source_id}),
            ("read_source", {"source_ref": locked.source_id}),
        ),
    )

    described = payload(result.calls[0])
    assert described["readable"] is False
    assert provenance(result.calls[0])["access_status"] == "PAYWALLED"
    assert "PAYWALLED" in str(described["read_refusal"])

    assert result.calls[1].failed
    assert "no bytes to read" in (result.calls[1].error or "")


async def test_bytes_that_disagree_with_the_row_are_refused_rather_than_read(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """The check that makes a read evidence-grade, provoked on demand.

    The object under the snapshot's key is replaced — which is what a corrupted
    store, a lifecycle rule that rewrote an object, or a transcode leaves
    behind — and the ledger row is left exactly as it was, because the ledger
    is not the thing that drifted. The bytes no longer hash to what the row
    records, so the read stops and names both hashes: whoever is looking at
    this is the one who has to decide what happened to the store.
    """
    stored = a_registered_source(database, project, integration_settings, artifact_store)
    recorded = stored.source.content_hash
    assert stored.source.snapshot_ref is not None and recorded is not None

    artifact_store.delete(stored.source.snapshot_ref)
    artifact_store.put(
        stored.source.snapshot_ref, [b"<html><body>Not the paper.</body></html>"]
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": stored.source_id}),
            ("read_source", {"source_ref": stored.source_id}),
        ),
    )

    assert result.calls[0].failed, "metadata describes the bytes, so it refuses them too"
    call = result.calls[1]
    assert call.failed
    assert "not the bytes that were read" in (call.error or "")
    assert recorded in (call.error or ""), "the message says what the ledger expected"
    assert call.payload is None, "and no text is returned under that provenance"


async def test_another_projects_source_is_not_readable_here(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
    other_project: Project,
) -> None:
    """A source is project-scoped, like every other record.

    The bytes are in the same bucket and the row is in the same database, and
    the session holding neither scope cannot reach it: the lookup is scoped
    before the read, so this is a refusal rather than a leak with a check on
    top of it.
    """
    stored = a_registered_source(
        database, other_project, integration_settings, artifact_store
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id}),),
    )

    call = result.calls[0]
    assert call.failed
    assert "not holding a source or a retrieval" in (call.error or "")


async def test_reading_writes_nothing(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
    research_task: Prepared,
) -> None:
    """A read is not a finding, and the ledger does not record one.

    Counted in PostgreSQL and in the bucket: sources, claims and artifacts are
    the same after three reads and a search as before them. A ledger that
    recorded every look would be a ledger of sessions rather than of sources,
    and the counts here are what says it is not.
    """
    stored = a_registered_source(
        database,
        project,
        integration_settings,
        artifact_store,
        body=a_pdf(["Methods: the force field was UFF."]),
        media_type="application/pdf",
        node_id=research_task.node_id,
    )

    def counts() -> tuple[int, int, int]:
        with database.read_only() as session:
            return (
                len(EvidenceSourceRepository(session, project.project_id).all()),
                len(EvidenceRepository(session, project.project_id).all()),
                len(ArtifactRepository(session, project.project_id).all()),
            )

    before = counts()
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": stored.source_id}),
            ("read_source", {"source_ref": stored.source_id}),
            ("search_source", {"source_ref": stored.source_id, "query": "force field"}),
            ("read_source", {"source_ref": stored.source_id, "pages": "1"}),
        ),
    )
    for call in result.calls:
        assert not call.failed, call.error
    assert counts() == before


# ── PDFs ────────────────────────────────────────────────────────────────────


async def test_a_pdf_is_read_by_page_and_its_text_is_the_body(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """A PDF stored as a PDF is read as its pages' words.

    This is the case L-25 was written about: the source carrying the answer was
    a paper, the paper was a PDF, and every word of it was unreachable.
    """
    stored = a_registered_source(
        database,
        project,
        integration_settings,
        artifact_store,
        body=a_pdf(
            [
                "Abstract: we studied a catalyst.",
                "Methods: the exchange-correlation functional was PBE.",
                "Results: retention was 91 percent.",
            ]
        ),
        media_type="application/pdf",
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": stored.source_id}),
            ("read_source", {"source_ref": stored.source_id, "pages": "2"}),
            ("search_source", {"source_ref": stored.source_id, "query": "retention"}),
        ),
    )

    described = payload(result.calls[0])
    assert described["format"] == "pdf"
    assert described["page_count"] == 3
    assert described["text_extractable"] is True

    read = payload(result.calls[1])
    assert read["unit"] == "pages"
    (page,) = read["pages"]  # type: ignore[misc]
    assert page["page"] == 2
    assert page["text"] == "Methods: the exchange-correlation functional was PBE."
    assert read["next"] == {"pages": "3-3"}, "the page after the one read"

    found = payload(result.calls[2])
    (match,) = found["matches"]  # type: ignore[misc]
    assert match["page"] == 3
    assert "91 percent" in match["context"]


async def test_a_scanned_pdf_says_it_has_no_text_instead_of_nothing(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """The explicit unsupported report, at the tool boundary.

    A session that reads a scanned paper gets pages, no words, and a sentence
    naming the reason. What it must not get is an empty string with no
    explanation, which reads as "this paper says nothing about it".
    """
    stored = a_registered_source(
        database,
        project,
        integration_settings,
        artifact_store,
        body=a_scanned_pdf(3),
        media_type="application/pdf",
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": stored.source_id}),
            ("read_source", {"source_ref": stored.source_id, "pages": "2-3"}),
            ("search_source", {"source_ref": stored.source_id, "query": "retention"}),
        ),
    )

    described = payload(result.calls[0])
    assert described["text_extractable"] is False
    assert "OCR" in str(described["format_note"])

    read = payload(result.calls[1])
    assert [page["text"] for page in read["pages"]] == ["", ""]  # type: ignore[union-attr]
    assert "OCR" in str(read["note"])
    assert "Report the gap rather than the contents" in str(read["note"])

    found = payload(result.calls[2])
    assert found["matches"] == []
    assert "does not read words out of page images" in str(found["note"])


async def test_a_pdf_refuses_a_character_region(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """Two addressing schemes, and the wrong one is refused rather than ignored.

    Silently reading page one for a caller that asked for characters 5000-6000
    would answer a question nobody asked with text from somewhere else.
    """
    stored = a_registered_source(
        database,
        project,
        integration_settings,
        artifact_store,
        body=a_pdf(["one page"]),
        media_type="application/pdf",
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id, "start": 5000, "length": 1000}),),
    )

    call = result.calls[0]
    assert call.failed
    assert "read by page" in (call.error or "")


async def test_a_character_offset_is_refused_for_a_source_that_has_pages(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """And the other way round: `pages` on a document without pages."""
    stored = a_registered_source(database, project, integration_settings, artifact_store)

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": stored.source_id, "pages": "1"}),),
    )

    call = result.calls[0]
    assert call.failed
    assert "use `start` and `length`" in (call.error or "")


async def test_a_metadata_refusal_is_not_a_stored_byte(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """The reference has to be one the ledger issued — not a URL or a title.

    `read_source` takes a reference and nothing else, so a session cannot ask
    it to fetch something and call the result a reading: the only bytes it can
    reach are ones a row already describes.
    """
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": URL}),),
    )

    call = result.calls[0]
    assert call.failed
    assert "not holding a source or a retrieval" in (call.error or "")


# ── Who may read ────────────────────────────────────────────────────────────


async def test_reading_tools_are_not_registered_for_any_other_role(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """The roster, not a prompt: no other server has these tools to call.

    Every role is asked, including the ones that hold reading tools of their
    own. What a seat may read out of a snapshot is the counterpart of what it
    may write into the ledger, and the ledger has one author.
    """
    readers = {"read_source", "search_source", "source_metadata"}

    for role in AgentRole:
        result = await probe(role_environment.for_project(project, role))
        if role is AgentRole.RESEARCH:
            assert readers <= set(result.tools), "Research's server must register the readers"
        else:
            assert not (readers & set(result.tools)), (
                f"{role.value} was given {readers & set(result.tools)}, and the reading "
                "surface is the ledger author's"
            )
