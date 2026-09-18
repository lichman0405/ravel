"""DAG nodes carry the invariants that keep the research plan honest."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ravel.domain.dag import DagEdge, DagNode
from ravel.domain.enums import FailurePolicy, JoinPolicy, NodeStatus, NodeType
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TransitionError


def _node(**overrides: object) -> DagNode:
    defaults: dict[str, object] = {
        "project_id": "proj-a",
        "node_type": NodeType.COMPUTATION,
        "objective": "Measure the inhibition constant.",
        "created_by": "master",
    }
    defaults.update(overrides)
    return DagNode.create(**defaults)  # type: ignore[arg-type]


def test_create_derives_the_executor() -> None:
    node = _node()
    assert node.executor_role is AgentRole.COMPUTE_WORKER
    assert node.status is NodeStatus.PLANNED


def test_create_gives_a_readable_display_id() -> None:
    assert _node().display_id.startswith("COMP-")
    assert _node(node_type=NodeType.RESEARCH).display_id.startswith("RES-")
    assert _node(node_type=NodeType.EXPERIMENT).display_id.startswith("EXP-")


def test_node_id_is_opaque_and_stable() -> None:
    from ravel.domain.ids import is_opaque_id

    node = _node()
    assert is_opaque_id(node.node_id)
    assert node.node_id not in node.display_id


def test_a_wrong_executor_is_refused() -> None:
    """Master cannot execute the computation it will later judge."""
    with pytest.raises(ValidationError, match="is executed by compute-worker"):
        _node(executor_role=AgentRole.MASTER)


def test_dependencies_require_a_join_policy() -> None:
    with pytest.raises(ValidationError, match="no join_policy"):
        _node(dependencies=("n1", "n2"))


def test_a_join_policy_without_dependencies_is_refused() -> None:
    with pytest.raises(ValidationError, match="no dependencies"):
        _node(join_policy=JoinPolicy.ALL)


def test_threshold_join_needs_a_threshold() -> None:
    with pytest.raises(ValidationError, match="needs join_threshold"):
        _node(dependencies=("n1", "n2"), join_policy=JoinPolicy.THRESHOLD)


def test_threshold_join_cannot_exceed_its_dependencies() -> None:
    with pytest.raises(ValidationError, match="could never fire"):
        _node(dependencies=("n1", "n2"), join_policy=JoinPolicy.THRESHOLD, join_threshold=3)


def test_threshold_is_refused_outside_a_threshold_join() -> None:
    with pytest.raises(ValidationError, match="without a THRESHOLD policy"):
        _node(dependencies=("n1",), join_policy=JoinPolicy.ALL, join_threshold=1)


def test_a_valid_threshold_join_is_accepted() -> None:
    node = _node(
        dependencies=("n1", "n2", "n3"),
        join_policy=JoinPolicy.THRESHOLD,
        join_threshold=2,
        failure_policy=FailurePolicy.CONTINUE,
    )
    assert node.join_threshold == 2


def test_a_node_cannot_depend_on_itself() -> None:
    node_id = "n-self"
    with pytest.raises(ValidationError, match="lists itself"):
        DagNode(
            node_id=node_id,
            display_id="COMP-0001",
            project_id="proj-a",
            node_type=NodeType.COMPUTATION,
            objective="x",
            executor_role=AgentRole.COMPUTE_WORKER,
            dependencies=(node_id,),
            join_policy=JoinPolicy.ALL,
            created_by="master",
        )


def test_a_self_edge_is_refused() -> None:
    with pytest.raises(ValidationError, match="cannot depend on itself"):
        DagEdge(project_id="proj-a", from_node="n1", to_node="n1")


def test_transition_returns_a_new_node_and_leaves_the_original_alone() -> None:
    node = _node()
    running = node.transition(NodeStatus.READY).transition(NodeStatus.RUNNING)
    assert running.status is NodeStatus.RUNNING
    assert node.status is NodeStatus.PLANNED
    assert running.started_at is not None
    assert node.started_at is None


def test_a_terminal_transition_stamps_completion() -> None:
    node = _node().transition(NodeStatus.READY).transition(NodeStatus.RUNNING)
    passed = node.transition(NodeStatus.PASSED)
    assert passed.completed_at is not None


def test_an_illegal_transition_raises() -> None:
    with pytest.raises(TransitionError):
        _node().transition(NodeStatus.PASSED)


def test_records_are_frozen() -> None:
    node = _node()
    with pytest.raises(ValidationError):
        node.status = NodeStatus.PASSED  # type: ignore[misc]


def test_unknown_fields_are_refused() -> None:
    """A typo in a tool call must not become a silently ignored field."""
    with pytest.raises(ValidationError):
        DagNode(
            display_id="COMP-1",
            project_id="proj-a",
            node_type=NodeType.COMPUTATION,
            objective="x",
            executor_role=AgentRole.COMPUTE_WORKER,
            created_by="master",
            priority="high",  # type: ignore[call-arg]
        )


# ── Starting work ───────────────────────────────────────────────────────────


def _ready() -> DagNode:
    return _node().transition(NodeStatus.READY)


def test_computation_cannot_run_without_frozen_criteria() -> None:
    check = _ready().can_enter_running(has_frozen_acceptance=False, has_execution_contract=True)
    assert not check.allowed
    assert "criteria are frozen" in check.reason


def test_no_node_runs_without_an_execution_contract() -> None:
    """A worker with no contract has no authority to act at all."""
    check = _ready().can_enter_running(has_frozen_acceptance=True, has_execution_contract=False)
    assert not check.allowed
    assert "no Execution Contract" in check.reason


def test_a_fully_prepared_computation_may_run() -> None:
    check = _ready().can_enter_running(has_frozen_acceptance=True, has_execution_contract=True)
    assert check.allowed


def test_research_does_not_need_frozen_criteria() -> None:
    node = _node(node_type=NodeType.RESEARCH).transition(NodeStatus.READY)
    assert node.can_enter_running(has_frozen_acceptance=False, has_execution_contract=True).allowed


def test_requires_frozen_criteria_matches_the_node_type() -> None:
    assert _node(node_type=NodeType.EXPERIMENT).requires_frozen_criteria
    assert not _node(node_type=NodeType.RESEARCH).requires_frozen_criteria


def test_success_counts_pass_and_partial() -> None:
    """A join asks whether enough was delivered, not whether it was good."""
    node = _node()
    assert not node.succeeded
    assert node.transition(NodeStatus.READY).transition(NodeStatus.RUNNING).transition(
        NodeStatus.PARTIAL
    ).succeeded
    assert _ready().transition(NodeStatus.RUNNING).transition(NodeStatus.FAILED).succeeded is False


def test_is_terminal_tracks_the_transition_table() -> None:
    assert _ready().transition(NodeStatus.RUNNING).transition(NodeStatus.PASSED).is_terminal
    assert not _ready().is_terminal
