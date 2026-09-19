"""The transition table is the spec's "state transitions must be validated in code"."""

from __future__ import annotations

import pytest

from ravel.domain.enums import (
    TERMINAL_JOB_STATES,
    FailureClass,
    JobState,
    JoinPolicy,
    NodeStatus,
    NodeType,
    ProjectStatus,
)
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import (
    ACTIVE_NODE_STATUSES,
    JOB_TRANSITIONS,
    NODE_TRANSITIONS,
    PROJECT_TRANSITIONS,
    TERMINAL_NODE_STATUSES,
    TERMINAL_PROJECT_STATUSES,
    TransitionError,
    can_transition_job,
    can_transition_node,
    can_transition_project,
    executor_for,
    is_join_satisfied,
    requires_frozen_criteria,
)


def test_the_table_covers_every_status() -> None:
    """A status missing from the table would have no defined behavior."""
    assert set(NODE_TRANSITIONS) == set(NodeStatus)
    assert set(PROJECT_TRANSITIONS) == set(ProjectStatus)


def test_every_target_is_a_real_status() -> None:
    for source, targets in NODE_TRANSITIONS.items():
        for target in targets:
            assert isinstance(target, NodeStatus), f"{source} -> {target}"


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (NodeStatus.PLANNED, NodeStatus.READY),
        (NodeStatus.PLANNED, NodeStatus.BLOCKED),
        (NodeStatus.READY, NodeStatus.RUNNING),
        (NodeStatus.READY, NodeStatus.WAITING_DECISION),
        (NodeStatus.RUNNING, NodeStatus.WAITING_EXTERNAL),
        (NodeStatus.RUNNING, NodeStatus.REVIEWING),
        (NodeStatus.RUNNING, NodeStatus.PASSED),
        (NodeStatus.RUNNING, NodeStatus.FAILED),
        (NodeStatus.RUNNING, NodeStatus.PARTIAL),
        (NodeStatus.WAITING_EXTERNAL, NodeStatus.RUNNING),
        (NodeStatus.WAITING_DECISION, NodeStatus.READY),
        (NodeStatus.REVIEWING, NodeStatus.PASSED),
        (NodeStatus.REVIEWING, NodeStatus.PARTIAL),
        (NodeStatus.BLOCKED, NodeStatus.READY),
    ],
)
def test_legal_transitions_are_allowed(source: NodeStatus, target: NodeStatus) -> None:
    assert can_transition_node(source, target).allowed


@pytest.mark.parametrize(
    ("source", "target"),
    [
        # Skipping the queue: a node that never became READY cannot run.
        (NodeStatus.PLANNED, NodeStatus.RUNNING),
        # Skipping review: a result that was never reviewed cannot pass or fail.
        (NodeStatus.WAITING_EXTERNAL, NodeStatus.PASSED),
        (NodeStatus.WAITING_DECISION, NodeStatus.PASSED),
        # A blocked node is not silently done.
        (NodeStatus.BLOCKED, NodeStatus.PASSED),
        (NodeStatus.PLANNED, NodeStatus.PASSED),
        # Backwards: a reviewed node does not return to the queue.
        (NodeStatus.REVIEWING, NodeStatus.PLANNED),
    ],
)
def test_skipping_a_stage_is_refused(source: NodeStatus, target: NodeStatus) -> None:
    check = can_transition_node(source, target)
    assert not check.allowed
    assert "may not become" in check.reason or "terminal" in check.reason


@pytest.mark.parametrize("status", sorted(TERMINAL_NODE_STATUSES))
def test_terminal_statuses_are_a_one_way_door(status: NodeStatus) -> None:
    """This is what makes the acceptance-criteria freeze meaningful."""
    for target in NodeStatus:
        if target is status:
            continue
        check = can_transition_node(status, target)
        assert not check.allowed
        assert "terminal" in check.reason


def test_the_four_terminal_statuses_are_the_expected_ones() -> None:
    assert {
        NodeStatus.PASSED,
        NodeStatus.FAILED,
        NodeStatus.PARTIAL,
        NodeStatus.CANCELLED,
    } == TERMINAL_NODE_STATUSES


@pytest.mark.parametrize("status", list(NodeStatus))
def test_reasserting_the_current_status_is_allowed(status: NodeStatus) -> None:
    """A retried activity reporting the state it already reached must not fault."""
    assert can_transition_node(status, status).allowed


def test_active_statuses_occupy_a_worker() -> None:
    assert {
        NodeStatus.RUNNING,
        NodeStatus.WAITING_EXTERNAL,
        NodeStatus.REVIEWING,
    } == ACTIVE_NODE_STATUSES
    for status in ACTIVE_NODE_STATUSES:
        assert status not in TERMINAL_NODE_STATUSES


def test_a_denied_transition_raises_with_the_reason() -> None:
    with pytest.raises(TransitionError, match="terminal"):
        can_transition_node(NodeStatus.PASSED, NodeStatus.RUNNING).raise_if_denied()


def test_project_lifecycle_is_ordered() -> None:
    assert can_transition_project(ProjectStatus.CREATED, ProjectStatus.CONTRACT_DEFINED).allowed
    assert can_transition_project(
        ProjectStatus.CONTRACT_DEFINED, ProjectStatus.EXECUTING
    ).allowed
    assert can_transition_project(ProjectStatus.EXECUTING, ProjectStatus.COMPLETED).allowed


def test_a_project_cannot_execute_without_a_contract() -> None:
    check = can_transition_project(ProjectStatus.CREATED, ProjectStatus.EXECUTING)
    assert not check.allowed


def test_project_terminal_statuses_are_final() -> None:
    assert {
        ProjectStatus.COMPLETED,
        ProjectStatus.FAILED,
        ProjectStatus.INCONCLUSIVE,
        ProjectStatus.CANCELLED,
    } == TERMINAL_PROJECT_STATUSES


def test_a_project_may_end_inconclusively_from_executing_or_paused() -> None:
    """The fourth ending A20 names, and the only two statuses it can be reached from.

    A project concludes nothing while it is still executing or while it is
    paused mid-run; from CREATED or CONTRACT_DEFINED there is no work to have
    been inconclusive about, so the ending is not reachable from them.
    """
    assert can_transition_project(ProjectStatus.EXECUTING, ProjectStatus.INCONCLUSIVE).allowed
    assert can_transition_project(ProjectStatus.PAUSED, ProjectStatus.INCONCLUSIVE).allowed
    assert not can_transition_project(
        ProjectStatus.CREATED, ProjectStatus.INCONCLUSIVE
    ).allowed
    assert not can_transition_project(
        ProjectStatus.CONTRACT_DEFINED, ProjectStatus.INCONCLUSIVE
    ).allowed


def test_concluding_nothing_is_an_ending() -> None:
    """A project that concluded nothing does not later conclude something."""
    check = can_transition_project(ProjectStatus.INCONCLUSIVE, ProjectStatus.EXECUTING)
    assert not check.allowed
    assert "terminal" in check.reason


def test_pause_is_reversible_but_completion_is_not() -> None:
    assert can_transition_project(ProjectStatus.EXECUTING, ProjectStatus.PAUSED).allowed
    assert can_transition_project(ProjectStatus.PAUSED, ProjectStatus.EXECUTING).allowed
    assert not can_transition_project(ProjectStatus.COMPLETED, ProjectStatus.EXECUTING).allowed


# ── Executor separation ─────────────────────────────────────────────────────


def test_each_node_type_has_exactly_one_executor() -> None:
    assert executor_for(NodeType.RESEARCH) is AgentRole.RESEARCH
    assert executor_for(NodeType.COMPUTATION) is AgentRole.COMPUTE_WORKER
    assert executor_for(NodeType.EXPERIMENT) is AgentRole.EXPERIMENTAL_WORKER
    assert executor_for(NodeType.REVIEW) is AgentRole.REVIEW
    assert executor_for(NodeType.HYPOTHESIS) is AgentRole.MASTER
    assert executor_for(NodeType.DECISION) is AgentRole.MASTER


def test_master_does_not_execute_computation_or_experiment() -> None:
    """The role that decides what a result means must not produce it."""
    for node_type in (NodeType.COMPUTATION, NodeType.EXPERIMENT):
        assert executor_for(node_type) is not AgentRole.MASTER


def test_review_is_never_executed_by_the_role_being_reviewed() -> None:
    assert executor_for(NodeType.REVIEW) is AgentRole.REVIEW
    assert AgentRole.REVIEW is not AgentRole.MASTER


def test_only_computation_and_experiment_freeze_criteria() -> None:
    assert requires_frozen_criteria(NodeType.COMPUTATION)
    assert requires_frozen_criteria(NodeType.EXPERIMENT)
    assert not requires_frozen_criteria(NodeType.RESEARCH)
    assert not requires_frozen_criteria(NodeType.REVIEW)


# ── Joins ───────────────────────────────────────────────────────────────────


def test_all_join_needs_every_dependency() -> None:
    assert not is_join_satisfied(JoinPolicy.ALL, None, 3, 2)
    assert is_join_satisfied(JoinPolicy.ALL, None, 3, 3)


def test_any_join_needs_one() -> None:
    assert is_join_satisfied(JoinPolicy.ANY, None, 3, 1)
    assert not is_join_satisfied(JoinPolicy.ANY, None, 3, 0)


def test_threshold_join_fires_only_when_satisfied() -> None:
    assert not is_join_satisfied(JoinPolicy.THRESHOLD, 2, 3, 1)
    assert is_join_satisfied(JoinPolicy.THRESHOLD, 2, 3, 2)
    assert is_join_satisfied(JoinPolicy.THRESHOLD, 2, 3, 3)


def test_a_threshold_join_without_a_threshold_is_a_fault() -> None:
    """Treating it as ALL would silently change what the author asked for."""
    with pytest.raises(ValueError, match="requires join_threshold"):
        is_join_satisfied(JoinPolicy.THRESHOLD, None, 3, 2)


def test_an_unreachable_threshold_is_a_fault_not_an_eternal_wait() -> None:
    with pytest.raises(ValueError, match="exceeds"):
        is_join_satisfied(JoinPolicy.THRESHOLD, 4, 3, 3)


def test_a_node_with_no_dependencies_is_always_ready() -> None:
    assert is_join_satisfied(None, None, 0, 0)


# --------------------------------------------------------------------------
# The job life cycle
# --------------------------------------------------------------------------


def test_the_job_table_covers_every_state() -> None:
    """A state missing from the table would have no defined behavior."""
    assert set(JOB_TRANSITIONS) == set(JobState)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (JobState.SUBMITTED, JobState.RUNNING),
        (JobState.SUBMITTED, JobState.COMPLETED),
        (JobState.RUNNING, JobState.WAITING_EXTERNAL),
        (JobState.RUNNING, JobState.COMPLETED),
        (JobState.RUNNING, JobState.FAILED),
        (JobState.RUNNING, JobState.TIMED_OUT),
        (JobState.WAITING_EXTERNAL, JobState.RUNNING),
        (JobState.WAITING_EXTERNAL, JobState.COMPLETED),
        (JobState.WAITING_EXTERNAL, JobState.FAILED),
    ],
)
def test_a_legal_job_move_is_allowed(source: JobState, target: JobState) -> None:
    assert can_transition_job(source, target).allowed


@pytest.mark.parametrize("state", sorted(TERMINAL_JOB_STATES))
def test_a_job_that_ended_did_not_end_another_way(state: JobState) -> None:
    """The reason the retry policy can trust the state it reads.

    A job reported COMPLETED and later FAILED would leave the durable layer
    unable to decide whether the work was done, and the answer it picked would
    be whichever report arrived last.
    """
    for target in JobState:
        if target is state:
            continue
        check = can_transition_job(state, target)
        assert not check.allowed
        assert "contradicts a recorded ending" in check.reason


@pytest.mark.parametrize("state", list(JobState))
def test_reasserting_a_jobs_state_is_allowed(state: JobState) -> None:
    """A poll that reports the same state twice is the normal case."""
    assert can_transition_job(state, state).allowed


def test_a_running_job_cannot_go_back_to_submitted() -> None:
    """The backend does not un-accept work, so a report that says it did is a bug."""
    assert not can_transition_job(JobState.RUNNING, JobState.SUBMITTED).allowed


def test_the_terminal_job_states_are_the_expected_ones() -> None:
    assert {
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.CANCELLED,
        JobState.TIMED_OUT,
    } == TERMINAL_JOB_STATES


def test_a_failure_class_is_only_two_things() -> None:
    """`acceptance/MOCK_SCENARIOS.yaml` fixes this list, and there is no "unknown".

    A backend that cannot classify its failure reports none, and RAVEL reads
    that as non-retryable rather than inventing a third value for it.
    """
    assert {member.value for member in FailureClass} == {
        "INFRA_RETRYABLE",
        "NON_RETRYABLE",
    }
