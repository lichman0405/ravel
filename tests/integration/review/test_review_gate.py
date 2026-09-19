"""A verdict moves the node, and only where Review's authority reaches.

`docs/02_AGENT_MODEL.md` §3 gives Review three powers — accept, diagnose,
advise — and three prohibitions: no DAG mutation, no Execution Contract change,
no altered acceptance criteria. A FINAL verdict is the one place where
accepting *is* a move, because a node sitting in REVIEWING is waiting for
judgement and Review is the role that ends it. Everywhere else the record is
the whole of what Review produces, and this file is where those two claims are
kept apart.

The refusals matter as much as the moves. A verdict over criteria the node
never froze, or one that answers part of what it owed and calls it a PASS, is
not a weaker measurement — it is a measurement of something else, and the
record would carry it as though it were about the node.
"""

from __future__ import annotations

import pytest
from tests.integration.review.conftest import against_the_contract, verdict

from ravel.domain.decisions import CriterionResult
from ravel.domain.enums import NodeStatus, NodeType, ReviewCheckpoint, ReviewOutcome
from ravel.review import ReviewError, latest_at, pre_run_clearance

pytestmark = pytest.mark.integration


# ── The final verdict, which is what ends a node ────────────────────────────


def test_a_final_pass_ends_the_node_as_passed(computation, driving, submit, status_of):
    node = driving(computation, NodeStatus.REVIEWING)

    submitted = submit(verdict(node))

    assert submitted.moved_to is NodeStatus.PASSED
    assert status_of(node.project_id, node.node_id) is NodeStatus.PASSED


def test_a_final_fail_ends_the_node_as_failed(computation, driving, submit, status_of):
    node = driving(computation, NodeStatus.REVIEWING)

    submitted = submit(verdict(node, satisfied=False, outcome=ReviewOutcome.FAIL))

    assert submitted.moved_to is NodeStatus.FAILED
    assert status_of(node.project_id, node.node_id) is NodeStatus.FAILED


def test_a_partly_met_result_ends_the_node_as_partial(prepare, driving, submit, status_of):
    node = driving(
        prepare(criteria=("Conductivity rises by 15%.", "Yield exceeds 40%.")),
        NodeStatus.REVIEWING,
    )
    submitted = submit(
        verdict(node, answers=(True, False), outcome=ReviewOutcome.PARTIAL)
    )

    assert submitted.moved_to is NodeStatus.PARTIAL
    assert status_of(node.project_id, node.node_id) is NodeStatus.PARTIAL


# ── Refusals: a verdict that is not about the frozen criteria ───────────────


def test_a_final_verdict_must_answer_every_frozen_criterion(
    prepare, driving, submit, reviews_of
):
    node = driving(
        prepare(criteria=("Conductivity rises by 15%.", "Yield exceeds 40%.")),
        NodeStatus.REVIEWING,
    )
    answered = verdict(node)
    half = answered.model_copy(update={"criterion_results": answered.criterion_results[:1]})

    with pytest.raises(ReviewError, match=node.acceptance.criteria[1].criterion_id):
        submit(half)
    assert reviews_of(node.project_id, node.node_id, ReviewCheckpoint.FINAL) == [], (
        "the refusal has to happen before the record is written; a review "
        "rejected after it was stored is a review someone can still read"
    )


def test_a_verdict_over_a_criterion_that_was_never_frozen_is_refused(
    computation, driving, submit
):
    node = driving(computation, NodeStatus.REVIEWING)
    honest = verdict(node)
    invented = honest.model_copy(
        update={
            "criterion_results": (
                *honest.criterion_results,
                # A criterion from some other node's contract, or from a
                # version of this one that was never frozen.
                honest.criterion_results[0].model_copy(
                    update={"criterion_id": "crit-from-elsewhere"}
                ),
            )
        }
    )

    with pytest.raises(ReviewError, match="crit-from-elsewhere"):
        submit(invented)


def test_a_verdict_naming_a_different_version_is_refused(computation, driving, submit):
    node = driving(computation, NodeStatus.REVIEWING)

    with pytest.raises(ReviewError, match="version"):
        submit(verdict(node, frozen_criteria_version=node.acceptance.version + 1))


def test_a_verdict_naming_some_other_contract_is_refused(computation, driving, submit):
    node = driving(computation, NodeStatus.REVIEWING)

    with pytest.raises(ReviewError):
        submit(verdict(node, frozen_criteria_ref=node.contract.contract_id))


def test_a_final_verdict_on_work_still_in_flight_is_refused(
    computation, driving, submit, reviews_of
):
    node = driving(computation, NodeStatus.RUNNING)

    with pytest.raises(ReviewError, match="REVIEWING"):
        submit(verdict(node))
    assert reviews_of(node.project_id, node.node_id, ReviewCheckpoint.FINAL) == []


def test_a_passing_verdict_the_criteria_contradict_is_refused(computation, driving):
    node = driving(computation, NodeStatus.REVIEWING)

    with pytest.raises(ValueError, match="none of"):
        verdict(node, satisfied=False, outcome=ReviewOutcome.PASS)


# ── The checkpoints that do not end a node ──────────────────────────────────


def test_a_pre_flight_pass_clears_the_node_without_moving_it(
    computation, submit, status_of, reviews_of
):
    pre_run = verdict(
        computation,
        checkpoint=ReviewCheckpoint.PRE_RUN,
        criterion_results=(),
        diagnosis="The criteria are measurable and the contract permits the run.",
    )

    submitted = submit(pre_run)

    assert submitted.moved_to is None
    assert status_of(computation.project_id, computation.node_id) is NodeStatus.READY
    clearance = pre_run_clearance(
        computation.node, reviews_of(computation.project_id, computation.node_id)
    )
    assert clearance.allowed, clearance.reason
    assert clearance.reason.endswith(submitted.review.display_id)


def test_a_pre_flight_refusal_parks_the_node_for_master(computation, submit, status_of):
    refused = verdict(
        computation,
        checkpoint=ReviewCheckpoint.PRE_RUN,
        outcome=ReviewOutcome.FAIL,
        criterion_results=(),
        diagnosis="The criterion cannot be measured from what the contract collects.",
    )

    submitted = submit(refused)

    assert submitted.moved_to is NodeStatus.WAITING_DECISION
    assert status_of(computation.project_id, computation.node_id) is (
        NodeStatus.WAITING_DECISION
    )


def test_a_runtime_review_advises_and_moves_nothing(computation, driving, submit, status_of):
    node = driving(computation, NodeStatus.RUNNING)
    advisory = verdict(
        node,
        checkpoint=ReviewCheckpoint.RUNTIME,
        outcome=ReviewOutcome.FAIL,
        criterion_results=(),
        diagnosis="The first two samples are drifting below the target.",
        recommendations=("Run the remaining samples at the higher temperature.",),
    )

    submitted = submit(advisory)

    assert submitted.moved_to is None, (
        "a runtime recommendation is advisory; acting on one is Master's, "
        "through a Decision Record"
    )
    assert status_of(node.project_id, node.node_id) is NodeStatus.RUNNING


# ── Nodes that have no acceptance criteria to be measured against ───────────


def test_a_node_without_criteria_is_measured_against_its_execution_contract(
    prepare, driving, submit, status_of
):
    node = driving(
        prepare(node_type=NodeType.RESEARCH, with_acceptance=False),
        NodeStatus.REVIEWING,
    )

    submitted = submit(against_the_contract(node))

    assert submitted.review.frozen_criteria_ref == node.contract.contract_id
    assert submitted.moved_to is NodeStatus.PASSED
    assert status_of(node.project_id, node.node_id) is NodeStatus.PASSED


def test_criterion_results_on_a_node_without_criteria_are_refused(
    prepare, driving, submit
):
    node = driving(
        prepare(node_type=NodeType.RESEARCH, with_acceptance=False),
        NodeStatus.REVIEWING,
    )
    borrowed = against_the_contract(
        node,
        criterion_results=(
            CriterionResult(criterion_id="crit-borrowed", satisfied=True),
        ),
    )

    with pytest.raises(ReviewError, match="Execution Contract"):
        submit(borrowed)


def test_a_node_whose_criteria_were_never_frozen_cannot_be_reviewed(
    prepare, driving, submit
):
    # A RESEARCH node, because a COMPUTATION one cannot reach RUNNING at all
    # without frozen criteria — the refusal below is about a node that got
    # here, not about one the state machine already stopped.
    node = driving(
        prepare(
            node_type=NodeType.RESEARCH,
            with_acceptance=True,
            freeze_acceptance=False,
        ),
        NodeStatus.REVIEWING,
    )

    with pytest.raises(ReviewError, match="never frozen"):
        submit(against_the_contract(node))


# ── The record itself ───────────────────────────────────────────────────────


def test_a_submitted_review_is_readable_afterwards(
    computation, driving, submit, reviews_of
):
    node = driving(computation, NodeStatus.REVIEWING)

    submitted = submit(verdict(node, review_session_ref="session-master-1"))

    written = reviews_of(node.project_id, node.node_id, ReviewCheckpoint.FINAL)
    assert [review.review_id for review in written] == [submitted.review.review_id]
    assert written[0].review_session_ref == "session-master-1"
    assert latest_at(written, ReviewCheckpoint.FINAL) == written[0]
