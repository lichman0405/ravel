"""A failure changes the future, and leaves the past alone.

`acceptance/V0_ACCEPTANCE.md` A09 asks for both halves in one sentence: a FAIL
causes Master to record a decision and mutate the *future* DAG, and the node
that failed remains historically failed. They are two claims about different
things — a plan and a history — and the failure mode this file exists to catch
is answering the first by editing the second: rerunning the node, or replacing
it, or clearing its verdict, so that the DAG stops containing the result that
made replanning necessary.

The second thing asserted here is that *what got stranded* is computed from the
join policies rather than from the shape of the graph. A node behind an `ANY`
join with a sibling that passed is not waiting for the failed node and is not
retired; one behind an `ALL` join is. A walk that retired every descendant
would take work with it that the plan still allows to run, which is the quiet
version of the same bug: the project gets less done and nothing says why.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    Confidence,
    DecisionType,
    FailurePolicy,
    JoinPolicy,
    NodeStatus,
    NodeType,
)
from ravel.domain.roles import AgentRole
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.records import DecisionRepository
from ravel.state.services.dag import DagMutationService, DecisionDraft

pytestmark = pytest.mark.integration

NodeFactory = Callable[..., DagNode]
Finish = Callable[..., DagNode]
Plan = Callable[..., list[DagNode]]


def _replacement() -> DecisionDraft:
    return DecisionDraft(
        decision_type=DecisionType.REPLACE_NODE,
        rationale="The measurement failed; the analysis reads a simulation instead.",
        confidence=Confidence.MEDIUM,
    )


def _status(service: DagMutationService, node_id: str) -> NodeStatus:
    return service.dag.node(node_id).status


@pytest.fixture
def chain(
    plan: Plan, a_node: NodeFactory, a_join_node: NodeFactory
) -> tuple[DagNode, DagNode, DagNode]:
    """`measurement -> analysis -> conclusion`, each waiting on the one before.

    Three deep on purpose. Two would not distinguish a walk that computes what
    is stranded from one that simply retires every descendant: with one step
    between the failure and the end of the graph, both give the same answer.
    """
    measurement = plan(a_node(NodeType.COMPUTATION, objective="Measure sample 1."))[0]
    analysis = plan(
        a_join_node((measurement.node_id,), objective="Fit the series.")
    )[0]
    conclusion = plan(
        a_join_node((analysis.node_id,), objective="Decide whether to continue.")
    )[0]
    return measurement, analysis, conclusion


def test_a_failure_retires_every_descendant_that_can_no_longer_run(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    measurement, analysis, conclusion = chain
    finish(measurement.node_id, NodeStatus.FAILED)

    replanned = service.replan_after_failure(
        measurement.node_id,
        [a_node(NodeType.COMPUTATION, objective="Simulate sample 1 instead.")],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    assert set(replanned.retired) == {analysis.node_id, conclusion.node_id}
    assert _status(service, analysis.node_id) is NodeStatus.CANCELLED
    assert _status(service, conclusion.node_id) is NodeStatus.CANCELLED


def test_the_node_that_failed_stays_failed(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    """A09's other half, and the one a tempting implementation gets wrong.

    Replacing the failed node rather than adding beside it would leave a DAG
    that no longer contains the failure — and every later reader, including the
    final audit, would find a project whose plan went straight from intent to
    result with nothing recording that the first attempt did not work.
    """
    measurement, _, _ = chain
    before = service.dag.node(measurement.node_id)
    finish(measurement.node_id, NodeStatus.FAILED)

    service.replan_after_failure(
        measurement.node_id,
        [a_node(NodeType.COMPUTATION, objective="Simulate sample 1 instead.")],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    after = service.dag.node(measurement.node_id)
    assert after.status is NodeStatus.FAILED
    assert after.completed_at is not None
    assert after.decision_ref == before.decision_ref, (
        "the failed node still belongs to the decision that created it, not to "
        "the one that answered its failure"
    )


def test_the_replacement_is_recorded_as_created_and_the_stranded_as_cancelled(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    measurement, analysis, conclusion = chain
    finish(measurement.node_id, NodeStatus.FAILED)
    replacement = a_node(NodeType.COMPUTATION, objective="Simulate sample 1 instead.")

    replanned = service.replan_after_failure(
        measurement.node_id,
        [replacement],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    affected = DecisionRepository(service.session, service.project_id).get(
        decision_id=replanned.decision.decision_id
    ).affected_nodes
    assert affected.created == (replanned.created[0].node_id,)
    assert set(affected.cancelled) == {analysis.node_id, conclusion.node_id}
    assert affected.modified == (), (
        "the failed node was not modified; a reader looking for what changed "
        "about it should find nothing, because nothing did"
    )


# ── What counts as stranded ─────────────────────────────────────────────────


def test_work_behind_an_any_join_is_left_alone(
    service: DagMutationService,
    plan: Plan,
    a_node: NodeFactory,
    a_join_node: NodeFactory,
    finish: Finish,
) -> None:
    """Two measurements, either of which will do, and one of them failed.

    The comparison can still be made, so it is not stranded — and cancelling it
    would throw away work the plan permits. This is the test that separates
    "descendants of a failure" from "work the failure stranded".
    """
    first, second = plan(
        a_node(NodeType.COMPUTATION, objective="Measure sample 1."),
        a_node(NodeType.COMPUTATION, objective="Measure sample 2."),
    )
    comparison = plan(
        a_join_node(
            (first.node_id, second.node_id),
            objective="Compare whichever measurement arrived.",
            join_policy=JoinPolicy.ANY,
        )
    )[0]
    finish(first.node_id, NodeStatus.FAILED)

    replanned = service.replan_after_failure(
        first.node_id,
        [a_node(NodeType.COMPUTATION, objective="Measure sample 1 again.")],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    assert replanned.retired == ()
    assert _status(service, comparison.node_id) is NodeStatus.PLANNED


def test_a_failure_tolerant_descendant_is_left_alone(
    service: DagMutationService,
    plan: Plan,
    a_node: NodeFactory,
    a_join_node: NodeFactory,
    finish: Finish,
) -> None:
    """The third reason a descendant survives, beside `ANY` and having finished.

    A node that declared `CONTINUE` is not waiting for its dependency to
    succeed; it is waiting for it to *settle*, and it has. Retiring it would
    cancel exactly the branch the plan was written to keep: the one that reads
    the failure and decides what to do about it.
    """
    measurement = plan(a_node(NodeType.COMPUTATION, objective="Measure sample 1."))[0]
    post_mortem = plan(
        a_join_node(
            (measurement.node_id,),
            objective="Decide what the failed measurement means.",
            join_policy=JoinPolicy.ALL,
            failure_policy=FailurePolicy.CONTINUE,
        )
    )[0]
    finish(measurement.node_id, NodeStatus.FAILED)

    replanned = service.replan_after_failure(
        measurement.node_id,
        [a_node(NodeType.COMPUTATION, objective="Simulate sample 1 instead.")],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    assert replanned.retired == ()
    assert _status(service, post_mortem.node_id) is NodeStatus.PLANNED


def test_a_descendant_is_retired_for_being_stranded_by_a_retirement(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    """The second layer, which only a live walk finds.

    The conclusion does not depend on the failed measurement at all — it
    depends on the analysis, which is still PLANNED and therefore "still
    running" to any reading taken before the retirement. It becomes stranded
    only because the analysis is being cancelled, so a set computed up front
    from the statuses in the table would leave it in the plan, waiting for work
    that is gone.
    """
    measurement, _analysis, conclusion = chain
    finish(measurement.node_id, NodeStatus.FAILED)
    assert service.dag.join_state(service.dag.node(conclusion.node_id)) == "waiting", (
        "the premise of this test: nothing about the conclusion looks stranded "
        "until its own dependency has been retired"
    )

    replanned = service.replan_after_failure(
        measurement.node_id,
        [a_node(NodeType.COMPUTATION, objective="Simulate sample 1 instead.")],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    assert conclusion.node_id in replanned.retired


def test_work_that_already_finished_is_not_retired(
    service: DagMutationService,
    plan: Plan,
    a_node: NodeFactory,
    a_join_node: NodeFactory,
    finish: Finish,
) -> None:
    """A terminal descendant has had its ending recorded and is not retired.

    A node that already PASSED is not waiting for anything; one that already
    FAILED has its own decision to answer. Cancelling either would overwrite a
    result with a plan change.
    """
    measurement = plan(a_node(NodeType.COMPUTATION, objective="Measure sample 1."))[0]
    analysis = plan(a_join_node((measurement.node_id,), objective="Fit the series."))[0]
    finish(analysis.node_id, NodeStatus.PASSED)
    finish(measurement.node_id, NodeStatus.FAILED)

    replanned = service.replan_after_failure(
        measurement.node_id,
        [a_node(NodeType.COMPUTATION, objective="Simulate sample 1 instead.")],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    assert replanned.retired == ()
    assert _status(service, analysis.node_id) is NodeStatus.PASSED


def test_a_replacement_may_depend_on_the_node_that_failed(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    """What the failed node produced is usually why the next plan is different.

    A second attempt that reads the failure, or an analysis under a policy that
    tolerates it, is legitimate. The prohibition is on depending on work that is
    being *cancelled* — that is a node waiting for something that will never
    run.

    The replacement declares `CONTINUE`, and that is not decoration: with any
    other failure policy the node would be created already stranded, since a
    fan-in over a branch that failed can never be satisfied. The two halves of
    this belong together — replanning permits the edge, and the failure policy
    is what makes the edge runnable.
    """
    measurement, _, _ = chain
    finish(measurement.node_id, NodeStatus.FAILED)

    replanned = service.replan_after_failure(
        measurement.node_id,
        [
            a_node(
                NodeType.COMPUTATION,
                objective="Analyse why the measurement failed.",
                dependencies=(measurement.node_id,),
                join_policy=JoinPolicy.ALL,
                failure_policy=FailurePolicy.CONTINUE,
            )
        ],
        role=AgentRole.MASTER,
        decision=_replacement(),
    )

    assert replanned.created[0].dependencies == (measurement.node_id,)
    assert service.dag.join_state(replanned.created[0]) == "satisfied", (
        "a replacement that reads the failure has to be able to run; an edge to "
        "a failed node under a policy that does not tolerate failure would make "
        "the replacement unrunnable the moment it was created"
    )


def test_a_replacement_may_not_depend_on_work_being_retired(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    measurement, analysis, _ = chain
    finish(measurement.node_id, NodeStatus.FAILED)

    with pytest.raises(ValueError, match="which this replanning is retiring"):
        service.replan_after_failure(
            measurement.node_id,
            [
                a_node(
                    NodeType.COMPUTATION,
                    objective="Analyse the abandoned plan.",
                    dependencies=(analysis.node_id,),
                    join_policy=JoinPolicy.ALL,
                )
            ],
            role=AgentRole.MASTER,
            decision=_replacement(),
        )

    assert _status(service, analysis.node_id) is NodeStatus.PLANNED, (
        "the refusal has to happen before anything is written"
    )


# ── Refusals ────────────────────────────────────────────────────────────────


def test_a_node_that_did_not_fail_cannot_be_replanned(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    a_node: NodeFactory,
) -> None:
    measurement, _, _ = chain

    with pytest.raises(ValueError, match="not FAILED"):
        service.replan_after_failure(
            measurement.node_id,
            [a_node(NodeType.COMPUTATION, objective="Something else.")],
            role=AgentRole.MASTER,
            decision=_replacement(),
        )


def test_replanning_records_a_replacement_decision(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    """The type is checked because it is how a reader finds this decision."""
    measurement, _, _ = chain
    finish(measurement.node_id, NodeStatus.FAILED)

    with pytest.raises(ValueError, match="REPLACE_NODE"):
        service.replan_after_failure(
            measurement.node_id,
            [a_node(NodeType.COMPUTATION, objective="Something else.")],
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=DecisionType.CREATE_NODE,
                rationale="A new node, apparently.",
            ),
        )


def test_replanning_without_a_replacement_is_refused(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
) -> None:
    measurement, _, _ = chain
    finish(measurement.node_id, NodeStatus.FAILED)

    with pytest.raises(ValueError, match="commits no nodes"):
        service.replan_after_failure(
            measurement.node_id,
            [],
            role=AgentRole.MASTER,
            decision=_replacement(),
        )


def test_an_unknown_node_cannot_be_replanned(
    service: DagMutationService, a_node: NodeFactory
) -> None:
    with pytest.raises(NotFound):
        service.replan_after_failure(
            "node-that-does-not-exist",
            [a_node(NodeType.COMPUTATION, objective="Something else.")],
            role=AgentRole.MASTER,
            decision=_replacement(),
        )


def test_only_master_may_replan(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    measurement, _, _ = chain
    finish(measurement.node_id, NodeStatus.FAILED)

    with pytest.raises(PermissionError, match="may not mutate the DAG"):
        service.replan_after_failure(
            measurement.node_id,
            [a_node(NodeType.COMPUTATION, objective="Something else.")],
            role=AgentRole.COMPUTE_WORKER,
            decision=_replacement(),
        )


def test_a_refused_replan_leaves_no_decision_behind(
    service: DagMutationService,
    chain: tuple[DagNode, DagNode, DagNode],
    finish: Finish,
    a_node: NodeFactory,
) -> None:
    """The write order, asserted where it is observable.

    The decision is written before the change, so a change that is refused must
    leave no decision — otherwise the record that exists to make DAG changes
    attributable would contain one describing a change that never happened.
    """
    measurement, analysis, _ = chain
    decisions = DecisionRepository(service.session, service.project_id)
    before = len(decisions.all())
    finish(measurement.node_id, NodeStatus.FAILED)

    with pytest.raises(ValueError, match="which this replanning is retiring"):
        service.replan_after_failure(
            measurement.node_id,
            [
                a_node(
                    NodeType.COMPUTATION,
                    objective="Analyse the abandoned plan.",
                    dependencies=(analysis.node_id,),
                    join_policy=JoinPolicy.ALL,
                )
            ],
            role=AgentRole.MASTER,
            decision=_replacement(),
        )

    assert len(decisions.all()) == before, (
        "a refused replan writes no decision; the only ones present are the "
        "ones that created the nodes"
    )
