"""Fixtures for the review gate.

The gate is `IMPLEMENTATION_PLAN.md` Phase 7's first half: a verdict is a
*measurement against something frozen*, and these fixtures exist so that the
tests can vary one thing at a time — the node's status, the contract a review
names, which criteria it answers — without any of them having to restate how a
runnable node is built.

That is the shared `prepare` fixture from `tests.integration.conftest`, reused
rather than rebuilt: a review of a node whose contracts were assembled
differently from production's would be a review of a node the system cannot
produce.
"""

from __future__ import annotations

# Fixtures are imported and then used as fixture parameters, which ruff reads as
# a redefinition. That is the pytest idiom.
# ruff: noqa: F811
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

import pytest
from sqlalchemy.orm import Session
from tests.integration.conftest import (  # noqa: F401
    DEFAULT_OUTPUTS,
    Prepared,
    artifact_store,
    clean,
    database,
    integration_settings,
    prepare,
    project,
)

from ravel.domain.dag import DagNode
from ravel.domain.decisions import CriterionResult, ReviewRecord
from ravel.domain.enums import NodeStatus, ReviewCheckpoint, ReviewOutcome
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import requires_frozen_criteria
from ravel.review import ReviewService, SubmittedReview
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import ReviewRepository

#: How a test walks a node to a status it wants it in, from wherever it is.
#: Written out rather than searched for: a path found by search would keep
#: working after a transition had been removed from the machine, and the tests
#: that depend on reaching REVIEWING would then be testing a state nothing can
#: reach.
_ROUTES: dict[tuple[NodeStatus, NodeStatus], tuple[NodeStatus, ...]] = {
    (NodeStatus.READY, NodeStatus.READY): (),
    (NodeStatus.READY, NodeStatus.RUNNING): (NodeStatus.RUNNING,),
    (NodeStatus.READY, NodeStatus.WAITING_EXTERNAL): (
        NodeStatus.RUNNING,
        NodeStatus.WAITING_EXTERNAL,
    ),
    (NodeStatus.READY, NodeStatus.WAITING_DECISION): (
        NodeStatus.RUNNING,
        NodeStatus.WAITING_DECISION,
    ),
    (NodeStatus.READY, NodeStatus.REVIEWING): (NodeStatus.RUNNING, NodeStatus.REVIEWING),
    (NodeStatus.READY, NodeStatus.PASSED): (
        NodeStatus.RUNNING,
        NodeStatus.REVIEWING,
        NodeStatus.PASSED,
    ),
    (NodeStatus.READY, NodeStatus.FAILED): (
        NodeStatus.RUNNING,
        NodeStatus.REVIEWING,
        NodeStatus.FAILED,
    ),
    (NodeStatus.READY, NodeStatus.PARTIAL): (
        NodeStatus.RUNNING,
        NodeStatus.REVIEWING,
        NodeStatus.PARTIAL,
    ),
    # Master's move after a refused pre-flight review: the node keeps its place
    # in the graph and goes back to waiting to run under a revised plan.
    (NodeStatus.WAITING_DECISION, NodeStatus.READY): (NodeStatus.READY,),
}


@pytest.fixture
def computation(prepare: Callable[..., Prepared]) -> Prepared:
    """A READY COMPUTATION node with one frozen criterion, awaiting its review.

    Uncleared, and that is this package's starting state: its subject is the
    review, so the node the tests begin from is the one nobody has judged yet.
    The shared `prepare` clears by default because "ready to run" is what most
    callers mean by preparing a node — here it would put a PASS in front of
    every verdict a test submits, and "the refusal did not clear the node" would
    become a race against the fixture's own approval rather than a statement
    about the verdict.
    """
    return prepare(cleared=False)


@pytest.fixture
def driving(database: Database) -> Callable[[Prepared, NodeStatus], Prepared]:
    """A prepared node, moved along the real transitions to a chosen status.

    The transitions are the point. A test that wrote `status = 'REVIEWING'`
    straight into the table would be reviewing a node that could not have got
    there, and would keep passing after the transition that used to lead there
    was removed.

    Where the node starts is read from the database rather than taken from the
    record the caller holds, so that a test can move a node a second time — a
    Master sending a refused node back to be reviewed again — without having to
    remember which copy of the record it kept.

    A route that enters RUNNING clears the node first, because that is what
    reaching RUNNING takes: the DAG refuses the transition otherwise, and a
    fixture that wrote the status anyway would hand the tests a node the system
    cannot produce. It is the same reason the routes are transitions rather than
    assignments.
    """

    def build(prepared: Prepared, status: NodeStatus) -> Prepared:
        with database.transaction() as session:
            dag = DagRepository(session, prepared.project_id)
            node = dag.node(prepared.node_id)
            route = _ROUTES.get((node.status, status))
            if route is None:
                raise AssertionError(
                    f"no route from {node.status.value} to {status.value}; add one "
                    "to _ROUTES rather than writing the status directly"
                )
            if NodeStatus.RUNNING in route and requires_frozen_criteria(node.node_type):
                _clear_for_running(session, prepared, node)
            for step in route:
                node = dag.transition_node(node.node_id, step, actor_id="scheduler")
        return replace(prepared, node=node)

    return build


def _clear_for_running(session: Session, prepared: Prepared, node: DagNode) -> None:
    """Give a node the pre-flight PASS it owes, unless it already has one.

    The guard is what keeps this from being a fixture that approves things on
    the tests' behalf: a node that arrived cleared is left exactly as it was,
    and only the nodes these tests deliberately left unreviewed are cleared —
    and then only because the transition they are being driven through cannot
    happen without it.

    Submitted through `ReviewService` for the reason the shared fixture gives:
    the gate reads the review the service accepted, and a clearance written by
    hand could be one the service would have refused.
    """
    dag = DagRepository(session, prepared.project_id)
    if dag.latest_pre_run_outcome(node.node_id) is ReviewOutcome.PASS:
        return
    measured = prepared.acceptance if prepared.acceptance is not None else prepared.contract
    ReviewService(session, prepared.project_id).submit(
        ReviewRecord(
            project_id=prepared.project_id,
            node_id=node.node_id,
            checkpoint=ReviewCheckpoint.PRE_RUN,
            frozen_criteria_ref=measured.contract_id,
            frozen_criteria_version=measured.version,
            outcome=ReviewOutcome.PASS,
            diagnosis="The plan states what will be measured and how it will be run.",
        ),
        role=AgentRole.REVIEW,
    )


@pytest.fixture
def status_of(database: Database) -> Callable[[str, str], NodeStatus]:
    """A node's status, read back from the database rather than remembered."""

    def read(project_id: str, node_id: str) -> NodeStatus:
        with database.read_only() as session:
            return DagRepository(session, project_id).node(node_id).status

    return read


@pytest.fixture
def reviews_of(database: Database) -> Callable[..., list[ReviewRecord]]:
    """Every Review Record written about a node, oldest first.

    `checkpoint` narrows it to one moment in the node's life, which is not the
    same question as "what has been written about this node": a node driven to
    RUNNING was cleared on the way, so a test asserting that *this* verdict was
    written has to say which verdict it means. Asking for all of them and
    picking out the ones it wanted would be a test that keeps passing after the
    checkpoint it cares about stopped being written.
    """

    def read(
        project_id: str,
        node_id: str,
        checkpoint: ReviewCheckpoint | None = None,
    ) -> list[ReviewRecord]:
        with database.read_only() as session:
            written = ReviewRepository(session, project_id).for_node(node_id)
        if checkpoint is None:
            return written
        return [review for review in written if review.checkpoint is checkpoint]

    return read


@pytest.fixture
def submit(database: Database) -> Callable[..., SubmittedReview]:
    """Submit a review the way a Review Worker's session would.

    The role is supplied here rather than by each test because it is not what
    the test is varying: a test that wants to show a *non*-Review caller being
    refused calls the service itself, and one that changed this default would
    be testing the fixture.
    """

    def run(
        review: ReviewRecord,
        *,
        role: AgentRole = AgentRole.REVIEW,
        actor_id: str | None = None,
    ) -> SubmittedReview:
        with database.transaction() as session:
            return ReviewService(session, review.project_id).submit(
                review, role=role, actor_id=actor_id
            )

    return run


def verdict(
    prepared: Prepared,
    *,
    satisfied: bool = True,
    answers: Sequence[bool] | None = None,
    **overrides: Any,
) -> ReviewRecord:
    """A well-formed final verdict on a node's acceptance criteria.

    Every frozen criterion satisfied, so a test that changes one field and
    expects a refusal is changing the thing it names rather than something the
    helper happened to leave out — and a test that expects acceptance is
    supplying a review that would have been accepted anyway.

    `satisfied=False` inverts all of them at once, which is what a FAIL needs.
    `answers` answers them one by one, which is what a PARTIAL needs: the
    domain refuses an outcome that contradicts the results beside it, so both
    of those tests have to move the results as well as the outcome.
    """
    if prepared.acceptance is None:
        raise AssertionError(
            f"{prepared.node.display_id} has no acceptance criteria to be measured "
            "against; use `against_the_contract` for a node measured against its "
            "Execution Contract"
        )
    criteria = prepared.acceptance.criteria
    if answers is None:
        answers = (satisfied,) * len(criteria)
    fields: dict[str, Any] = {
        "project_id": prepared.project_id,
        "node_id": prepared.node_id,
        "checkpoint": ReviewCheckpoint.FINAL,
        "frozen_criteria_ref": prepared.acceptance.contract_id,
        "frozen_criteria_version": prepared.acceptance.version,
        "outcome": ReviewOutcome.PASS,
        "criterion_results": tuple(
            CriterionResult(criterion_id=criterion.criterion_id, satisfied=answer)
            for criterion, answer in zip(criteria, answers, strict=True)
        ),
        "diagnosis": "Conductivity rose by 21% across the series.",
    }
    fields.update(overrides)
    return ReviewRecord(**fields)



def against_the_contract(prepared: Prepared, **overrides: Any) -> ReviewRecord:
    """A verdict on a node measured against its Execution Contract.

    Such a node has no criteria to answer, so the well-formed review of one
    reports none — which is what makes the tests that hand it criterion results
    meaningful.
    """
    fields: dict[str, Any] = {
        "project_id": prepared.project_id,
        "node_id": prepared.node_id,
        "checkpoint": ReviewCheckpoint.FINAL,
        "frozen_criteria_ref": prepared.contract.contract_id,
        "frozen_criteria_version": prepared.contract.version,
        "outcome": ReviewOutcome.PASS,
        "diagnosis": "The survey covers the requested series.",
    }
    fields.update(overrides)
    return ReviewRecord(**fields)
