"""The ledger's entrance, driven by a real session against the real Internet.

`tests/integration/roles/test_research_tools.py` proves every rule the Research
tool server enforces that can be settled without a network: who may write, what
is refused, how a claim is rated. This module settles the one step that cannot
be — *open the original source → register it* — and it settles it the only way
that means anything: by opening a URL over the wire, registering what came
back, and then checking the row against the bytes.

The distinction is the whole point. The ledger's claim to authority is that a
row describes a retrieval that really happened. A test that handed
`register_source` a canned body would confirm that the code hashes what it is
given, which was never in doubt. What is in doubt is whether the reference a
session holds names bytes *the process actually read*, and whether the hash
written to PostgreSQL is the hash of the snapshot in the object store — so both
are checked here against a real URL, a real arXiv response, and real bytes.

Two hops of the flow are exercised end to end, and each gets a test because
each adds something the other cannot: opening and registering proves the ledger
row describes the retrieval, and recording a claim on that row proves the
claim's standing is read off the source rather than taken from its author.

Marked `live`, and skipped without a contact address, because RAVEL refuses to
fetch anonymously. Nothing here is mocked or substituted: if arXiv does not
answer, these tests fail rather than read a fixture.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

import pytest
from tests.dsh.mcp_probe import ProbeResult, ToolCall, probe
from tests.integration.conftest import Prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.enums import AccessStatus, EvidenceSourceTier
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.research import EvidenceSourceRepository
from ravel.state.store import ArtifactStore

pytestmark = pytest.mark.live

#: A source RAVEL can actually read. arXiv is open access by definition, which
#: is what this module needs: its assertions are about bytes that were
#: obtained, and a publisher that refused an anonymous client would turn every
#: one of them into a test of the refusal path instead. That path has its own
#: tests in `tests/live_research/test_live_sources.py`, on the same URL space,
#: deliberately.
READABLE_URL = "https://arxiv.org/abs/1606.00335"

#: What the tier rule says about that host. Asserted rather than derived, and
#: asserted as a literal: a test that read the expected tier out of RAVEL would
#: pass whatever RAVEL happened to decide.
EXPECTED_TIER = EvidenceSourceTier.B
EXPECTED_RULE = "preprint"


def _digest(data: bytes) -> str:
    """The hash RAVEL records for a body, written out independently.

    Not imported from RAVEL, for the reason every check like this is written
    out: a helper shared with the code under test would agree with it about a
    mistake.
    """
    return f"sha256:{hashlib.sha256(data).hexdigest()}"


def _payload(result: ProbeResult, index: int) -> dict[str, object]:
    """One call's structured payload, with the ways it can be absent ruled out.

    Raising here rather than three lines later is what makes a live failure
    legible: a run that fails because a publisher was slow says so, instead of
    failing on a subscript of `None`.
    """
    call: ToolCall = result.calls[index]
    assert not call.failed, f"{call.tool} failed: {call.error}"
    assert call.payload is not None, f"{call.tool} returned no structured payload"
    return call.payload


def _registering(
    node_id: str,
) -> Callable[[tuple[ToolCall, ...]], dict[str, object]]:
    """`register_source`'s arguments, once `open_source` has answered.

    The reference has to come off the earlier reply because the bytes it names
    live in the tool server's memory and nowhere else — which is the property
    the integration suite asserts by refusing a reference this process never
    issued. Reading it here is what makes the two calls one reading rather than
    two claims about a URL.
    """

    def arguments(outcomes: tuple[ToolCall, ...]) -> dict[str, object]:
        opened: ToolCall = outcomes[0]
        assert not opened.failed, f"open_source failed: {opened.error}"
        assert opened.payload is not None, "open_source returned nothing structured"
        return {"retrieval_ref": opened.payload["retrieval_ref"], "node_id": node_id}

    return arguments


async def test_a_source_a_session_opened_is_the_source_the_ledger_records(
    live_role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    artifact_store: ArtifactStore,
) -> None:
    """The gate. A row in the ledger describes bytes this process obtained.

    Everything else the ledger supports rests on this: an assessment of
    sufficiency counts sources, a Review checks a claim against its sources,
    and Master decides on both. If the hash in PostgreSQL were not the hash of
    the bytes that were read, none of those would be checking anything.
    """
    result = await probe(
        live_role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("open_source", {"url": READABLE_URL}),
            ("register_source", _registering(research_task.node_id)),
        ),
    )

    opened = _payload(result, 0)
    assert opened["access_status"] == AccessStatus.OK.value, opened["note"]
    assert opened["content_hash"] is not None
    assert opened["excerpt"], "a source RAVEL read has text, or nothing was read"

    registered = _payload(result, 1)
    source = registered["source"]
    assert isinstance(source, dict)
    assert registered["was_read"] is True

    # The row describes the retrieval, field for field. `final_url` rather than
    # the requested one, because a redirect is part of what happened: RAVEL
    # records where the bytes came from.
    assert source["url"] == opened["final_url"]
    assert source["content_hash"] == opened["content_hash"]
    assert source["retrieved_at"] == opened["retrieved_at"]
    assert source["access_status"] == AccessStatus.OK.value

    # The tier is RAVEL's, with the rule that produced it, so a reader who
    # disagrees with the rule can see which one to argue with.
    assert registered["tier"] == {
        "tier": EXPECTED_TIER.value,
        "rule": EXPECTED_RULE,
        "detail": "a preprint server: not yet peer reviewed",
    }

    # The snapshot is the bytes that were hashed — read back out of the object
    # store and hashed here, not compared against a value RAVEL reported.
    snapshot_ref = registered["snapshot_ref"]
    assert snapshot_ref is not None, "no snapshot was stored, so nothing is checkable"
    stored = artifact_store.get(str(snapshot_ref))
    assert _digest(stored) == source["content_hash"]

    # And the row as it is actually in the database, which is the thing every
    # later reader will see — not the object this call happened to return.
    with database.read_only() as session:
        persisted = EvidenceSourceRepository(session, project.project_id).get(
            source_id=str(source["source_id"])
        )
    assert persisted is not None, "the row was reported but not written"
    assert persisted.content_hash == source["content_hash"]
    assert persisted.snapshot_ref == snapshot_ref
    assert persisted.was_read
    assert persisted.tier is EXPECTED_TIER


async def test_a_claim_a_session_files_is_rated_by_the_source_it_read(
    live_role_environment: RoleEnvironment,
    project: Project,
    research_task: Prepared,
    database: Database,
    artifact_store: ArtifactStore,
) -> None:
    """A claim's standing is derived from its sources, over a real source.

    The integration suite proves this with rows it wrote itself, which is the
    right way to test the derivation. What it cannot prove is that the tier a
    real registration assigns is the tier a real claim inherits — that is the
    join between two modules, and it is the join the sufficiency assessment
    reads. The claim also carries the source's hash and reading time, so a
    reader can check the claim against the bytes rather than against the
    source's own account of itself.
    """
    result = await probe(
        live_role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("open_source", {"url": READABLE_URL}),
            ("register_source", _registering(research_task.node_id)),
            ("record_evidence", _claiming(research_task.node_id)),
        ),
    )

    registered = _payload(result, 1)
    source = registered["source"]
    assert isinstance(source, dict)
    source_id = str(source["source_id"])
    claim = _payload(result, 2)

    assert claim["claim_class"] == "FACT"
    assert claim["source_refs"] == [source_id]
    # A FACT may rest only on something RAVEL read, so the derivation has one
    # possible answer here — and it is the source's answer, not the claim's.
    assert claim["source_tier"] == source["tier"] == EXPECTED_TIER.value
    assert claim["access_status"] == AccessStatus.OK.value
    assert claim["content_hash"] == source["content_hash"]
    assert claim["retrieved_at"] == source["retrieved_at"]

    # The hash the claim carries is the bytes behind it. This is the end of the
    # chain a reader follows: claim → source → snapshot → bytes, each hop
    # checked rather than trusted. The first test checks the middle hop; this
    # checks that the claim is joined to it.
    snapshot_ref = registered["snapshot_ref"]
    assert snapshot_ref is not None
    assert _digest(artifact_store.get(str(snapshot_ref))) == claim["content_hash"]

    with database.read_only() as session:
        stored = EvidenceSourceRepository(session, project.project_id).get(source_id=source_id)
    assert stored is not None and stored.tier is EXPECTED_TIER


def _claiming(node_id: str) -> Callable[[tuple[ToolCall, ...]], dict[str, object]]:
    """`record_evidence`'s arguments, once a source has been registered.

    The claim cites the row the previous call wrote, read off that call's reply,
    so what is being tested is the derivation rather than a citation the test
    happened to know in advance.
    """

    def arguments(outcomes: tuple[ToolCall, ...]) -> dict[str, object]:
        registered: ToolCall = outcomes[1]
        assert not registered.failed, f"register_source failed: {registered.error}"
        assert registered.payload is not None
        source = registered.payload["source"]
        assert isinstance(source, dict)
        return {
            "node_id": node_id,
            "statement": "The deposited series was measured against a baseline.",
            "claim_class": "FACT",
            "source_refs": [source["source_id"]],
        }

    return arguments
