"""A fan-in fires on the rule it declared, and not before.

The unit suite already asserts the arithmetic of `is_join_satisfied`. What is
asserted here is that the arithmetic is what actually moves the DAG: a node
waits in PLANNED while its join is unmet, becomes READY the moment it is met,
and becomes BLOCKED — visibly, in the stream and in the table — when it can no
longer be met at all.

The distinction that matters most is THRESHOLD against ALL. A THRESHOLD join
that has two of the three results it asked for fires without waiting for the
third; an ALL join in the same position does not move. If those two behaved the
same way, the policy field would be decoration, and the rolling horizon would
be waiting on work nobody needed.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.integration.dag.conftest import STAGES

from ravel.domain.dag import DagNode
from ravel.domain.enums import Confidence, DecisionType, JoinPolicy, NodeStatus, NodeType
from ravel.domain.roles import AgentRole
from ravel.state.services.dag import DagMutationService, DecisionDraft

pytestmark = pytest.mark.integration

NodeFactory = Callable[..., DagNode]


def _draft() -> DecisionDraft:
    return DecisionDraft(
        decision_type=DecisionType.CREATE_NODE,
        rationale="The stage needs a fan-in to compare the series.",
        confidence=Confidence.MEDIUM,
    )


@pytest.fixture
def finish(
    service: DagMutationService,
    freeze_criteria: Callable[[str], str],
    execution_contract: Callable[[str], str],
    clear_to_run: Callable[[str], None],
) -> Callable[..., DagNode]:
    """Drive a node to a terminal status the way the runtime would.

    A node cannot start without its two contracts and a pre-flight PASS, so this
    supplies all three in the order a worker would meet them. `REVIEWING` is on
    the path to PASSED because a result is reviewed before it is accepted, not
    because the DAG requires it.
    """

    def drive(node_id: str, outcome: NodeStatus = NodeStatus.PASSED) -> DagNode:
        service.dag.bind_acceptance_contract(node_id, freeze_criteria(node_id))
        service.dag.bind_execution_contract(node_id, execution_contract(node_id))
        service.dag.transition_node(node_id, NodeStatus.READY, actor_id="scheduler")
        clear_to_run(node_id)
        service.dag.transition_node(node_id, NodeStatus.RUNNING, actor_id="compute-worker")
        if outcome is NodeStatus.PASSED:
            service.dag.transition_node(node_id, NodeStatus.REVIEWING, actor_id="compute-worker")
        return service.dag.transition_node(node_id, outcome, actor_id="review")

    return drive


@pytest.fixture
def plan(service: DagMutationService) -> Callable[..., list[DagNode]]:
    """Commit nodes to the current stage under one decision."""

    def commit(*nodes: DagNode) -> list[DagNode]:
        return list(
            service.expand_phase(
                STAGES[0], list(nodes), role=AgentRole.MASTER, decision=_draft()
            ).nodes
        )

    return commit


@pytest.fixture
def dependencies(plan: Callable[..., list[DagNode]], a_node: NodeFactory) -> list[DagNode]:
    """Three independent pieces of work for a fan-in to wait on."""
    return plan(
        *[
            a_node(NodeType.COMPUTATION, objective=f"Measure sample {index}.")
            for index in range(3)
        ]
    )


def _status(service: DagMutationService, node: DagNode) -> NodeStatus:
    return service.dag.node(node.node_id).status


# ── The gate: a join fires on its own rule ──────────────────────────────────


def test_a_threshold_join_does_not_fire_below_its_threshold(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies),
            join_policy=JoinPolicy.THRESHOLD,
            join_threshold=2,
        )
    )[0]
    finish(dependencies[0].node_id)

    moved = service.dag.refresh_readiness()

    assert service.dag.join_state(service.dag.node(comparison.node_id)) == "waiting"
    assert _status(service, comparison) is NodeStatus.PLANNED
    assert comparison.node_id not in [node.node_id for node in moved]


def test_a_threshold_join_fires_once_its_threshold_is_reached(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    """The second result is enough, and the third is not waited for.

    This is the whole difference between a THRESHOLD join and an ALL join: the
    work still outstanding is work the decision said it would not need.
    """
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies),
            join_policy=JoinPolicy.THRESHOLD,
            join_threshold=2,
        )
    )[0]
    finish(dependencies[0].node_id)
    finish(dependencies[1].node_id)

    moved = service.dag.refresh_readiness()

    assert _status(service, comparison) is NodeStatus.READY
    assert comparison.node_id in [node.node_id for node in moved]
    assert not service.dag.node(dependencies[2].node_id).succeeded, (
        "the join fired while the third result still does not exist"
    )


def test_an_all_join_waits_for_every_dependency(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    """The same two results, and a different answer, because the rule differs."""
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies), join_policy=JoinPolicy.ALL
        )
    )[0]
    finish(dependencies[0].node_id)
    finish(dependencies[1].node_id)

    service.dag.refresh_readiness()

    assert _status(service, comparison) is NodeStatus.PLANNED

    finish(dependencies[2].node_id)
    service.dag.refresh_readiness()

    assert _status(service, comparison) is NodeStatus.READY


def test_an_any_join_fires_on_the_first_result(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies), join_policy=JoinPolicy.ANY
        )
    )[0]
    finish(dependencies[1].node_id)

    service.dag.refresh_readiness()

    assert _status(service, comparison) is NodeStatus.READY


def test_a_satisfied_join_is_not_undone_by_a_later_failure(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    """A failed dependency is not a failed join when the rule allowed for it.

    An ANY join that has one result has what it asked for. Treating the later
    failure as a reason to un-ready the node would make the join a report about
    its dependencies rather than a decision about them.
    """
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies), join_policy=JoinPolicy.ANY
        )
    )[0]
    finish(dependencies[0].node_id)

    service.dag.refresh_readiness()
    assert _status(service, comparison) is NodeStatus.READY

    finish(dependencies[1].node_id, NodeStatus.FAILED)
    service.dag.refresh_readiness()

    assert _status(service, comparison) is NodeStatus.READY


# ── A join that can no longer fire says so ──────────────────────────────────


def test_a_join_that_can_no_longer_fire_is_blocked(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    """Waiting forever and waiting for something are told apart.

    With one dependency failed and one still unsettled, three results can no
    longer arrive; the node is BLOCKED so a reader sees a decision is needed
    rather than a stage that appears to be quietly working.
    """
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies),
            join_policy=JoinPolicy.THRESHOLD,
            join_threshold=3,
        )
    )[0]
    finish(dependencies[0].node_id, NodeStatus.FAILED)

    service.dag.refresh_readiness()

    assert service.dag.join_state(service.dag.node(comparison.node_id)) == "unsatisfiable"
    assert _status(service, comparison) is NodeStatus.BLOCKED


def test_a_blocked_node_is_re_promoted_once_its_fan_in_is_satisfied(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    """BLOCKED is a status, not a sentence.

    A node can be blocked for a reason that has nothing to do with its fan-in —
    an external dependency, a user pause. Once that is resolved and its join
    holds, the scheduler promotes it rather than leaving it behind a blocker
    that no longer applies.
    """
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies), join_policy=JoinPolicy.ALL
        )
    )[0]
    service.dag.transition_node(comparison.node_id, NodeStatus.BLOCKED, actor_id="scheduler")

    for dependency in dependencies:
        finish(dependency.node_id)
    moved = service.dag.refresh_readiness()

    assert comparison.node_id in [node.node_id for node in moved]
    assert _status(service, comparison) is NodeStatus.READY


# ── The cases with nothing to join ──────────────────────────────────────────


def test_a_node_with_no_fan_in_is_ready_immediately(
    service: DagMutationService, plan: Callable[..., list[DagNode]], a_node: NodeFactory
) -> None:
    """Otherwise nothing in the DAG could ever start."""
    standalone = plan(a_node(NodeType.COMPUTATION))[0]

    service.dag.refresh_readiness()

    assert _status(service, standalone) is NodeStatus.READY


def test_readiness_is_not_recomputed_for_a_node_that_already_moved(
    service: DagMutationService,
    plan: Callable[..., list[DagNode]],
    dependencies: list[DagNode],
    a_join_node: NodeFactory,
    finish: Callable[..., DagNode],
) -> None:
    """A scheduler pass is idempotent: promoting twice is not two promotions.

    The pass runs on every loop, so a node that is already READY or RUNNING must
    not be re-announced — a second NODE_READY in the stream would tell a reader
    the node had been re-planned.
    """
    comparison = plan(
        a_join_node(
            tuple(node.node_id for node in dependencies), join_policy=JoinPolicy.ALL
        )
    )[0]
    for dependency in dependencies:
        finish(dependency.node_id)

    first = service.dag.refresh_readiness()
    second = service.dag.refresh_readiness()

    assert comparison.node_id in [node.node_id for node in first]
    assert comparison.node_id not in [node.node_id for node in second]
