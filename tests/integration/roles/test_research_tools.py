"""Phase 4 gate: the Research role, driven through its own tool server.

`docs/05` states the rule this whole surface exists to enforce as a sequence:
*search result → lead → open the original source → verify → register evidence*.
What these tests drive is whether the running tool server can be made to skip a
step. Every call goes over real stdio to a server launched the way the harness
launches one, against a real PostgreSQL, because the claims being made are about
a running process: that no other role can write to the ledger, that a reference
this process never issued is refused, and that a claim's tier is read off its
sources rather than taken from whoever wrote the claim.

The one step that needs the open Internet — opening a URL and registering what
came back — is in `tests/live_research`, where the rest of the real-Internet
suite lives. What is here is everything that is decidable without it, including
every refusal, since a refusal that only works when the network is up is not a
rule.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from tests.dsh.mcp_probe import probe
from tests.integration.conftest import Prepared, a_source
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.enums import AccessStatus, CompletionStatus, EvidenceSourceTier
from ravel.domain.evidence import Evidence, EvidenceSource
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.mcp.registry import DAG_MUTATION_TOOLS
from ravel.state.database import Database
from ravel.state.repositories.research import (
    EvidenceConflictRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
    ResearchRecordRepository,
)

#: Every role but Research. The ledger has one author, and the point of asking
#: each of the others is that the refusal is at the transport rather than in a
#: prompt: the tool is not registered, so there is no handler to reach.
NOT_RESEARCH = tuple(role for role in AgentRole if role is not AgentRole.RESEARCH)

#: What the ledger's author holds. Written out rather than derived from the
#: registry, so that a tool added to Research's roster has to be added here too
#: — which is the moment somebody asks whether Research should have it.
RESEARCH_TOOLS = frozenset(
    {
        "whoami",
        # The seat begins its own task, the way a Worker begins its run: the
        # node moves to RUNNING because the role that serves it asked for it,
        # and the DAG decides whether it may.
        "begin_research",
        "read_research_task",
        "search_sources",
        "search_web",
        "open_source",
        "register_source",
        "record_evidence",
        "record_conflict",
        "assess_evidence",
        "submit_research_record",
    }
)


def _claims(database: Database, project_id: str, node_id: str) -> list[Evidence]:
    with database.read_only() as session:
        return EvidenceRepository(session, project_id).for_task(node_id)


def _sources(database: Database, project_id: str) -> list[EvidenceSource]:
    with database.read_only() as session:
        return EvidenceSourceRepository(session, project_id).all()


def _conflicts(database: Database, project_id: str) -> list[Any]:
    with database.read_only() as session:
        return EvidenceConflictRepository(session, project_id).all()


def _records(database: Database, project_id: str, node_id: str) -> list[Any]:
    with database.read_only() as session:
        return ResearchRecordRepository(session, project_id).for_node(node_id)


# ── The roster ──────────────────────────────────────────────────────────────


async def test_research_holds_the_ledgers_entrance_and_nothing_that_plans(
    role_environment: RoleEnvironment, project: Project
) -> None:
    """Search, open, register, and say what it found — and no way to replan."""
    result = await probe(role_environment.for_project(project, AgentRole.RESEARCH))

    assert set(result.tools) == RESEARCH_TOOLS
    assert set(result.tools).isdisjoint(DAG_MUTATION_TOOLS)
    assert result.whoami is not None
    assert result.whoami["may_mutate_dag"] is False


@pytest.mark.parametrize("role", NOT_RESEARCH)
async def test_only_research_may_write_to_the_ledger(
    role: AgentRole, role_environment: RoleEnvironment, project: Project, database: Database
) -> None:
    """Four writing tools, four other roles, and nothing written by any of them.

    A ledger a Master could file a source into would be a ledger whose claims
    were checked by the role that wanted them; one a Reviewer could write to
    would be a reviewer supplying its own evidence.
    """
    calls = tuple((tool, _LEDGER_WRITES[tool]) for tool in sorted(_LEDGER_WRITES))
    result = await probe(role_environment.for_project(project, role), calls=calls)

    for call in result.calls:
        assert call.failed, f"{role.value} was allowed to call {call.tool}"
        assert "Unknown tool" in (call.error or "")

    assert _sources(database, project.project_id) == []
    with database.read_only() as session:
        assert EvidenceRepository(session, project.project_id).all() == []
        assert EvidenceConflictRepository(session, project.project_id).all() == []
        assert ResearchRecordRepository(session, project.project_id).all() == []


#: The arguments each ledger-writing tool is called with when the caller should
#: not have it at all. Never used against a Research session: what is being
#: asserted is the refusal, so the arguments only have to be well-formed.
_LEDGER_WRITES: dict[str, dict[str, Any]] = {
    "register_source": {"retrieval_ref": "ret-000000000000000000000000", "node_id": "n-1"},
    "record_evidence": {
        "node_id": "n-1",
        "statement": "Something was observed.",
        "claim_class": "HYPOTHESIS",
    },
    "record_conflict": {"evidence_refs": ["e-1", "e-2"], "description": "They disagree."},
    "submit_research_record": {"node_id": "n-1"},
}


# ── What the task is told ───────────────────────────────────────────────────


async def test_the_task_carries_the_question_the_terms_and_an_empty_ledger(
    role_environment: RoleEnvironment, project: Project, research_task: Prepared
) -> None:
    """Everything a researcher needs before searching, and nothing else.

    The question is the user's, the terms are Master's, and the ledger is what
    is already recorded — which starts empty and is the thing a second pass must
    add to rather than re-derive.
    """
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_research_task", {"node_id": research_task.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["node"]["node_type"] == "RESEARCH"
    assert call.payload["project"]["original_user_goal"].startswith("Find a dopant")
    assert call.payload["project"]["prohibited_actions"] == ["No testing on live reactors."]
    assert call.payload["terms"]["contract_id"] == research_task.contract.contract_id
    assert call.payload["terms"]["is_frozen"] is True
    terms = call.payload["terms"]
    assert terms["allowed_actions"] == list(research_task.contract.allowed_actions)
    assert terms["required_outputs"] == list(research_task.contract.required_outputs)
    assert call.payload["ledger"] == {
        "sources": [],
        "claims": [],
        "conflicts": [],
        "records": [],
    }


async def test_the_task_reports_what_has_already_been_recorded(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    research_task: Prepared,
) -> None:
    """A second pass reads the ledger rather than re-deriving what is in it."""
    source = a_source(
        database, project.project_id, url="https://example.org/paper", tier=EvidenceSourceTier.A
    )
    await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "The doped series retained its conductivity.",
                    "claim_class": "FACT",
                    "source_refs": [source.source_id],
                },
            ),
        ),
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("read_research_task", {"node_id": research_task.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    (recorded,) = call.payload["ledger"]["claims"]
    assert recorded["statement"] == "The doped series retained its conductivity."
    assert recorded["claim_class"] == "FACT"
    assert recorded["source_tier"] == "A"
    assert [entry["source_id"] for entry in call.payload["ledger"]["sources"]] == [
        source.source_id
    ]


async def test_research_is_refused_a_node_whose_work_is_not_its_own(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    prepare: Callable[..., Prepared],
) -> None:
    """Reading and writing both refuse a node a Worker owns.

    The ledger records what a research task found. A session that could record
    claims about a computation's node would be writing evidence for work nobody
    asked it to do, under a contract it does not hold.
    """
    computation = prepare(required_outputs=("conductivity.csv",))
    environment = role_environment.for_project(project, AgentRole.RESEARCH)

    result = await probe(
        environment,
        calls=(
            ("read_research_task", {"node_id": computation.node_id}),
            (
                "record_evidence",
                {
                    "node_id": computation.node_id,
                    "statement": "The catalyst degraded.",
                    "claim_class": "HYPOTHESIS",
                },
            ),
        ),
    )

    for call in result.calls:
        assert call.failed, f"{call.tool} accepted a COMPUTATION node"
        assert "COMPUTATION node" in (call.error or "")
        assert "compute-worker" in (call.error or ""), (
            "the refusal names the role whose work this is, so the agent knows "
            f"whose node it reached for; it said {call.error!r}"
        )

    with database.read_only() as session:
        assert EvidenceRepository(session, project.project_id).all() == []


# ── The ledger's only entrance ──────────────────────────────────────────────


async def test_a_reference_this_session_never_issued_is_refused(
    role_environment: RoleEnvironment, project: Project, database: Database, research_task: Prepared
) -> None:
    """A source is registered from bytes RAVEL read, not from a URL and a hash.

    There is no parameter anywhere in this surface that takes a URL, a title, a
    hash or an excerpt, so a caller with a well-formed reference it made up has
    nothing to fall back on — and the refusal says what to do instead.
    """
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            (
                "register_source",
                {"retrieval_ref": "ret-0123456789abcdef01234567", "node_id": research_task.node_id},
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert "not holding a retrieval" in (call.error or "")
    assert "open_source" in (call.error or ""), (
        "the refusal has to say how to make a reference that works, or the only "
        f"way forward is to guess; it said {call.error!r}"
    )
    assert _sources(database, project.project_id) == []


# ── What a claim's standing is derived from ─────────────────────────────────


async def test_a_claim_is_rated_by_its_sources_rather_than_by_its_author(
    role_environment: RoleEnvironment,
    project: Project,
    database: Database,
    research_task: Prepared,
) -> None:
    """The tier is read off the sources, and the strongest *read* one wins.

    There is no parameter for it. A model that could name its own tier could
    file a blog post as tier A, and every later reader — and the sufficiency
    assessment — would believe it.
    """
    strong = a_source(
        database, project.project_id, url="https://www.nature.com/articles/x",
        tier=EvidenceSourceTier.A,
    )
    weak = a_source(
        database, project.project_id, url="https://blog.example.org/x",
        tier=EvidenceSourceTier.D,
    )
    unread = a_source(
        database,
        project.project_id,
        url="https://pubs.acs.org/doi/10.1021/x",
        tier=EvidenceSourceTier.A,
        access_status=AccessStatus.PAYWALLED,
    )

    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "The doped series retained 91% of its activity.",
                    "claim_class": "FACT",
                    "source_refs": [weak.source_id, strong.source_id, unread.source_id],
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["source_tier"] == "A"
    assert call.payload["access_status"] == "OK"
    assert call.payload["retrieved_at"] is not None
    assert call.payload["content_hash"] == strong.content_hash

    (stored,) = _claims(database, project.project_id, research_task.node_id)
    assert stored.source_tier is EvidenceSourceTier.A
    assert stored.research_task_ref == research_task.node_id, (
        "a claim belongs to the task that produced it, or the next pass cannot "
        "find it"
    )


async def test_a_fact_resting_on_a_source_that_was_not_read_is_refused(
    role_environment: RoleEnvironment, project: Project, database: Database, research_task: Prepared
) -> None:
    """The claim class is what a fact is allowed to rest on.

    A paywalled paper is a real finding and can be registered; it cannot be the
    support for a fact, because RAVEL never read it. The same statement filed as
    a hypothesis is accepted, which is the difference the classes exist for.
    """
    unread = a_source(
        database,
        project.project_id,
        url="https://pubs.acs.org/doi/10.1021/x",
        tier=EvidenceSourceTier.A,
        access_status=AccessStatus.PAYWALLED,
    )
    environment = role_environment.for_project(project, AgentRole.RESEARCH)

    refused = await probe(
        environment,
        calls=(
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "The catalyst retained 91% of its activity.",
                    "claim_class": "FACT",
                    "source_refs": [unread.source_id],
                },
            ),
        ),
    )

    call = refused.calls[0]
    assert call.failed
    assert "PAYWALLED" in (call.error or "")
    assert _claims(database, project.project_id, research_task.node_id) == []

    accepted = await probe(
        environment,
        calls=(
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "The catalyst may retain 91% of its activity.",
                    "claim_class": "HYPOTHESIS",
                    "source_refs": [unread.source_id],
                },
            ),
        ),
    )
    assert not accepted.calls[0].failed, accepted.calls[0].error
    assert accepted.calls[0].payload is not None
    assert accepted.calls[0].payload["access_status"] == "PAYWALLED"


async def test_a_hypothesis_may_rest_on_nothing_yet(
    role_environment: RoleEnvironment, project: Project, database: Database, research_task: Prepared
) -> None:
    """The one claim that may be recorded without support, and it is rated as such.

    A hypothesis resting on nothing is the weakest claim the ledger can hold, so
    it is filed at the weakest tier rather than refused. Refusing it would make
    the HYPOTHESIS class unreachable, which is the class a research task uses to
    say what it now suspects.
    """
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "Vanadium may be the more stable dopant.",
                    "claim_class": "HYPOTHESIS",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["source_tier"] == "D"
    assert call.payload["source_refs"] == []


# ── Conflicts ───────────────────────────────────────────────────────────────


async def test_a_conflict_is_recorded_rather_than_resolved(
    role_environment: RoleEnvironment, project: Project, database: Database, research_task: Prepared
) -> None:
    """Two claims that disagree, preserved, with what they disagree about.

    The tool writes the disagreement and stops there. Which of the two is right
    is a scientific decision, and a researcher that resolved it here would be
    deciding what the evidence means for the plan.
    """
    environment = role_environment.for_project(project, AgentRole.RESEARCH)
    recorded = await probe(
        environment,
        calls=(
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "The catalyst retained its activity.",
                    "claim_class": "HYPOTHESIS",
                },
            ),
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "The catalyst lost most of its activity.",
                    "claim_class": "HYPOTHESIS",
                },
            ),
        ),
    )
    first, second = (call.payload for call in recorded.calls)
    assert first is not None and second is not None

    result = await probe(
        environment,
        calls=(
            (
                "record_conflict",
                {
                    "evidence_refs": [first["evidence_id"], second["evidence_id"]],
                    "description": "One reports retention, the other degradation.",
                    "condition_difference": "Measured at different load levels.",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["disagrees_about"] == [
        "The catalyst retained its activity.",
        "The catalyst lost most of its activity.",
    ]
    assert call.payload["resolution"] is None
    assert "condition difference: Measured at different load levels." in call.payload[
        "description"
    ]

    (conflict,) = _conflicts(database, project.project_id)
    assert conflict.evidence_refs == (first["evidence_id"], second["evidence_id"])


async def test_one_claim_disagreeing_with_nothing_is_not_a_conflict(
    role_environment: RoleEnvironment, project: Project, database: Database
) -> None:
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            (
                "record_conflict",
                {"evidence_refs": ["e-only"], "description": "It disagrees with itself."},
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert "at least two claims" in (call.error or "")
    assert _conflicts(database, project.project_id) == []


# ── Measuring, and submitting ───────────────────────────────────────────────


async def test_the_assessment_writes_nothing_and_says_what_would_change_it(
    role_environment: RoleEnvironment, project: Project, database: Database, research_task: Prepared
) -> None:
    """The question "is there enough yet" is asked repeatedly, and is not a record.

    Writing an assessment on every call would fill the ledger with a researcher's
    own opinion of its work, and the sufficiency that matters is the one carried
    by the record it submits.
    """
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(("assess_evidence", {"node_id": research_task.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["sufficiency"] in {"WEAK", "INSUFFICIENT"}
    assert call.payload["would_change_with"], (
        "an assessment with nothing to act on is a complaint rather than a measurement"
    )
    assert [m["consideration"] for m in call.payload["measurements"]] == [
        "independence",
        "authority",
        "directness",
        "condition match",
        "reproducibility",
        "conflict",
    ], "all six axes are measured, in the order docs/05 lists them"
    assert call.payload["sources_read"] == 0

    assert _records(database, project.project_id, research_task.node_id) == []
    assert _claims(database, project.project_id, research_task.node_id) == []


async def test_completion_is_ravels_answer_not_the_researchers(
    role_environment: RoleEnvironment, project: Project, database: Database, research_task: Prepared
) -> None:
    """A task with nothing to show comes back INCOMPLETE, with the gaps named.

    The alternative — a record that says COMPLETE because the agent said so — is
    the failure mode the completion contract exists to prevent, and INCOMPLETE
    with a clear list is the answer a well-behaved research task produces.
    """
    result = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            (
                "submit_research_record",
                {
                    "node_id": research_task.node_id,
                    "question": "Which dopant keeps conductivity above the threshold?",
                    "suggested_acceptance_criteria": ["Conductivity gain >= 15%."],
                    "recommended_followups": ["Measure at the upper dopant level."],
                    "report": "No source could be opened in this pass.",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["is_complete"] is False
    assert call.payload["completion_status"] == "INCOMPLETE"
    assert "structured research record validated" in call.payload["unmet"]
    assert call.payload["unmet"], "an INCOMPLETE record has to say what is unmet"
    assert call.payload["detail"], "and why, or the next pass is a guess"

    (record,) = _records(database, project.project_id, research_task.node_id)
    assert record.completion_status is CompletionStatus.INCOMPLETE
    assert record.node_id == research_task.node_id
    assert record.task_id == research_task.node_id
    assert record.report == "No source could be opened in this pass."
    assert set(record.unknowns) >= set(call.payload["detail"]), (
        "the gaps the contract found are part of the record's unknowns, not only "
        "of the reply to the model"
    )
