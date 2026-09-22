"""Reading a real paper's words back out of the store that holds it.

`tests/integration/research/test_deep_read.py` proves the reading path over the
real database, the real object store and the real transport, and it does so
with bytes this repository wrote. What it cannot prove is the thing the whole
item exists for: that a *publisher's* PDF — the actual file at the end of a
real URL, with the fonts, the encodings and the two-column layout that no
fixture has — yields its body text.

So this module opens a real paper over the wire, registers it, and reads it
again by `source_id` out of MinIO. The page it reads is checked against the
paper's own arXiv identifier, which every arXiv PDF carries in the margin and
which no landing page, snippet or abstract can supply: if that string comes
back, the bytes that were read are the paper's, and the words around it are the
paper's too.

Marked `live`, like the rest of this suite. Nothing here is mocked, and the
paper is not bundled: if arXiv does not answer, these tests fail rather than
read a fixture.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from io import BytesIO

import pytest
from pypdf import PdfReader
from tests.dsh.mcp_probe import ProbeResult, ToolCall, probe
from tests.integration.conftest import Prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.research import EvidenceSourceRepository
from ravel.state.store import ArtifactStore

pytestmark = pytest.mark.live

#: A real paper, as a PDF. The identifier in the URL is the identifier printed
#: on the paper, which is what makes an assertion about it an assertion about
#: the bytes rather than about the URL RAVEL was asked for.
PAPER_ID = "1606.00335"
PAPER_URL = f"https://arxiv.org/pdf/{PAPER_ID}"

#: A real page of prose. Any paper with real text in it clears this by a wide
#: margin; a stub, an error page or a scanned image does not.
A_PAGE_OF_PROSE = 1000


def _payload(result: ProbeResult, index: int) -> dict[str, object]:
    """One call's structured payload, with the ways it can be absent ruled out."""
    call: ToolCall = result.calls[index]
    assert not call.failed, f"{call.tool} failed: {call.error}"
    assert call.payload is not None, f"{call.tool} returned no structured payload"
    return call.payload


def _registering(node_id: str) -> Callable[[tuple[ToolCall, ...]], dict[str, object]]:
    """`register_source`'s arguments, taken from `open_source`'s reply.

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


async def _a_registered_paper(
    environment: RoleEnvironment,
    project: Project,
    node_id: str,
) -> tuple[str, str]:
    """Open the paper, register it, and return `(source_id, snapshot_ref)`."""
    result = await probe(
        environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("open_source", {"url": PAPER_URL}),
            ("register_source", _registering(node_id)),
        ),
    )
    registered = _payload(result, 1)
    source = registered["source"]
    assert isinstance(source, dict), "register_source returned no source block"
    assert registered["was_read"] is True, "RAVEL could not read the paper"
    snapshot_ref = registered["snapshot_ref"]
    assert snapshot_ref is not None, "a paper registered without a snapshot cannot be read back"
    return str(source["source_id"]), str(snapshot_ref)


async def test_a_real_papers_pages_are_readable_again_after_registration(
    live_role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    artifact_store: ArtifactStore,
    database: Database,
) -> None:
    """The item, on the file this item was written for.

    Everything about a deep read is easy on a document RAVEL produced and hard
    on a document a publisher did, so this is the one that decides whether the
    feature works. The paper is read back out of the store, by page, in a
    second session that never touched the network — and what it says is checked
    against the paper's own identifier.
    """
    source_id, snapshot_ref = await _a_registered_paper(
        live_role_environment, project, research_task.node_id
    )

    result = await probe(
        live_role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("source_metadata", {"source_ref": source_id}),
            ("read_source", {"source_ref": source_id, "pages": "1"}),
        ),
    )

    described = _payload(result, 0)
    assert described["format"] == "pdf"
    assert described["text_extractable"] is True, (
        "RAVEL found no text layer in a paper arXiv generated from LaTeX"
    )
    page_count = described["page_count"]
    assert isinstance(page_count, int) and page_count >= 5, f"page_count was {page_count}"

    read = _payload(result, 1)
    assert read["unit"] == "pages"
    (page,) = read["pages"]  # type: ignore[misc]
    text = str(page["text"])
    assert len(text) > A_PAGE_OF_PROSE, f"page one yielded {len(text)} characters"
    assert PAPER_ID in text, (
        "the identifier arXiv prints on the paper itself. A landing page, an "
        "abstract or a snippet cannot produce it, so this is the check that the "
        "bytes read are the paper's"
    )
    assert read["next"] == {"pages": f"2-{min(page_count, 6)}"}, "the next five pages"

    # And independently: the same page, extracted here from the snapshot in
    # the store. Agreeing with RAVEL about the text is only worth something if
    # it is a comparison rather than a reassembly of what RAVEL said.
    stored = PdfReader(BytesIO(artifact_store.get(snapshot_ref)))
    assert stored.pages[0].extract_text() == text

    # The row, as it is in the database, is the row the reading cites.
    digest = hashlib.sha256(artifact_store.get(snapshot_ref)).hexdigest()
    with database.read_only() as session:
        row = EvidenceSourceRepository(session, project.project_id).get(source_id=source_id)
    assert row.content_hash == f"sha256:{digest}"
    assert row.snapshot_ref == snapshot_ref


async def test_a_real_papers_body_can_be_searched_and_the_place_found(
    live_role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
) -> None:
    """Find a phrase in a real paper, then read the passage it is in.

    A Research session's actual move, on an actual paper: which page says this,
    and what does it say around it. The phrase is taken from the paper's own
    text rather than assumed, so the test cannot pass by agreeing with itself
    about a document that is not there — and it is taken from a single line of
    that text, because a search is literal and a printed line break is a
    character like any other.
    """
    source_id, _ = await _a_registered_paper(
        live_role_environment, project, research_task.node_id
    )
    environment = live_role_environment.for_project(project, AgentRole.RESEARCH)

    read = await probe(
        environment,
        calls=(("read_source", {"source_ref": source_id, "pages": "2"}),),
    )
    (page,) = _payload(read, 0)["pages"]  # type: ignore[misc]
    text = str(page["text"])
    assert len(text.split()) > 100, "page two of a paper carries more than a caption"
    line = max((stripped for stripped in map(str.strip, text.splitlines())), key=len)
    phrase = line[:40]
    assert len(phrase) > 20 and phrase in text

    searched = await probe(
        environment,
        calls=(("search_source", {"source_ref": source_id, "query": phrase}),),
    )
    found = _payload(searched, 0)
    assert found["unit"] == "pages"
    total = found["total_matches"]
    assert isinstance(total, int) and total >= 1, (
        f"{phrase!r} was read from page two and not found there"
    )
    match = found["matches"][0]  # type: ignore[index]
    assert isinstance(match, dict)
    assert match["page"] == 2
    assert phrase in str(match["context"]), "the context around a match is the text around it"

    # Where the search says the phrase is, it is: the page read again carries
    # the match's own context, which is what makes the offset a citation rather
    # than a hint.
    again = await probe(
        environment,
        calls=(("read_source", {"source_ref": source_id, "pages": str(match["page"])}),),
    )
    (same_page,) = _payload(again, 0)["pages"]  # type: ignore[misc]
    assert phrase in str(same_page["text"])


async def test_a_real_pages_text_is_not_reachable_by_character_offset(
    live_role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
) -> None:
    """The addressing boundary, on a document where it matters.

    A caller that asked a paper for character 5000 would be asking a question
    with no answer: the characters of a PDF are its pages, and a reader that
    silently read page one instead would be answering from somewhere else.
    """
    source_id, _ = await _a_registered_paper(
        live_role_environment, project, research_task.node_id
    )

    result = await probe(
        live_role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_source", {"source_ref": source_id, "start": 5000, "length": 1000}),),
    )

    call = result.calls[0]
    assert call.failed
    assert "read by page" in (call.error or "")
