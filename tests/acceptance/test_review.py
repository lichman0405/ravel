"""A08 and A09: the verdict, and what a bad one does to the plan.

The two are one argument in two halves. A08 is that a result is judged against
criteria that were frozen before it existed, and that the judgement is a record
rather than a message. A09 is what follows: a failure is not an error to be
retried away, it is an answer — one that Master reads, records a decision about,
and plans differently in light of, while the node that produced it stays in the
DAG saying what it came to.

Neither is about a model's opinion. Both are asserted on rows in PostgreSQL.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.e2e.conftest import Headless, Task
from tests.integration.conftest import Prepared
from tests.integration.review.conftest import verdict

from ravel.domain.dag import DagNode
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import (
    DecisionType,
    NodeStatus,
    NodeType,
    ProjectStatus,
    ReviewCheckpoint,
    ReviewOutcome,
)
from ravel.domain.roles import AgentRole
from ravel.review import SubmittedReview
from ravel.state.repositories.contracts import AcceptanceContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import DecisionRepository, ReviewRepository

pytestmark = [pytest.mark.acceptance, pytest.mark.timeout(600)]

#: What A08's node is frozen against. Two criteria, because PARTIAL is a verdict
#: about a set: a node with one criterion can only have passed it or failed it,
#: so a one-criterion PARTIAL would be a verdict with nothing to be partial about.
CRITERIA = (
    "Conductivity rises by at least 15%.",
    "The gain survives 500 hours under load.",
)

#: One case per outcome A08 names: the finding a Reviewer would have made, and
#: the verdict and node status that follow from it.
CASES = [
    pytest.param((True, True), ReviewOutcome.PASS, NodeStatus.PASSED, id="pass"),
    pytest.param((False, False), ReviewOutcome.FAIL, NodeStatus.FAILED, id="fail"),
    pytest.param((True, False), ReviewOutcome.PARTIAL, NodeStatus.PARTIAL, id="partial"),
]

#: The objectives A09's script plans by. It picks its verdict by phrase, so the
#: replacement must not contain the failing one — a replacement that failed the
#: same way would be the same node asked again, which is what A09 is not.
FAILING = "Measure the series."
REPLACEMENT = "Re-measure with the alternate protocol."


def _node(headless: Headless, objective: str, node_type: NodeType) -> DagNode:
    """One node, built the way the domain builds one."""
    return DagNode.create(
        project_id=headless.project.project_id,
        node_type=node_type,
        objective=objective,
        created_by=AgentRole.MASTER.value,
    )


# ── A08 ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(("answers", "outcome", "ending"), CASES)
def test_a08_a_verdict_is_recorded_against_the_frozen_criteria(
    prepare: Callable[..., Prepared],
    driving: Callable[[Prepared, NodeStatus], Prepared],
    submit: Callable[..., SubmittedReview],
    reviews_of: Callable[..., list[ReviewRecord]],
    status_of: Callable[[str, str], NodeStatus],
    answers: tuple[bool, ...],
    outcome: ReviewOutcome,
    ending: NodeStatus,
) -> None:
    """A08: "Review returns PASS/FAIL/PARTIAL against frozen criteria and
    persists ReviewRecord."

    Three things, and each is checked rather than assumed. The *criteria* are
    the ones frozen before the node could run — the review names that contract
    and that version, so a verdict cannot be measured against a definition of
    done written after the result came in. The *outcome* follows from the
    findings rather than sitting beside them: the results are the reviewer's, and
    PARTIAL is one criterion met and one not, which is what makes it a third
    answer rather than a synonym for FAIL. And the *record* is read back out of
    PostgreSQL, because a verdict that lived only in a return value would be one
    the next session could not find.

    The node is walked to REVIEWING along the real transitions rather than
    having its status written, so this is a review of a node that could have got
    there. What is not performed here is the run itself: the subject is the
    verdict, and the loop that judges a real Execution Record's result end to end
    is A20's and A06's, in this same suite.
    """
    prepared = prepare(criteria=CRITERIA, cleared=False)
    assert prepared.acceptance is not None, "a COMPUTATION node runs against criteria"
    frozen = prepared.acceptance
    prepared = driving(prepared, NodeStatus.REVIEWING)

    submitted = submit(verdict(prepared, answers=answers, outcome=outcome))

    assert submitted.review.outcome is outcome, "the service changed the verdict"
    assert submitted.moved_to is ending, (
        f"a {outcome.value} verdict left the node at {submitted.moved_to}"
    )

    written = reviews_of(prepared.project_id, prepared.node_id, ReviewCheckpoint.FINAL)
    assert len(written) == 1, f"expected one final review, found {len(written)}"
    record = written[0]
    assert record.review_id == submitted.review.review_id, (
        "the review that was applied is not the review that was stored"
    )
    assert record.frozen_criteria_ref == frozen.contract_id
    assert record.frozen_criteria_version == frozen.version, (
        "the verdict names a version other than the one that was frozen"
    )
    assert {result.criterion_id for result in record.criterion_results} == {
        criterion.criterion_id for criterion in frozen.criteria
    }, "a frozen criterion nobody answered is a criterion nobody checked"
    assert record.satisfied_count == sum(answers)
    assert record.diagnosis, "a verdict with no stated basis"
    assert status_of(prepared.project_id, prepared.node_id) is ending


# ── A09 ─────────────────────────────────────────────────────────────────────


async def test_a09_a_failure_replans_the_future_and_leaves_the_past_alone(
    headless: Headless,
) -> None:
    """A09: "A FAIL causes Master to create DecisionRecord and mutate future DAG;
    original failed node remains historically failed."

    *A FAIL*, not a failed run: the computation completes and delivers what it
    owed, and it is Review that finds the criteria unmet. The distinction is the
    whole reason the verdict comes from a different agent than the run did, and
    the script is written so the two cannot be confused — the scenario is a
    success, and the failing word belongs to the reviewer.

    *Mutate future DAG* is asserted as a change to the graph rather than to a
    field: the replacement node exists, it carries the decision that authorized
    it, and it ran and passed. *Remains historically failed* is asserted as the
    absence of a rewrite — the node is still there, still FAILED, still holding
    the criteria it was judged against and the verdict that judged it. A system
    that deleted a failed node or reset it to READY would look the same from
    everywhere except here.
    """
    headless.compute("COMPUTE_SUCCESS")

    def replan(failed: DagNode) -> tuple[Task, ...]:
        """What goes in the failed node's place. `failed` is the contract's."""
        return (Task(build=lambda: _node(headless, REPLACEMENT, NodeType.COMPUTATION)),)

    master = headless.master(
        (
            Task(
                build=lambda: _node(headless, FAILING, NodeType.COMPUTATION),
                criteria=CRITERIA,
            ),
        ),
        replan=replan,
    )
    review = headless.review({FAILING: ReviewOutcome.FAIL})

    run = await headless.drive(master, review)

    assert run.finished, f"the loop halted at {run.status.value}"
    assert run.status is ProjectStatus.COMPLETED, (
        "the project did not move past the failure, so nothing was replanned"
    )
    assert master.trace.count("replanned") == 1

    planned = master.planned[0]
    with headless.database.read_only() as session:
        nodes = DagRepository(session, headless.project.project_id).nodes()
        decisions = DecisionRepository(session, headless.project.project_id).all()
        # Read inside the block, not after it. A repository built here and asked
        # a question outside would begin a second transaction on a session whose
        # connection was already returned, and nothing would ever return that
        # one — leaving a connection `idle in transaction` holding a read lock
        # on the table it read. The next test's TRUNCATE then waits on it for as
        # long as its timeout allows, and the failure lands on the *next* test
        # rather than this one.
        criteria = AcceptanceContractRepository(
            session, headless.project.project_id
        ).for_node(planned.node_id)
        verdicts = ReviewRepository(session, headless.project.project_id).for_node(
            planned.node_id
        )

    # Read back rather than kept from the script: the node Master planned is the
    # node before its terms were bound, and a test that asserted on that copy
    # would be reading a plan rather than what the run left behind.
    failed = next((node for node in nodes if node.node_id == planned.node_id), None)
    assert failed is not None, (
        "the node that failed is gone from the DAG; a failure is a result, and a "
        "result that has been deleted is one nobody can be asked about"
    )

    replans = [
        decision
        for decision in decisions
        if decision.decision_type is DecisionType.REPLACE_NODE
    ]
    assert len(replans) == 1, f"expected one replan, found {len(replans)}"
    decision = replans[0]
    assert decision.authority_check.actor_role == AgentRole.MASTER.value, (
        "only Master changes the plan; a replan recorded against another actor "
        "would mean the DAG moved for some other reason"
    )
    assert decision.authority_check.permitted
    assert decision.rationale, "a replan with no stated reason"
    assert len(decision.affected_nodes.created) == 1

    replacements = [node for node in nodes if node.node_id != failed.node_id]
    assert len(replacements) == 1, (
        f"the DAG holds {len(nodes)} nodes after one failure and one replan"
    )
    replacement = replacements[0]
    assert replacement.status is NodeStatus.PASSED, (
        "the replacement did not run, so the plan was changed and not carried out"
    )
    assert decision.affected_nodes.created == (replacement.node_id,), (
        "the decision does not name the node it created"
    )
    assert replacement.decision_ref == decision.decision_id, (
        "the new work does not say which decision authorized it"
    )
    assert replacement.objective == REPLACEMENT

    # And the past. The failed node is still in the graph, saying what it came
    # to — not deleted, not reset, and not quietly replaced in place.
    assert failed.status is NodeStatus.FAILED
    assert failed.objective == FAILING
    assert [
        criterion.statement for criterion in criteria.criteria
    ] == list(CRITERIA), "the criteria it failed were edited after the fact"
    finals = [review for review in verdicts if review.checkpoint is ReviewCheckpoint.FINAL]
    assert [review.outcome for review in finals] == [ReviewOutcome.FAIL], (
        "the verdict that caused the replan is not the verdict on record"
    )
