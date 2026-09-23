"""P11-03: what Master can read back out of a research task, through its server.

The design has always drawn the chain `Master → Research → Evidence → Master →
Decision`, and the last arrow had no tool behind it. What is asserted here is
that it has one now, and that the tool reads the ledger rather than anything the
Master session was told: the record is produced by the Research *seat*, in a
different process, and every field Master reads is read back out of PostgreSQL.

The two directions are asserted together, because a read surface that came with
a writer would be worse than none. Master may read the Ledger and may not write
to it; a claim Master wants that Research did not record is a research task
rather than a line to add, and the roster is what makes that structural rather
than a matter of the prompt.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tests.dsh.mcp_probe import ToolCall, probe
from tests.integration.conftest import Prepared, a_source
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    Confidence,
    EvidenceSourceTier,
    NodeType,
    ReviewCheckpoint,
    ReviewOutcome,
)
from ravel.domain.evidence import Evidence
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.mcp.registry import WRITE_TOOLS, tools_for
from ravel.review.service import ReviewService
from ravel.state.database import Database
from ravel.state.repositories.research import (
    EvidenceRepository,
    EvidenceSourceRepository,
)

#: The tools this item adds. Written out rather than derived, so that one of
#: them being handed to a second role has to be noticed here.
READBACK_TOOLS = frozenset(
    {
        "list_research_results",
        "read_research_result",
        "read_evidence",
        "read_source_metadata",
    }
)

#: What the seat recorded, in its own words. Read back by Master field by field.
CLAIM = "Niobium doping at 3 mol% raised conductivity by 18% up to 480 hours."
SOURCE_URL = "https://example.org/articles/niobium-stability"


def payload(call: ToolCall) -> dict[str, Any]:
    """One call's structured result, with the ways it can be absent ruled out."""
    assert not call.failed, f"{call.tool} failed: {call.error}"
    assert call.payload is not None, f"{call.tool} returned no structured payload"
    return call.payload


def a_ledger(database: Database, project: Project, task: Prepared) -> str:
    """One source and one claim resting on it, written the way Research writes.

    Through the repositories the seat's own tools write through, because a
    fixture that inserted rows directly would let a readback test assert against
    a ledger no session could have produced. The claim's tier is taken from the
    source rather than asserted here, which is the rule the seat runs under too.

    Returns:
        The source's id, so a test can read its metadata back.
    """
    source = a_source(
        database,
        project.project_id,
        url=SOURCE_URL,
        tier=EvidenceSourceTier.B,
        access_status=AccessStatus.OK,
    )
    with database.transaction() as session:
        EvidenceRepository(session, project.project_id).register(
            Evidence(
                project_id=project.project_id,
                statement=CLAIM,
                claim_class=ClaimClass.FACT,
                source_tier=source.tier or EvidenceSourceTier.B,
                source_refs=(source.source_id,),
                confidence=Confidence.HIGH,
                research_task_ref=task.node_id,
            ),
            actor_id=AgentRole.RESEARCH.value,
        )
    return source.source_id


async def a_submitted_record(
    role_environment: RoleEnvironment, project: Project, task: Prepared
) -> dict[str, Any]:
    """The record the seat's own tools write, taken from their answer.

    Two calls, because the hand-over is what makes a result exist and it only
    happens from a task the seat has begun: a READY node that submitted would
    have a record and no hand-over, which is a state the tools can produce and
    not one this readback is about. Going through `begin_research` is also the
    only path that moves a node into RUNNING, so this is the sequence a real
    seat runs rather than a shortcut around it.
    """
    submitted = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("begin_research", {"node_id": task.node_id}),
            (
                "submit_research_record",
                {
                    "node_id": task.node_id,
                    "question": "Which dopant keeps conductivity above the threshold?",
                    "recommended_followups": ["Measure the 5 mol% series."],
                    "unknowns": ["Nothing above 500 hours."],
                    "report": "One source read; one claim recorded.",
                },
            ),
        ),
    )
    handed = payload(submitted.calls[1])
    assert handed["handed_over"] is True, (
        f"the seat's submission did not hand the task over: {handed}"
    )
    record = handed["record"]
    assert isinstance(record, dict), "the seat submitted no record"
    return record


async def test_master_reads_a_research_result_its_seat_submitted(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    research_task: Prepared,
) -> None:
    """The whole item in one case: a record written by Research, read by Master.

    The record is produced by the *tool the seat calls*, in the seat's own
    server process, and then read through Master's server. Between the two there
    is nothing but PostgreSQL, which is the point: what Master reads is what was
    recorded, not what either session believed.
    """
    source_id = a_ledger(database, project, research_task)
    record = await a_submitted_record(role_environment, project, research_task)
    assert CLAIM in record["facts"], f"the seat's record is not about the claim: {record}"

    read = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            ("read_research_result", {"node_id": research_task.node_id}),
            ("read_evidence", {"evidence_id": str(record["evidence_refs"][0])}),
            ("read_source_metadata", {"source_id": source_id}),
        ),
    )

    result = payload(read.calls[0])
    assert result["node"]["display_id"] == research_task.node.display_id
    assert result["node"]["objective"] == research_task.node.objective
    # Handed over to the final checkpoint by the submission itself, so the turn
    # Master takes next is one where a verdict is waiting rather than one where
    # the task is still running.
    assert result["node"]["status"] == "REVIEWING"
    assert result["has_result"] is True
    assert result["result"]["question"] == (
        "Which dopant keeps conductivity above the threshold?"
    )
    assert result["result"]["unknowns"] == ["Nothing above 500 hours."]
    assert [claim["statement"] for claim in result["claims"]] == [CLAIM]
    assert [source["url"] for source in result["sources"]] == [SOURCE_URL]
    assert result["sufficiency"]["sufficiency"], "the ledger was assessed as nothing"

    claim = payload(read.calls[1])
    assert claim["claim"]["statement"] == CLAIM
    assert [source["source_id"] for source in claim["sources"]] == [source_id]

    metadata = payload(read.calls[2])
    assert metadata["source"]["url"] == SOURCE_URL
    assert metadata["has_snapshot"] is False
    assert metadata["cited_by"] == [claim["claim"]["evidence_id"]]
    assert "text" not in metadata, (
        "the source's own words are the Research seat's to read and quote; a "
        "Master-facing metadata read that returned them would be a second "
        "reader of the document rather than a reader of the ledger"
    )


async def test_the_listing_names_which_tasks_have_something_to_read(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    research_task: Prepared,
) -> None:
    """Master's first question is which results exist, not what they say."""
    a_ledger(database, project, research_task)
    await a_submitted_record(role_environment, project, research_task)

    listed = payload(
        (
            await probe(
                role_environment.for_project(project, AgentRole.MASTER),
                calls=(("list_research_results", {}),),
            )
        ).calls[0]
    )

    assert listed["with_a_record"] == 1
    entry = listed["results"][0]
    assert entry["display_id"] == research_task.node.display_id
    assert entry["node_status"] == "REVIEWING"
    assert entry["handed_over"] is True
    assert entry["claims"] == 1
    assert entry["sources"] == 1
    assert entry["research_record_ref"], "the listing names no record to read"
    assert entry["completion_status"], "the record came back unjudged"
    assert entry["sufficiency"], "the listing omits how strong the evidence came back"
    # A summary, not the contents: the claim itself is a second call, and the
    # listing is what decides which task is worth making it about.
    assert CLAIM not in repr(listed)


async def test_a_non_research_node_has_no_result_to_read(
    role_environment: RoleEnvironment,
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """The refusal a Master that guessed wrong needs, in the words it needs."""
    computation = prepare(node_type=NodeType.COMPUTATION)

    read = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(("read_research_result", {"node_id": computation.node_id}),),
    )

    call = read.calls[0]
    assert call.failed, f"a computation node returned a research result: {call.payload}"
    error = call.error or ""
    assert computation.node.display_id in error
    assert "COMPUTATION" in error
    assert "list_research_results" in error, (
        "the refusal does not say where the nodes that do have a result are "
        f"named: {error}"
    )


def test_the_readback_tools_are_masters_and_write_nothing() -> None:
    """The roster, from the table, for the property the item is about.

    Read-only is not a detail of the handlers: Master judging the evidence
    Research supplied is the separation of powers, and a read surface that
    acquired a writer would be the judge of a record filing it. The other half —
    that Master holds no ledger-writing tool — is `test_tool_roster.py`'s
    authorship check, and is asserted against the running servers by
    `tests/integration/roles/test_research_tools.py`.
    """
    for tool in READBACK_TOOLS:
        assert tool in tools_for(AgentRole.MASTER), f"{tool} is not Master's"
        assert tool not in WRITE_TOOLS, f"{tool} writes, and it is a readback tool"

    for role in AgentRole:
        if role is AgentRole.MASTER:
            continue
        assert READBACK_TOOLS.isdisjoint(tools_for(role)), (
            f"{role.value} can read the ledger back; a Worker that could hold the "
            "evidence of its own node would be judging its own work"
        )


async def test_the_project_state_says_a_result_is_available(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    prepare: Callable[..., Prepared],
    research_task: Prepared,
) -> None:
    """The turn Master gets next has to say there is something to read.

    A tool nobody calls is not a chain. What makes the readback reachable is
    that `read_project_state` reports which research tasks have handed a result
    over and what Review made of it, so a Master with no memory of the turn the
    record was written in learns that it exists from the state it reads first.

    The task is taken the whole way — begun, submitted, judged — because that
    is the state a result is actually in when Master reads it, and through the
    service that applies verdicts rather than the repository that writes them,
    so the node moves the way production moves it.
    """
    a_ledger(database, project, research_task)
    await a_submitted_record(role_environment, project, research_task)
    with database.transaction() as session:
        ReviewService(session, project.project_id).submit(
            ReviewRecord(
                project_id=project.project_id,
                node_id=research_task.node_id,
                checkpoint=ReviewCheckpoint.FINAL,
                frozen_criteria_ref=research_task.contract.contract_id,
                frozen_criteria_version=research_task.contract.version,
                outcome=ReviewOutcome.PARTIAL,
                diagnosis="One source is not enough to carry the claim.",
            ),
            role=AgentRole.REVIEW,
        )
    # A second research task, nothing done on it. It is the case the state read
    # has to keep out: "there is a research node" is already in the DAG summary,
    # and a listing that named every research node would tell Master nothing it
    # could not read there while growing with the project.
    untouched = prepare(node_type=NodeType.RESEARCH, with_acceptance=False)

    read = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            ("read_project_state", {}),
            ("read_research_result", {"node_id": research_task.node_id}),
        ),
    )

    state = payload(read.calls[0])
    completed = state["completed_research"]
    assert [entry["display_id"] for entry in completed] == [
        research_task.node.display_id
    ], (
        "the project state does not say that a research result is available, so a "
        "Master with no memory of the turn it was produced in has no way to learn "
        f"that it is: {completed}"
    )
    entry = completed[0]
    assert entry["node_status"] == "PARTIAL"
    assert entry["handed_over"] is True
    assert entry["review_outcome"] == "PARTIAL"
    assert entry["review_diagnosis"] == "One source is not enough to carry the claim."
    assert entry["claims"] == 1
    assert entry["research_record_ref"], "the hand-over left no record to read"
    assert entry["completion_status"], "the record came back unjudged"
    # The untouched task is in the state read, and it ought to be: the DAG
    # summary names it as a node ready to run, which is what it is. What it must
    # not have is a *result*. The distinction this item turns on is between a
    # research node and one that has handed something over, so both halves are
    # asserted — otherwise "it is absent from `completed_research`" would pass
    # just as well against a node the read never mentioned.
    assert untouched.node.display_id in state["dag"]["ready_to_run"], (
        "the second research task is not named by the state read at all, so this "
        "case is not testing the filter it means to"
    )
    assert untouched.node.display_id not in repr(completed), (
        "a research task with nothing recorded on it is reported as a result, "
        "which tells Master there is something to read where there is nothing"
    )
    assert CLAIM not in repr(state), (
        "the claim itself reached the project-state read, which is the read every "
        "turn takes"
    )

    result = payload(read.calls[1])
    assert result["has_result"] is True
    assert result["reviews"][0]["outcome"] == "PARTIAL"
    assert result["reviews"][0]["diagnosis"] == (
        "One source is not enough to carry the claim."
    )


def test_the_ledger_holds_what_the_readback_reports(
    database: Database, project: Project, research_task: Prepared
) -> None:
    """Read from the database rather than from a tool, for the same rows.

    A readback that returned the right thing because a fixture put it in the
    right place would be a readback nobody had tested; this reads the rows back
    out of PostgreSQL and compares them field by field.
    """
    source_id = a_ledger(database, project, research_task)

    with database.read_only() as session:
        claims = EvidenceRepository(session, project.project_id).for_task(
            research_task.node_id
        )
        source = EvidenceSourceRepository(session, project.project_id).get(
            source_id=source_id
        )

    assert [claim.statement for claim in claims] == [CLAIM]
    assert source.url == SOURCE_URL
    assert source.tier is EvidenceSourceTier.B
