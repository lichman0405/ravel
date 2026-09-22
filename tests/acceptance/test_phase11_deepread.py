"""P11-02: a source RAVEL kept, it can read again — and quote.

`KNOWN_LIMITATIONS.md` L-25 was that RAVEL stored what it read and gave no seat
a way to read it back, so the most a Research session could ever see of a paper
it had obtained was a six-hundred-character excerpt. Everything downstream of
the ledger is built on the assumption that the snapshot is the thing a claim is
checked against, and until this item lands, nothing can check anything.

The cases here are the plan's §3.5 list, on the real stack: real PostgreSQL,
real MinIO, and the real Research tool server over real MCP stdio. What they
are *not* is scripted — sources are registered through the gateway that hashes
what it is given, and read back out of the object store, because a case that
called the reading functions directly would prove the functions work and say
nothing about whether a session can reach them.

The last case is live: a real paper off arXiv, read by page, with the phrase a
claim rests on checked against text extracted from the publisher's own bytes in
this file. That one carries the item's reason for existing — everything else can
be satisfied by a document this repository wrote, and the item is about the
documents it did not.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

import pytest
from pypdf import PdfReader
from tests.documents import a_pdf, a_scanned_pdf
from tests.dsh.mcp_probe import ProbeResult, ToolCall, probe
from tests.integration.conftest import Prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.config import Settings
from ravel.domain.enums import AccessStatus, ClaimClass
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.research.gateway import ResearchSourceGateway, SourceRequest
from ravel.research.leads import Retrieval
from ravel.state.database import Database
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
    ResearchRecordRepository,
)
from ravel.state.store import ArtifactStore, hash_chunks

pytestmark = [pytest.mark.phase11, pytest.mark.timeout(900)]

#: The paper the live case reads. arXiv is open access by definition, and the
#: identifier in this URL is printed on the paper itself, which is what makes an
#: assertion about the text an assertion about the bytes.
PAPER_ID = "1606.00335"
PAPER_URL = f"https://arxiv.org/pdf/{PAPER_ID}"

#: A methods section, as HTML, in the shape a session actually meets one: prose
#: with the parameters of a computation in it. The sentence the claim rests on
#: is in the body rather than in the abstract, which is the whole of what P11-02
#: makes reachable.
METHODS_URL = "https://example.org/papers/catalyst-methods.html"
METHODS_PAGE = (
    b"<html><head><title>Catalyst stability</title></head><body>"
    b"<h2>Methods</h2>"
    b"<p>The exchange-correlation functional was PBE, with a plane-wave cutoff of 520 eV.</p>"
    b"<p>The convergence threshold was 1e-6 eV per atom.</p>"
    b"</body></html>"
)

#: What that page says, as a fragment a claim can quote and a test can look for
#: in the stored bytes without asking RAVEL where it is.
PARAMETER = "plane-wave cutoff of 520 eV"


@dataclass(frozen=True, slots=True)
class Source:
    """A registered source, and what the ledger says about it."""

    source_id: str
    content_hash: str
    snapshot_ref: str


def _payload(result: ProbeResult, index: int, what: str) -> dict[str, Any]:
    """One call's structured payload, with the ways it can be absent ruled out."""
    call: ToolCall = result.calls[index]
    assert not call.failed, f"{what} failed: {call.error}"
    assert call.payload is not None, f"{what} returned no structured payload"
    return call.payload


def _stored(store: ArtifactStore, source: Source) -> bytes:
    """The bytes, out of the object store, hashed here rather than asked about.

    The one check that turns "the row describes a retrieval" into something a
    reader can act on: what is in the bucket hashes to what PostgreSQL says.
    """
    body = store.get(source.snapshot_ref)
    assert f"sha256:{hashlib.sha256(body).hexdigest()}" == source.content_hash
    return body


def _register(
    database: Database,
    settings: Settings,
    store: ArtifactStore,
    project: Project,
    *,
    body: bytes,
    url: str,
    media_type: str,
    node_id: str,
) -> Source:
    """Put bytes in the ledger the only way anything is put there.

    Through the gateway, which recomputes the hash from the body it is handed
    and refuses a retrieval whose hash does not describe its bytes, and through
    the real object store, which keeps them. A fixture that wrote the row
    itself could hand the reading path a source no registration could produce.
    """
    digest, _ = hash_chunks([body])
    retrieval = Retrieval(
        requested_url=url,
        final_url=url,
        access_status=AccessStatus.OK,
        retrieved_at=datetime(2026, 9, 22, 10, 0, tzinfo=UTC),
        content_hash=digest,
        size_bytes=len(body),
        media_type=media_type,
        body=body,
    )
    with database.transaction() as session:
        registration = ResearchSourceGateway(
            session=session, project_id=project.project_id, settings=settings, store=store
        ).register(
            SourceRequest(url=url, node_id=node_id),
            retrieval,
            actor_id=AgentRole.RESEARCH.value,
            node_id=node_id,
        )
    snapshot_ref = registration.source.snapshot_ref
    assert snapshot_ref is not None, "the gateway registered without storing the bytes"
    return Source(
        source_id=registration.source.source_id,
        content_hash=digest,
        snapshot_ref=snapshot_ref,
    )


# ── Reading what was stored ─────────────────────────────────────────────────


async def test_p11_02_a_stored_paper_is_read_again_by_the_page_it_is_on(
    role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """The item: what RAVEL kept, it can read — and not a byte more.

    Three pages are registered and one is read. The bounds are part of the
    claim as much as the text is: a session that could only ask for the whole
    document would put a paper in its context every time it wanted a sentence,
    and a session given more than it asked for would not know what it had read.
    """
    source = _register(
        database,
        integration_settings,
        artifact_store,
        project,
        body=a_pdf(
            [
                "Abstract: we studied the stability of a doped catalyst.",
                "Methods: the exchange-correlation functional was PBE.",
                "Results: retention was 91 percent after 500 hours.",
            ]
        ),
        url="https://example.org/papers/catalyst.pdf",
        media_type="application/pdf",
        node_id=research_task.node_id,
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": source.source_id}),
            ("read_source", {"source_ref": source.source_id, "pages": "2"}),
            ("read_source", {"source_ref": source.source_id, "pages": "1-3"}),
        ),
    )

    described = _payload(result, 0, "source_metadata")
    assert described["format"] == "pdf"
    assert described["page_count"] == 3
    assert described["text_extractable"] is True

    one_page = _payload(result, 1, "read_source")
    assert one_page["unit"] == "pages"
    (page,) = one_page["pages"]
    assert page["page"] == 2, "the page that was asked for, and only that one"
    assert page["text"] == "Methods: the exchange-correlation functional was PBE."
    assert one_page["next"] == {"pages": "3-3"}, "where to read next, computed by RAVEL"

    # Asking for three pages gets three; the cap is a cap on what one read
    # costs, not a target, and it is not a reason to return less than was asked
    # for when the document has it.
    three = _payload(result, 2, "read_source")
    assert [page["page"] for page in three["pages"]] == [1, 2, 3]
    assert three["truncated"] is False

    # The provenance is the ledger row's own fields, and the hash in it is the
    # hash of the bytes in the bucket — recomputed here.
    body = _stored(artifact_store, source)
    provenance = one_page["source"]
    assert provenance["source_id"] == source.source_id
    assert provenance["read_from"] == "snapshot", "the bytes came from the store, not a refetch"
    assert provenance["content_hash"] == f"sha256:{hashlib.sha256(body).hexdigest()}"
    assert provenance["hash_verified"] is True
    assert provenance["retrieved_at"] is not None, (
        "a reading with no retrieval time cannot be placed in the run that made it"
    )


async def test_p11_02_a_methods_parameter_is_read_out_of_the_body_and_cited(
    role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """§3.5's chain: read the body → write the claim → hand the task over.

    A research task's output is a claim resting on a source, and the claim is
    worth what the reading behind it is worth. The parameter here is in the
    body of the page and in no summary of it, so the assertion that matters is
    made against the bytes in the object store: the sentence the claim rests on
    is a sentence in what was kept.
    """
    source = _register(
        database,
        integration_settings,
        artifact_store,
        project,
        body=METHODS_PAGE,
        url=METHODS_URL,
        media_type="text/html",
        node_id=research_task.node_id,
    )
    node_id = research_task.node_id
    statement = f"The cited study used a {PARAMETER}."

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            # The task begins the way it begins in production: the seat asks
            # for it and the DAG decides. A hand-over from a node nobody began
            # is not a hand-over, which is what the record's own answer says.
            ("begin_research", {"node_id": node_id}),
            ("read_source", {"source_ref": source.source_id, "length": 2000}),
            (
                "record_evidence",
                {
                    "node_id": node_id,
                    "statement": statement,
                    "claim_class": ClaimClass.FACT.value,
                    "source_refs": [source.source_id],
                },
            ),
            (
                "submit_research_record",
                {
                    "node_id": node_id,
                    "question": "Which functional and cutoff were used?",
                    "recommended_followups": ["Check whether the cutoff was converged."],
                    "unknowns": ["Whether the same settings hold for the doped series."],
                },
            ),
        ),
    )

    began = _payload(result, 0, "begin_research")
    assert began["node_status"] == "RUNNING", f"the task did not begin: {began}"

    read = _payload(result, 1, "read_source")
    assert PARAMETER in str(read["text"]), "the body of the page, not a summary of it"
    assert read["source"]["source_id"] == source.source_id

    # The claim is the paper's, checked against the paper: the *stored bytes*
    # are searched here for the words the statement quotes, so a reading that
    # produced text from somewhere other than the source fails this line.
    stored = _stored(artifact_store, source).decode()
    assert "plane-wave cutoff of 520 eV" in stored
    assert "<p>" in stored, "the stored bytes are the page as it arrived, markup and all"

    claim = _payload(result, 2, "record_evidence")
    assert claim["claim_class"] == ClaimClass.FACT.value, (
        "a claim quoting a source RAVEL read is a fact"
    )
    assert claim["source_refs"] == [source.source_id]

    submitted = _payload(result, 3, "submit_research_record")
    assert submitted["handed_over"] is True, "the record was written and the node handed over"
    assert submitted["completion_status"] in {"COMPLETE", "INCOMPLETE"}, (
        "the status is RAVEL's judgement; INCOMPLETE with a reason is a correct answer"
    )

    with database.read_only() as session:
        claims = EvidenceRepository(session, project.project_id).for_task(node_id)
        sources = EvidenceSourceRepository(session, project.project_id).all()
        records = ResearchRecordRepository(session, project.project_id).for_node(node_id)
        artifacts = ArtifactRepository(session, project.project_id, artifact_store)
        # Every call on a repository happens inside the block that holds its
        # session. Reaching for a closed one checks out a connection nothing
        # returns, and the next test's TRUNCATE finds it.
        versions = artifacts.versions(str(sources[0].artifact_ref))
        verified = artifacts.verify(versions[0])

    assert [claim.statement for claim in claims] == [statement]
    assert claims[0].source_refs == (source.source_id,)
    assert [row.source_id for row in sources] == [source.source_id]
    assert len(records) == 1, "the record a later phase reads back was written once"

    # The whole chain, as a reader of the ledger would walk it: the record
    # names the task, the claim names the source, the source names a stored
    # object, and the object is verified against its own hash — which is what
    # makes any of it checkable by somebody who was not in the session.
    assert records[0].task_id == node_id
    assert sources[0].snapshot_ref == source.snapshot_ref
    assert versions[0].storage_key == source.snapshot_ref
    assert verified, "the stored page is the page that was read"


async def test_p11_02_a_scanned_paper_is_reported_and_never_invented(
    role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """The boundary the item exists to keep honest.

    A scanned paper has pages and no words. RAVEL does not do OCR, so the only
    correct answers are "there is text here" and "there is not" — and a reader
    that returned prose for an image would be the one failure this whole design
    is arranged against. So the test reads one, searches it and asks for its
    metadata, and requires all three to say the same thing.
    """
    source = _register(
        database,
        integration_settings,
        artifact_store,
        project,
        body=a_scanned_pdf(4),
        url="https://example.org/papers/scan.pdf",
        media_type="application/pdf",
        node_id=research_task.node_id,
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": source.source_id}),
            ("read_source", {"source_ref": source.source_id, "pages": "1-2"}),
            ("search_source", {"source_ref": source.source_id, "query": "functional"}),
        ),
    )

    described = _payload(result, 0, "source_metadata")
    assert described["readable"] is True, "the pages are there; the words are not"
    assert described["text_extractable"] is False
    assert "OCR" in str(described["format_note"])

    read = _payload(result, 1, "read_source")
    assert [page["text"] for page in read["pages"]] == ["", ""], (
        "a page with no text layer yields no text; anything else would be invented"
    )
    assert "OCR" in str(read["note"]) and "Report the gap" in str(read["note"])

    found = _payload(result, 2, "search_source")
    assert found["matches"] == []
    assert found["total_matches"] == 0
    assert "does not read words out of page images" in str(found["note"]), (
        "a search that finds nothing in a scan has to say that it could not look"
    )


async def test_p11_02_reading_a_source_changes_nothing_about_the_project(
    role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    integration_settings: Settings,
    artifact_store: ArtifactStore,
) -> None:
    """A look is not an act, and the ledger does not record one.

    Every tool on this surface is a read, and the way that stays true is that
    the tables do not move: sources, claims and artifacts are counted before
    four reads and after them, and the count is the same.
    """
    source = _register(
        database,
        integration_settings,
        artifact_store,
        project,
        body=METHODS_PAGE,
        url=METHODS_URL,
        media_type="text/html",
        node_id=research_task.node_id,
    )
    node_id = research_task.node_id

    def counts() -> tuple[int, int, int]:
        with database.read_only() as session:
            return (
                len(EvidenceSourceRepository(session, project.project_id).all()),
                len(EvidenceRepository(session, project.project_id).for_task(node_id)),
                len(ArtifactRepository(session, project.project_id).all()),
            )

    before = counts()
    assert before[0] == 1 and before[2] >= 1, "the source and its snapshot are there to begin with"

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": source.source_id}),
            ("read_source", {"source_ref": source.source_id}),
            ("read_source", {"source_ref": source.source_id, "start": 40, "length": 40}),
            ("search_source", {"source_ref": source.source_id, "query": "cutoff"}),
        ),
    )
    for index, call in enumerate(result.calls):
        assert not call.failed, f"call {index} failed: {call.error}"

    assert counts() == before


async def test_p11_02_the_reading_surface_belongs_to_the_ledgers_author(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """Who may read a snapshot is decided where who may write one is.

    Asked of every role over the real transport, which is the only way the
    answer means anything: the tools are not registered for anybody else, so
    there is no handler to reach rather than a prompt asking politely.
    """
    readers = {"read_source", "search_source", "source_metadata"}

    for role in AgentRole:
        registered = set((await probe(role_environment.for_project(project, role))).tools)
        if role is AgentRole.RESEARCH:
            assert readers <= registered
        else:
            assert not (readers & registered), (
                f"{role.value} holds {sorted(readers & registered)}: a source row "
                "describes bytes only the seat that read them may quote"
            )


# ── The live case ───────────────────────────────────────────────────────────


def _registering(node_id: str) -> Callable[[tuple[ToolCall, ...]], dict[str, object]]:
    """`register_source`'s arguments, taken from `open_source`'s own reply.

    The reference names bytes the tool server is holding, so it can only come
    off the earlier call in the same session — which is what makes the two
    calls one reading of the paper rather than two claims about a URL.
    """

    def arguments(outcomes: tuple[ToolCall, ...]) -> dict[str, object]:
        opened = outcomes[0]
        assert not opened.failed, f"open_source failed: {opened.error}"
        assert opened.payload is not None, "open_source returned nothing structured"
        return {"retrieval_ref": opened.payload["retrieval_ref"], "node_id": node_id}

    return arguments


@pytest.mark.live
async def test_p11_02_a_real_papers_body_is_read_and_quoted(
    live_role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    artifact_store: ArtifactStore,
) -> None:
    """The one case a document this repository wrote cannot satisfy.

    A publisher's PDF — real fonts, real encodings, a two-column body — is
    opened, registered, read back by page out of MinIO, searched, and turned
    into a claim. The phrase the claim quotes is checked against text extracted
    from the stored bytes *in this file*, so the reading is compared with the
    document rather than with RAVEL's account of it.

    Skipped without a contact address, like every live case here: RAVEL will not
    fetch anonymously, and a placeholder address would be a false claim about
    who is asking.
    """
    node_id = research_task.node_id
    environment = live_role_environment.for_project(project, AgentRole.RESEARCH)

    result = await probe(
        environment,
        calls=(
            ("open_source", {"url": PAPER_URL}),
            ("register_source", _registering(node_id)),
        ),
    )
    registered = _payload(result, 1, "register_source")
    assert registered["was_read"] is True, "RAVEL could not read the paper"
    row = registered["source"]
    assert isinstance(row, dict)
    source = Source(
        source_id=str(row["source_id"]),
        content_hash=str(row["content_hash"]),
        snapshot_ref=str(registered["snapshot_ref"]),
    )

    read = await probe(
        environment,
        calls=(("read_source", {"source_ref": source.source_id, "pages": "1"}),),
    )
    page = _payload(read, 0, "read_source")["pages"][0]
    text = str(page["text"])
    assert len(text) > 1000, f"page one of a paper yielded {len(text)} characters"
    assert PAPER_ID in text, (
        "the identifier arXiv prints on the paper. Nothing but the paper's own "
        "bytes can produce it"
    )

    # Independently: page one, extracted here from the stored snapshot.
    assert PdfReader(BytesIO(_stored(artifact_store, source))).pages[0].extract_text() == text

    # A phrase from the body, taken from a single printed line — a search is
    # literal, and a line break in the document is a character in the text.
    line = max((part.strip() for part in text.splitlines()), key=len)
    phrase = line[:40]
    searched = await probe(
        environment,
        calls=(("search_source", {"source_ref": source.source_id, "query": phrase}),),
    )
    found = _payload(searched, 0, "search_source")
    total = found["total_matches"]
    assert isinstance(total, int) and total >= 1, (
        f"{phrase!r} was read from page one and not found there"
    )

    claimed = await probe(
        environment,
        calls=(
            (
                "record_evidence",
                {
                    "node_id": node_id,
                    "statement": f"The paper's first page reads: {phrase}",
                    "claim_class": ClaimClass.FACT.value,
                    "source_refs": [source.source_id],
                },
            ),
        ),
    )
    assert _payload(claimed, 0, "record_evidence")["source_refs"] == [source.source_id]

    with database.read_only() as session:
        claims = EvidenceRepository(session, project.project_id).for_task(node_id)
        persisted = EvidenceSourceRepository(session, project.project_id).get(
            source_id=source.source_id
        )
    assert [claim.statement for claim in claims] == [f"The paper's first page reads: {phrase}"]
    assert persisted.content_hash == source.content_hash
    assert persisted.snapshot_ref == source.snapshot_ref
    assert persisted.was_read and persisted.access_status is AccessStatus.OK
