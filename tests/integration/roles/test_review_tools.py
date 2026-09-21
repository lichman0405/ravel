"""Phase 7 gate: the Review role, driven through its own tool server.

Review is the role that ends a node, so what these tests drive is the whole of
its surface: what it is told is waiting, what it is given to judge, and what
happens to the project when it answers. Every call goes over real stdio to a
server launched the way the harness launches one, against a real PostgreSQL,
because the claims being made are about a running process — that a verdict
opens the gate a node was blocked at, that a verdict that skips a criterion is
refused, and that none of it changes the plan.

The node states are set up the way the runtime reaches them — through the
repositories production uses — rather than by writing rows, so a verdict here
is a verdict about a state the system can actually be in.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.dsh.mcp_probe import probe
from tests.integration.conftest import Prepared, a_source
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.dag import DagNode
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import EvidenceSourceTier, NodeStatus, ReviewCheckpoint
from ravel.domain.roles import AgentRole
from ravel.mcp.registry import DAG_MUTATION_TOOLS
from ravel.state.database import Database
from ravel.state.repositories.contracts import AcceptanceContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import DecisionRepository, ReviewRepository

#: Every role but Review. A verdict is Review's act, and the point of asking
#: each of the others is that the refusal is at the transport rather than in a
#: prompt: the tool is not registered, so there is no handler to reach.
NOT_REVIEW = tuple(role for role in AgentRole if role is not AgentRole.REVIEW)


def _move(
    database: Database, project_id: str, node_id: str, status: NodeStatus, *, actor: str
) -> DagNode:
    """Move a node the way the runtime moves one.

    Through the repository, so the gate that refuses an uncleared node to run
    is the one being exercised rather than stepped around.
    """
    with database.transaction() as session:
        return DagRepository(session, project_id).transition_node(
            node_id, status, actor_id=actor
        )


def _verdicts(
    database: Database, project_id: str, node_id: str, checkpoint: ReviewCheckpoint
) -> list[ReviewRecord]:
    """The verdicts already recorded about a node at one checkpoint.

    Filtered by checkpoint because a COMPUTATION node arrives here already
    carrying its pre-flight review: `prepare` clears it to run the way the
    runtime does, and a test that counted every review of the node would be
    counting that one.
    """
    with database.read_only() as session:
        return [
            review
            for review in ReviewRepository(session, project_id).for_node(node_id)
            if review.checkpoint is checkpoint
        ]


def _to_reviewing(database: Database, project_id: str, node_id: str) -> None:
    """Drive a cleared node to the point where its result is judged.

    Two moves rather than one, because a node arrives at REVIEWING by having
    run: there is no edge that skips RUNNING, and a test that wrote the status
    directly would be judging a state the runtime never produces.
    """
    _move(database, project_id, node_id, NodeStatus.RUNNING, actor="compute-worker")
    _move(database, project_id, node_id, NodeStatus.REVIEWING, actor="compute-worker")


# ── The roster ──────────────────────────────────────────────────────────────


async def test_review_holds_exactly_the_tools_a_verdict_needs(
    role_environment: RoleEnvironment, project: Any
) -> None:
    """Two ways to read, one way to answer, and no way to change the plan."""
    result = await probe(role_environment.for_project(project, AgentRole.REVIEW))

    assert set(result.tools) == {
        "whoami",
        "read_review_work",
        "read_review_package",
        "submit_review",
    }
    assert set(result.tools).isdisjoint(DAG_MUTATION_TOOLS)
    assert result.whoami is not None
    assert result.whoami["may_mutate_dag"] is False


@pytest.mark.parametrize("role", NOT_REVIEW)
async def test_only_review_may_submit_a_verdict(
    role: AgentRole, role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """Refused at the transport, and nothing is written before the refusal."""
    result = await probe(
        role_environment.for_project(project, role),
        calls=(
            (
                "submit_review",
                {
                    "node_id": "node-does-not-matter",
                    "checkpoint": "FINAL",
                    "outcome": "PASS",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed, f"{role.value} was allowed to submit a verdict: {call.payload}"
    assert "Unknown tool" in (call.error or "")

    with database.read_only() as session:
        assert ReviewRepository(session, project.project_id).all() == []


# ── What Review is told to look at ──────────────────────────────────────────


async def test_the_queue_says_what_the_project_is_waiting_for(
    role_environment: RoleEnvironment, project: Any, prepare: Any
) -> None:
    """A node that cannot start until it is cleared is work, not background.

    The distinction the queue draws is the one that decides whether a reviewer
    is needed at all: a node held at the pre-flight checkpoint is holding the
    project up, and a node that has already been cleared is not.
    """
    waiting = prepare(cleared=False)
    cleared = prepare()
    environment = role_environment.for_project(project, AgentRole.REVIEW)

    result = await probe(environment, calls=(("read_review_work", {}),))

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    held_up = {entry["node_id"]: entry for entry in call.payload["held_up"]}
    assert waiting.node_id in held_up
    assert cleared.node_id not in held_up, (
        "a node already cleared to run is not work a verdict is waiting on"
    )

    entry = held_up[waiting.node_id]
    assert entry["checkpoint"] == "PRE_RUN"
    assert entry["holds_up_the_project"] is True
    assert entry["status"] == "READY"
    assert entry["definition_of_done"]["kind"] == "acceptance_contract"
    assert entry["definition_of_done"]["criteria"] == 1
    assert entry["last_review_at_this_checkpoint"] is None
    assert entry["unreviewable"] is None


async def test_the_package_carries_the_criteria_a_verdict_must_answer(
    role_environment: RoleEnvironment, project: Any, prepare: Any
) -> None:
    """The identifiers a verdict names come from here and nowhere else."""
    prepared = prepare(cleared=False)
    result = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(("read_review_package", {"node_id": prepared.node_id}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["node"]["node_id"] == prepared.node_id
    assert call.payload["checkpoints"] == ["PRE_RUN"]
    assert call.payload["checkpoints_owed"] == ["PRE_RUN", "FINAL"]
    assert call.payload["unreviewable"] is None

    definition = call.payload["definition_of_done"]
    assert definition["kind"] == "acceptance_contract"
    assert definition["contract_id"] == prepared.acceptance.contract_id
    assert definition["version"] == prepared.acceptance.version
    assert [criterion["criterion_id"] for criterion in definition["criteria"]] == [
        criterion.criterion_id for criterion in prepared.acceptance.criteria
    ]
    assert [criterion["statement"] for criterion in definition["criteria"]] == [
        criterion.statement for criterion in prepared.acceptance.criteria
    ]
    assert call.payload["execution"] is None
    assert call.payload["artifacts"] == []
    assert call.payload["reviews"] == []
    assert call.payload["research"] == {
        "records": [],
        "claims": [],
        "sources": [],
        "conflicts": [],
    }, "a node that ran was given a research delivery to judge"


async def test_a_research_node_is_judged_on_the_record_it_handed_over(
    role_environment: RoleEnvironment,
    project: Any,
    database: Database,
    research_task: Prepared,
) -> None:
    """P10-03: what a research task delivered is in the package that judges it.

    A task whose work is reading owes no Execution Record and produces no
    artifacts, so four of the package's keys are empty or `null` for one — and
    the row that says what it *did* deliver was, until this key existed, in none
    of them. A live Review seat judged a completed task from exactly those four
    empty fields and wrote "the node was handed over with a null Execution
    Record, an empty artifact list, no reviews, and no deviations"; it was
    reading its package correctly. Review holds three tools and this is the only
    per-node view, so a delivery missing here is a delivery no verdict can ever
    see.

    The delivery is produced by the seat that owns it, over stdio, and read back
    by the seat that judges it, over stdio. Nothing in between is a fixture.
    """
    source = a_source(
        database,
        project.project_id,
        url="https://example.org/niobium",
        tier=EvidenceSourceTier.A,
    )
    research = await probe(
        role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("begin_research", {"node_id": research_task.node_id}),
            (
                "record_evidence",
                {
                    "node_id": research_task.node_id,
                    "statement": "Niobium doping held conductivity above the threshold.",
                    "claim_class": "FACT",
                    "source_refs": [source.source_id],
                },
            ),
            (
                "submit_research_record",
                {
                    "node_id": research_task.node_id,
                    "question": research_task.contract.objective,
                    "recommended_followups": ["Measure the series at 500 hours."],
                    "report": "The series held.",
                },
            ),
        ),
    )
    handed_over = research.calls[-1]
    assert not handed_over.failed, handed_over.error
    assert handed_over.payload is not None
    assert handed_over.payload["handed_over"] is True

    judged = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(("read_review_package", {"node_id": research_task.node_id}),),
    )

    call = judged.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["checkpoints"] == ["FINAL"]

    # The two keys a reviewer would otherwise read as "nothing was delivered".
    assert call.payload["execution"] is None
    assert call.payload["artifacts"] == []

    delivery = call.payload["research"]
    assert [record["research_id"] for record in delivery["records"]] == [
        handed_over.payload["record"]["research_id"]
    ], "the record the seat handed over is not the record the judge was given"
    assert delivery["records"][0]["completion_status"] == (
        handed_over.payload["completion_status"]
    ), "the judge was given a different completeness answer than RAVEL computed"
    assert [claim["statement"] for claim in delivery["claims"]] == [
        "Niobium doping held conductivity above the threshold."
    ]
    assert [entry["source_id"] for entry in delivery["sources"]] == [source.source_id]
    assert delivery["sources"][0]["tier"] == EvidenceSourceTier.A.value, (
        "a criterion about provenance cannot be answered without the tier"
    )


# ── What a verdict does ─────────────────────────────────────────────────────


async def test_a_pre_flight_verdict_is_what_lets_the_node_run(
    role_environment: RoleEnvironment, project: Any, database: Any, prepare: Any
) -> None:
    """The gate is the point: without the verdict the transition is refused.

    Asserted in both directions, because a PASS that merely recorded itself
    would leave the node exactly as stuck as it was — and the record would look
    identical either way.
    """
    prepared = prepare(cleared=False)
    environment = role_environment.for_project(project, AgentRole.REVIEW)
    criterion_id = prepared.acceptance.criteria[0].criterion_id

    result = await probe(
        environment,
        calls=(
            (
                "submit_review",
                {
                    "node_id": prepared.node_id,
                    "checkpoint": "PRE_RUN",
                    "outcome": "PASS",
                    "criterion_results": [
                        {"criterion_id": criterion_id, "satisfied": True}
                    ],
                    "diagnosis": "The criterion names a threshold and a series to measure.",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["status_before"] == "READY"
    assert call.payload["moved_to"] is None, (
        "a pre-flight PASS clears the gate; it does not move the node"
    )

    running = _move(
        database, project.project_id, prepared.node_id, NodeStatus.RUNNING, actor="compute-worker"
    )
    assert running.status is NodeStatus.RUNNING, (
        "the verdict was recorded and the node still could not run"
    )


async def test_a_final_verdict_ends_the_node_against_the_frozen_criteria(
    role_environment: RoleEnvironment, project: Any, database: Any, prepare: Any
) -> None:
    """The whole act, in one call: judged, recorded, and the node ended.

    The recorded statement is compared against the frozen criterion rather than
    against what the caller sent, because the caller does not send one: Review
    says what it found, and the criteria it found it against are facts RAVEL
    already holds.
    """
    prepared = prepare()
    _to_reviewing(database, project.project_id, prepared.node_id)
    criterion = prepared.acceptance.criteria[0]

    result = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(
            (
                "submit_review",
                {
                    "node_id": prepared.node_id,
                    "checkpoint": "FINAL",
                    "outcome": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": criterion.criterion_id,
                            "satisfied": True,
                            "observed": "Conductivity rose 18% across the series.",
                        }
                    ],
                    "diagnosis": "The delivered series shows the required gain.",
                    "recommendations": ["Repeat at the upper dopant level."],
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["status_before"] == "REVIEWING"
    assert call.payload["moved_to"] == "PASSED"
    assert call.payload["review"]["outcome"] == "PASS"
    assert call.payload["review"]["frozen_criteria_ref"] == prepared.acceptance.contract_id
    assert call.payload["review"]["frozen_criteria_version"] == prepared.acceptance.version

    with database.read_only() as session:
        status = DagRepository(session, project.project_id).node(prepared.node_id).status
        assert DecisionRepository(session, project.project_id).all() == [], (
            "a verdict is Review's; deciding what to do about it is Master's"
        )
    final = _verdicts(database, project.project_id, prepared.node_id, ReviewCheckpoint.FINAL)

    assert status is NodeStatus.PASSED
    assert [review.review_id for review in final] == [call.payload["review"]["review_id"]]
    recorded = final[-1].criterion_results[0]
    assert recorded.criterion_id == criterion.criterion_id
    assert recorded.statement == criterion.statement
    assert recorded.observed == "Conductivity rose 18% across the series."


async def test_a_verdict_that_answers_with_a_string_is_refused(
    role_environment: RoleEnvironment, project: Any, database: Any, prepare: Any
) -> None:
    """`"false"` is not false, and reading it as one would invert the verdict.

    A non-empty string is truthy, so a reviewer that wrote
    `"satisfied": "false"` — being careful, in the wrong way — would have its
    judgement recorded as the criterion having been met. Nothing downstream can
    detect that: the record would say PASS, with a statement and an observation
    that read as consistent with it. So the type is demanded, and the refusal
    names what arrived.
    """
    prepared = prepare()
    _to_reviewing(database, project.project_id, prepared.node_id)
    criterion = prepared.acceptance.criteria[0]

    result = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(
            (
                "submit_review",
                {
                    "node_id": prepared.node_id,
                    "checkpoint": "FINAL",
                    "outcome": "PASS",
                    "criterion_results": [
                        {"criterion_id": criterion.criterion_id, "satisfied": "false"}
                    ],
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert "'false'" in (call.error or "")
    assert _verdicts(database, project.project_id, prepared.node_id, ReviewCheckpoint.FINAL) == []


# ── What is refused ─────────────────────────────────────────────────────────


async def test_a_final_verdict_that_skips_a_criterion_is_refused(
    role_environment: RoleEnvironment, project: Any, database: Any, prepare: Any
) -> None:
    """A PASS over a question nobody answered is not a verdict.

    The refusal has to reach the model with the criterion named, or the only
    way to find out what was missing is to guess and be refused again.
    """
    prepared = prepare()
    _to_reviewing(database, project.project_id, prepared.node_id)

    result = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(
            (
                "submit_review",
                {
                    "node_id": prepared.node_id,
                    "checkpoint": "FINAL",
                    "outcome": "PASS",
                    "diagnosis": "It looked fine.",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert call.error is not None
    prefix = "Error executing tool submit_review"
    assert call.error.startswith(prefix)
    reason = call.error[len(prefix) :].lstrip(": ")
    criterion_id = prepared.acceptance.criteria[0].criterion_id
    assert criterion_id in reason, (
        f"the refusal has to name the criterion nobody answered; it said {reason!r}"
    )

    assert _verdicts(database, project.project_id, prepared.node_id, ReviewCheckpoint.FINAL) == []
    with database.read_only() as session:
        assert (
            DagRepository(session, project.project_id).node(prepared.node_id).status
            is NodeStatus.REVIEWING
        ), "a refused verdict leaves the node where it was"


async def test_a_verdict_naming_a_criterion_that_was_never_frozen_is_refused(
    role_environment: RoleEnvironment, project: Any, database: Any, prepare: Any
) -> None:
    """The freeze is what makes a verdict measurable; this is the freeze working.

    The criterion named here is a well-formed identifier that this node never
    had, which is exactly what a reviewer working from memory or from a
    superseded version of the criteria would produce.
    """
    prepared = prepare()
    _to_reviewing(database, project.project_id, prepared.node_id)

    result = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(
            (
                "submit_review",
                {
                    "node_id": prepared.node_id,
                    "checkpoint": "FINAL",
                    "outcome": "PASS",
                    "criterion_results": [
                        {"criterion_id": "crit-never-frozen", "satisfied": True}
                    ],
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert "crit-never-frozen" in (call.error or "")
    assert _verdicts(database, project.project_id, prepared.node_id, ReviewCheckpoint.FINAL) == []


async def test_a_verdict_at_a_checkpoint_the_node_is_not_at_is_refused(
    role_environment: RoleEnvironment, project: Any, prepare: Any
) -> None:
    """A final verdict on work that never ran is a verdict about nothing."""
    prepared = prepare(cleared=False)

    result = await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(
            (
                "submit_review",
                {
                    "node_id": prepared.node_id,
                    "checkpoint": "FINAL",
                    "outcome": "PASS",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert call.error is not None
    assert "REVIEWING" in call.error, (
        "the refusal says which status a final verdict is of, so the reviewer can "
        f"tell a wrong checkpoint from a wrong node; it said {call.error!r}"
    )


async def test_the_contract_a_verdict_names_is_read_rather_than_given(
    role_environment: RoleEnvironment, project: Any, database: Any, prepare: Any
) -> None:
    """A reviewer cannot point its verdict at a contract of its own choosing.

    There is no parameter for it, so the only way to name another contract is
    to be another caller — and this asserts the record on the node is the one
    that was frozen *and* that the node's binding agrees with the repository's
    lookup, which is what a second version of the criteria would break.
    """
    prepared = prepare(cleared=False)

    await probe(
        role_environment.for_project(project, AgentRole.REVIEW),
        calls=(
            (
                "submit_review",
                {
                    "node_id": prepared.node_id,
                    "checkpoint": "PRE_RUN",
                    "outcome": "PASS",
                    "criterion_results": [
                        {
                            "criterion_id": prepared.acceptance.criteria[0].criterion_id,
                            "satisfied": True,
                        }
                    ],
                },
            ),
        ),
    )

    with database.read_only() as session:
        node = DagRepository(session, project.project_id).node(prepared.node_id)
        frozen = AcceptanceContractRepository(
            session, project.project_id
        ).frozen_for_node(prepared.node_id)
        written = ReviewRepository(session, project.project_id).for_node(prepared.node_id)

    assert frozen is not None
    assert node.acceptance_contract_ref == frozen.contract_id
    assert [review.frozen_criteria_ref for review in written] == [frozen.contract_id]
    assert [review.frozen_criteria_version for review in written] == [frozen.version]
