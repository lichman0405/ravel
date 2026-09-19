"""Which reviews a node owes, and which of them hold it up.

The pre-flight review is the only one with a gate in it: a COMPUTATION or
EXPERIMENT node freezes its acceptance criteria before it runs, and the
pre-flight review is what reads them, so an absent or refused one is the
difference between a node that may be handed to a Worker and one that may not.

The rest of this file is about the two ways that answer can be *not yes* — a
node that owes no pre-flight review, where the question does not apply, and a
node that is not waiting to run, where it applies to a different moment. Both
are denials of a sort, and neither is a refusal of the node's plan.
"""

from __future__ import annotations

import pytest
from tests.integration.review.conftest import verdict

from ravel.domain.enums import NodeStatus, NodeType, ReviewCheckpoint, ReviewOutcome
from ravel.review import (
    PRE_RUN_NODE_TYPES,
    NotClearedError,
    pre_run_clearance,
    required_checkpoints,
)
from ravel.review.checkpoints import PRE_RUN_NODE_TYPES as EXPORTED

pytestmark = pytest.mark.integration


def test_every_node_that_freezes_criteria_owes_a_pre_flight_review():
    assert required_checkpoints(NodeType.COMPUTATION) == (
        ReviewCheckpoint.PRE_RUN,
        ReviewCheckpoint.FINAL,
    )
    assert required_checkpoints(NodeType.EXPERIMENT) == (
        ReviewCheckpoint.PRE_RUN,
        ReviewCheckpoint.FINAL,
    )


def test_a_node_that_freezes_nothing_owes_only_a_final_review():
    assert required_checkpoints(NodeType.RESEARCH) == (ReviewCheckpoint.FINAL,)


def test_the_pre_flight_node_types_are_the_ones_that_freeze_criteria():
    assert {NodeType.COMPUTATION, NodeType.EXPERIMENT} == PRE_RUN_NODE_TYPES
    assert EXPORTED is PRE_RUN_NODE_TYPES, (
        "the package re-exports the same object, so a caller cannot widen the "
        "gate by importing it from the other place"
    )


def test_a_node_that_has_not_been_reviewed_is_not_cleared(computation):
    clearance = pre_run_clearance(computation.node, [])

    assert not clearance.allowed
    assert "has not been reviewed" in clearance.reason
    with pytest.raises(NotClearedError):
        clearance.raise_if_denied()


def test_a_node_that_owes_no_pre_flight_review_is_clear(prepare):
    node = prepare(node_type=NodeType.RESEARCH, with_acceptance=False)

    clearance = pre_run_clearance(node.node, [])

    assert clearance.allowed
    assert "owes no pre-flight review" in clearance.reason, (
        "clear because the question does not apply is a different answer from "
        "clear because someone said yes, and the reason has to say which"
    )
    clearance.raise_if_denied()


def test_a_pre_flight_pass_clears_the_node(computation, submit, reviews_of):
    submitted = submit(
        verdict(
            computation,
            checkpoint=ReviewCheckpoint.PRE_RUN,
            criterion_results=(),
        )
    )

    clearance = pre_run_clearance(
        computation.node, reviews_of(computation.project_id, computation.node_id)
    )

    assert clearance.allowed
    assert submitted.review.display_id in clearance.reason


def test_a_pre_flight_refusal_does_not_clear_the_node(computation, submit, reviews_of):
    submitted = submit(
        verdict(
            computation,
            checkpoint=ReviewCheckpoint.PRE_RUN,
            outcome=ReviewOutcome.PARTIAL,
            criterion_results=(),
            diagnosis="The target is stated in units the instrument does not report.",
        )
    )

    clearance = pre_run_clearance(
        computation.node, reviews_of(computation.project_id, computation.node_id)
    )

    assert not clearance.allowed
    assert "PARTIAL" in clearance.reason
    assert submitted.review.display_id in clearance.reason


def test_a_revised_plan_is_judged_by_the_review_that_read_it(
    computation, driving, submit, reviews_of, status_of
):
    """Master sends a refused node back, and the second opinion is the one that counts.

    Which is why `latest_at` is latest and not first: the first review is about
    a plan that no longer exists, and clearing a node on the strength of it
    would be clearing the plan that was refused.
    """
    submit(
        verdict(
            computation,
            checkpoint=ReviewCheckpoint.PRE_RUN,
            outcome=ReviewOutcome.FAIL,
            criterion_results=(),
            diagnosis="The contract does not collect the quantity the criterion names.",
        )
    )
    assert status_of(computation.project_id, computation.node_id) is (
        NodeStatus.WAITING_DECISION
    )

    # Master's move, not Review's: the node goes back to waiting to run.
    node = driving(computation, NodeStatus.READY)
    assert status_of(computation.project_id, computation.node_id) is NodeStatus.READY

    cleared = submit(
        verdict(
            node,
            checkpoint=ReviewCheckpoint.PRE_RUN,
            criterion_results=(),
            diagnosis="The revised contract collects it, in the units the criterion names.",
        )
    )

    written = reviews_of(computation.project_id, computation.node_id)
    assert [review.checkpoint for review in written] == [
        ReviewCheckpoint.PRE_RUN,
        ReviewCheckpoint.PRE_RUN,
    ]
    clearance = pre_run_clearance(node.node, written)
    assert clearance.allowed
    assert cleared.review.display_id in clearance.reason


def test_a_node_that_is_not_waiting_to_run_cannot_be_cleared(computation, driving):
    """A clearance is about a moment, not about a node.

    A RUNNING node has not been refused anything — it is past the point the
    question applies to, and answering yes would be a clearance to do what it
    is already doing.
    """
    node = driving(computation, NodeStatus.RUNNING)

    clearance = pre_run_clearance(node.node, [])

    assert not clearance.allowed
    assert "RUNNING" in clearance.reason
    assert "only a node waiting to run" in clearance.reason
