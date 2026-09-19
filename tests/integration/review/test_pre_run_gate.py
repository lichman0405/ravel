"""The pre-flight review is a gate, not a document.

`acceptance/V0_ACCEPTANCE.md` A09 asks that a COMPUTATION or EXPERIMENT node
cannot be handed to a Worker before its pre-flight review has passed, and the
distinction this file exists to hold is between a rule that is *written down*
and one that is *enforced*. `pre_run_clearance` was the first of those for a
while: it was documented as the checkpoint that clears a node to run, every
test of it passed, and nothing on the path into RUNNING called it. A node could
be run without ever being reviewed, and the only thing standing in the way was
that nobody had written the code to skip the review.

So the gate lives in `DagNode.can_enter_running`, which is the one place every
route into RUNNING passes through — a Worker's activity, Master's own
transition, a test's fixture — and the tests here are about the *transition*
refusing, not about a function agreeing that it would.

The role tests are here for the same reason. A gate that only Review can open
is worth nothing if a Worker can write the PASS itself, so "who may submit a
verdict" is part of what the checkpoint enforces rather than a separate
concern.
"""

from __future__ import annotations

import pytest
from tests.integration.conftest import Prepared
from tests.integration.review.conftest import verdict

from ravel.domain.enums import NodeStatus, NodeType, ReviewCheckpoint, ReviewOutcome
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TransitionError
from ravel.review import ReviewService, pre_run_clearance
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository

pytestmark = pytest.mark.integration


def _start(database: Database, prepared: Prepared) -> NodeStatus:
    """Try to start the node the way a Worker's activity would.

    The exception is not caught: a test that expects a run to be *allowed* wants
    the refusal to fail the test loudly, and one that expects a refusal says so
    with `pytest.raises`. A helper that returned a boolean would let a test
    assert on the wrong side of the gate and still pass.
    """
    with database.transaction() as session:
        node = DagRepository(session, prepared.project_id).transition_node(
            prepared.node_id, NodeStatus.RUNNING, actor_id="compute-worker"
        )
        return node.status


def _clear(prepared: Prepared, submit, outcome: ReviewOutcome = ReviewOutcome.PASS):
    """Submit the node's pre-flight verdict, through the service."""
    return submit(
        verdict(
            prepared,
            checkpoint=ReviewCheckpoint.PRE_RUN,
            outcome=outcome,
            criterion_results=(),
            diagnosis="The criteria are measurable and the contract permits the run.",
        )
    )


# ── The gate on the transition ──────────────────────────────────────────────


def test_a_computation_that_was_never_reviewed_cannot_start(computation, database):
    with pytest.raises(TransitionError, match="has not been reviewed"):
        _start(database, computation)


def test_a_cleared_computation_starts(computation, submit, database):
    _clear(computation, submit)

    assert _start(database, computation) is NodeStatus.RUNNING


def test_a_refused_computation_cannot_start(computation, submit, database):
    """The node is parked, and the revival does not open the gate.

    A refused pre-flight review leaves the node at WAITING_DECISION, and Master
    may send it back to READY to be revised. That route exists so the plan can
    be fixed; it is not a way to run the plan that was refused, which is why
    the refusal is still the latest verdict on the node and RUNNING is still
    refused from WAITING_DECISION as well as from READY.
    """
    _clear(computation, submit, outcome=ReviewOutcome.FAIL)
    with database.transaction() as session:
        DagRepository(session, computation.project_id).transition_node(
            computation.node_id, NodeStatus.READY, actor_id="master"
        )

    with pytest.raises(TransitionError, match="the verdict was FAIL"):
        _start(database, computation)


def test_a_partial_verdict_does_not_clear_the_node(computation, submit, database):
    """Only a PASS clears. PARTIAL is a judgement that something is missing."""
    _clear(computation, submit, outcome=ReviewOutcome.PARTIAL)

    with pytest.raises(TransitionError, match="the verdict was PARTIAL"):
        _start(database, computation)


def test_a_research_node_runs_without_a_pre_flight_review(prepare, database):
    """The gate applies to the node types that freeze criteria, and no others.

    A RESEARCH node has no pre-flight review to pass, so requiring one would
    make it un-runnable — a gate that stops work the acceptance criteria say
    should proceed is a different bug from the one this file is about.
    """
    node = prepare(node_type=NodeType.RESEARCH, with_acceptance=False, cleared=False)

    assert _start(database, node) is NodeStatus.RUNNING


def test_the_gate_cannot_be_opened_by_looking_cleared(computation, submit, database):
    """The transition asks the reviews, it does not take anyone's word.

    A caller holds a `Prepared` whose node record it read before the verdict was
    written, and a gate that trusted that copy would open on the strength of a
    status the database no longer agrees with.
    """
    _clear(computation, submit)

    # The record the caller is holding still says READY and knows nothing about
    # the review; the run is allowed anyway, because the *database* has it.
    assert computation.node.status is NodeStatus.READY
    assert _start(database, computation) is NodeStatus.RUNNING


# ── Who may open it ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "role",
    [AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER, AgentRole.MASTER],
)
def test_only_review_may_submit_a_verdict(computation, database, reviews_of, role):
    """The three powers, kept apart at the one door that writes a verdict.

    A Worker that could submit its own PASS would be reviewing its own work,
    which is the separation `docs/02_AGENT_MODEL.md` §3 exists to draw — and
    Master is refused for the same reason: accepting a result is Review's
    authority, and Master's own route to overriding a verdict is a Decision
    Record, which is visible as the different thing it is.
    """
    with (
        pytest.raises(PermissionError, match="may not submit a Review Record"),
        database.transaction() as session,
    ):
        ReviewService(session, computation.project_id).submit(
            verdict(
                computation,
                checkpoint=ReviewCheckpoint.PRE_RUN,
                criterion_results=(),
                diagnosis="The plan looks fine to me.",
            ),
            role=role,
        )

    assert reviews_of(computation.project_id, computation.node_id) == [], (
        "the refusal has to happen before the record is written"
    )


def test_the_refused_worker_does_not_open_the_gate(computation, database):
    """End to end: the attempt above leaves the node exactly as un-runnable."""
    with (
        pytest.raises(PermissionError),
        database.transaction() as session,
    ):
        ReviewService(session, computation.project_id).submit(
            verdict(
                computation,
                checkpoint=ReviewCheckpoint.PRE_RUN,
                criterion_results=(),
                diagnosis="The plan looks fine to me.",
            ),
            role=AgentRole.COMPUTE_WORKER,
        )

    with pytest.raises(TransitionError, match="has not been reviewed"):
        _start(database, computation)


# ── The two answers must not drift ──────────────────────────────────────────


@pytest.mark.parametrize(
    "outcome",
    [None, ReviewOutcome.PASS, ReviewOutcome.FAIL],
    ids=["never-reviewed", "cleared", "refused"],
)
def test_the_enforced_gate_and_the_readable_one_agree(
    prepare, driving, submit, reviews_of, database, outcome
):
    """Two implementations of one rule, asked the same question.

    `pre_run_clearance` answers in Python from a list of Review Records and
    exists so a caller can say *why* a node cannot start; the gate that actually
    refuses reads the latest PRE_RUN outcome out of SQL. Nothing makes them
    agree except being asked, and the drift is silent in the direction that
    matters: an explanation that says a node is cleared beside a transition that
    refuses it reads as a bug in the transition.

    The node is READY in all three cases — including the refused one, which is
    driven back the way Master would drive it — because a node in some other
    status is refused by both for a reason that has nothing to do with reviews,
    and the comparison would then hold even if the review check were deleted.
    """
    prepared = prepare(cleared=False)
    if outcome is not None:
        _clear(prepared, submit, outcome=outcome)
    if outcome is ReviewOutcome.FAIL:
        prepared = driving(prepared, NodeStatus.READY)

    with database.read_only() as session:
        dag = DagRepository(session, prepared.project_id)
        node = dag.node(prepared.node_id)
        enforced = node.can_enter_running(
            has_frozen_acceptance=dag.has_frozen_acceptance(node.node_id),
            has_execution_contract=dag.has_execution_contract(node.node_id),
            pre_run_outcome=dag.latest_pre_run_outcome(node.node_id),
        )

    readable = pre_run_clearance(
        node, reviews_of(prepared.project_id, prepared.node_id)
    )

    assert node.status is NodeStatus.READY, "the comparison assumes a node waiting to run"
    assert enforced.allowed == readable.allowed, (
        f"the transition says {enforced.reason!r} and the explanation says "
        f"{readable.reason!r}"
    )
