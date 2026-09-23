"""P11-03: the research Master planned reaches Master, as the record and not as a memory.

The architecture has always drawn the chain

```text
Master → Research Agent → Evidence / ResearchRecord → Master → Decision
```

and until this item the last arrow had no tool behind it. Master could read the
whole project state and could not read one research task's result, so what a
task found reached the next decision only if the session that received it was
still the session making the decision — which, across a crash, a rebuild or a
supervisor restart, it is not.

The cases here drive the chain the way a project drives it: the Research seat's
own server begins the task and hands the record over, the Review seat's server
judges it, and the Master's server reads it back. Three processes, three
rosters, and PostgreSQL between them. What is asserted about Master's answer is
compared against the rows, because a readback that returned a plausible summary
of something else would satisfy every assertion about shape.

Offline by construction: sources are registered through the repository the
gateway writes through, so nothing here needs the network. The live end-to-end —
a real Master planning research, a real seat searching, a real verdict, and a
downstream computation planned from the result — is P11-11's certification.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.dsh.mcp_probe import ToolCall, probe
from tests.integration.conftest import Prepared, a_source
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    Confidence,
    EvidenceSourceTier,
)
from ravel.domain.evidence import Evidence
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.dsh.agents import HarnessAgent
from ravel.execution.loop import read_situation
from ravel.state.database import Database
from ravel.state.repositories.research import (
    EvidenceRepository,
    EvidenceSourceRepository,
)

pytestmark = [pytest.mark.phase11, pytest.mark.timeout(900)]

#: The tools this item adds, written out rather than derived from the registry:
#: a case that asked the registry which tools to expect would pass if one of
#: them were handed to a second role.
READBACK_TOOLS = frozenset(
    {
        "list_research_results",
        "read_research_result",
        "read_evidence",
        "read_source_metadata",
    }
)

#: What the seat recorded, in its own words, read back field by field.
CLAIM = "Niobium doping at 3 mol% raised conductivity by 18% up to 480 hours."
SOURCE_URL = "https://example.org/articles/niobium-stability"
QUESTION = "Which dopant keeps conductivity above the threshold?"
UNKNOWN = "Nothing above 500 hours."
DIAGNOSIS = "One source is not enough to carry the claim."


def payload(call: ToolCall) -> dict[str, Any]:
    """One call's structured result, with the ways it can be absent ruled out."""
    assert not call.failed, f"{call.tool} failed: {call.error}"
    assert call.payload is not None, f"{call.tool} returned no structured payload"
    return call.payload


def a_ledger(database: Database, project: Project, task: Prepared) -> str:
    """One source and one claim resting on it, written the way Research writes.

    Through the repositories the seat's own tools write through, because a
    fixture that inserted rows directly would let this suite assert against a
    ledger no session could have produced.

    Returns:
        The source's id, so a case can read its metadata back.
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


async def handed_over(
    role_environment: RoleEnvironment, project: Project, task: Prepared
) -> dict[str, Any]:
    """The record the Research seat's own tools write, taken from their answer.

    Two calls, because the hand-over is the act this item turns on and only a
    task the seat has begun can make it: a READY node that submitted would have
    a record and no hand-over, which is a state the tools produce and not one
    the rest of the project treats as a result.
    """
    submitted = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("begin_research", {"node_id": task.node_id}),
            (
                "submit_research_record",
                {
                    "node_id": task.node_id,
                    "question": QUESTION,
                    "recommended_followups": ["Measure the 5 mol% series."],
                    "unknowns": [UNKNOWN],
                    "report": "One source read; one claim recorded.",
                },
            ),
        ),
    )
    assert payload(submitted.calls[0])["node_status"] == "RUNNING", (
        "the seat could not begin its own task"
    )
    answer = payload(submitted.calls[1])
    assert answer["handed_over"] is True, (
        f"the seat's submission did not hand the task over: {answer}"
    )
    record = answer["record"]
    assert isinstance(record, dict), "the seat submitted no record"
    return record


async def test_p11_03_master_reads_the_result_a_research_seat_handed_over(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    research_task: Prepared,
) -> None:
    """The whole chain in one case, across the three servers that make it up.

    The record is produced by the *seat's own tool*, the verdict by Review's
    own tool, and the reading by Master's. Between the three there is nothing
    but PostgreSQL, which is the point: what Master reads is what was recorded,
    not what any session believed about an earlier turn.
    """
    source_id = a_ledger(database, project, research_task)
    record = await handed_over(role_environment, project, research_task)

    reviewed = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(
            ("read_review_package", {"node_id": research_task.node_id}),
            (
                "submit_review",
                {
                    "node_id": research_task.node_id,
                    "checkpoint": "FINAL",
                    "outcome": "PARTIAL",
                    "diagnosis": DIAGNOSIS,
                },
            ),
        ),
    )
    package = payload(reviewed.calls[0])
    assert [claim["statement"] for claim in package["research"]["claims"]] == [CLAIM], (
        "Review is judging a node without the ledger the record was assembled from"
    )
    assert payload(reviewed.calls[1])["moved_to"] == "PARTIAL"

    read = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            ("read_project_state", {}),
            ("read_research_result", {"node_id": research_task.node_id}),
            ("read_evidence", {"evidence_id": str(record["evidence_refs"][0])}),
            ("read_source_metadata", {"source_id": source_id}),
        ),
    )

    # The turn Master gets next says a result is there, which is what makes the
    # reading reachable at all: a Master with no memory of the turn it was
    # produced in learns that it exists from the state it reads first.
    state = payload(read.calls[0])
    assert [entry["display_id"] for entry in state["completed_research"]] == [
        research_task.node.display_id
    ]
    entry = state["completed_research"][0]
    assert entry["node_id"] == research_task.node_id
    assert entry["review_outcome"] == "PARTIAL"
    assert entry["research_record_ref"], "the state names no record to read"
    assert entry["claims"] == 1
    assert CLAIM not in repr(state), (
        "the claim itself reached the project-state read, which is the read every "
        "turn takes"
    )

    # And the result itself, with its parts.
    result = payload(read.calls[1])
    assert result["node"]["objective"] == research_task.node.objective
    assert result["node"]["status"] == "PARTIAL"
    assert result["has_result"] is True
    assert result["result"]["question"] == QUESTION
    assert result["result"]["facts"] == [CLAIM]
    assert result["result"]["completion_status"], "the record came back unjudged"
    assert [claim["statement"] for claim in result["claims"]] == [CLAIM]
    assert [source["url"] for source in result["sources"]] == [SOURCE_URL]
    assert result["records_submitted"] == 1
    assert result["sufficiency"]["sufficiency"], "the ledger was assessed as nothing"
    assert result["reviews"][0]["outcome"] == "PARTIAL"
    assert result["reviews"][0]["diagnosis"] == DIAGNOSIS

    claim = payload(read.calls[2])
    assert claim["claim"]["statement"] == CLAIM
    assert [source["source_id"] for source in claim["sources"]] == [source_id]

    # What is read is the row, not a recollection of it: the same fields, read
    # straight out of PostgreSQL.
    with database.read_only() as session:
        rows = EvidenceRepository(session, project.project_id).for_task(
            research_task.node_id
        )
        source_row = EvidenceSourceRepository(session, project.project_id).get(
            source_id=source_id
        )
    assert [row.evidence_id for row in rows] == [
        claim["claim"]["evidence_id"]
    ], "Master read a claim no row holds"
    assert source_row.url == payload(read.calls[3])["source"]["url"]
    assert payload(read.calls[3])["was_read"] is True


async def test_p11_03_a_task_that_has_not_handed_over_is_not_a_result_yet(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    research_task: Prepared,
) -> None:
    """The filter that keeps the state read from listing work in progress.

    A research task under way has evidence and no result: what it has found is
    readable if Master asks for it, and what it has not done is hand anything
    over. Both halves matter — a state read that named every research node
    would grow with the project and tell Master nothing the DAG summary does
    not, and a `read_research_result` that refused a task in flight would hide
    the partial evidence a decision to keep going or to stop turns on.
    """
    a_ledger(database, project, research_task)
    begun = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("begin_research", {"node_id": research_task.node_id}),),
    )
    assert payload(begun.calls[0])["node_status"] == "RUNNING"

    read = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            ("read_project_state", {}),
            ("list_research_results", {}),
            ("read_research_result", {"node_id": research_task.node_id}),
        ),
    )

    state = payload(read.calls[0])
    assert state["completed_research"] == [], (
        "a research task still being worked on is reported as a result"
    )

    listed = payload(read.calls[1])
    assert listed["with_a_record"] == 0
    assert [entry["node_status"] for entry in listed["results"]] == ["RUNNING"]
    assert listed["results"][0]["handed_over"] is False
    assert listed["results"][0]["research_record_ref"] is None
    assert listed["results"][0]["claims"] == 1
    assert listed["results"][0]["review_outcome"] is None

    underway = payload(read.calls[2])
    assert underway["has_result"] is False
    assert underway["result"] is None
    assert [claim["statement"] for claim in underway["claims"]] == [CLAIM], (
        "the evidence a task in flight has already recorded is unreadable"
    )
    assert underway["sufficiency"]["sufficiency"], (
        "a task with no record was reported as having no evidence"
    )


async def test_p11_03_the_masters_turn_points_at_the_result_it_can_read(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    research_task: Prepared,
) -> None:
    """A read that exists and is never pointed at is a read the seat does not have.

    The same rule P10's prompt cases are written under. Master's turn is
    assembled from the situation, so a result that arrived while nobody was
    looking has to be named in the turn that follows it — otherwise the tool is
    reachable only by a session that already remembered to reach for it, which
    is exactly the memory this item exists to stop depending on.
    """
    a_ledger(database, project, research_task)
    await handed_over(role_environment, project, research_task)

    situation = read_situation(database, project.project_id)
    prompt = HarnessAgent(
        pool=None,  # type: ignore[arg-type]
        project_id=project.project_id,
        role=AgentRole.MASTER,
    )._master_prompt(situation)

    assert research_task.node.display_id in prompt, (
        "Master's turn does not name the research task whose result is waiting"
    )
    assert "read_research_result" in prompt, (
        "Master is told a result exists without being told what reads it, so the "
        f"record is as unreachable as it was before the item: {prompt}"
    )
    assert CLAIM not in prompt, (
        "the claim itself is in the turn's prompt; the prompt points at the record, "
        "and the record is read through the tool"
    )


async def test_p11_03_the_readback_belongs_to_master_and_to_nobody_else(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """Over the transport, for every role: who is served these four, and how.

    Read-only is not a property of the handlers but of the roster, and the one
    that matters most is the second half: Master holds the ledger's readers and
    none of its writers. A Master that could register a claim would be the role
    that judges the evidence supplying it, which is the separation the five
    seats exist to keep.
    """
    seen: dict[AgentRole, frozenset[str]] = {}
    for role in AgentRole:
        served = await probe(role_environment.for_project(project, role))
        seen[role] = frozenset(served.tools)

    assert seen[AgentRole.MASTER] >= READBACK_TOOLS, (
        "a Master session is not served the tools this item adds: "
        f"{sorted(READBACK_TOOLS - seen[AgentRole.MASTER])}"
    )
    for role, tools in seen.items():
        if role is AgentRole.MASTER:
            continue
        assert tools.isdisjoint(READBACK_TOOLS), (
            f"{role.value} is served {sorted(tools & READBACK_TOOLS)}; a seat that "
            "can read a project's ledger is a seat judging beyond its own node"
        )

    # And Master's own answer about itself: it may not mutate the DAG, and the
    # tools it holds that write are the plan's, not the ledger's.
    master = await probe(role_environment.for_project(project, AgentRole.MASTER))
    assert master.whoami is not None
    judged_writers = {
        "begin_research",
        "register_source",
        "record_evidence",
        "record_conflict",
        "submit_research_record",
        "open_source",
        "search_sources",
        "search_web",
        "submit_review",
    }
    assert seen[AgentRole.MASTER].isdisjoint(judged_writers), (
        "Master is served a tool that writes what it judges — a claim, a source, "
        "a research record, or a verdict about a seat's work: "
        f"{sorted(seen[AgentRole.MASTER] & judged_writers)}"
    )
