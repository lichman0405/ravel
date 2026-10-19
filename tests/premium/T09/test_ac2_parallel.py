"""T09-AC2: quota-bounded parallel submission and a join over only its inputs.

The coordinator is driven against the real backend and the scripted target
cluster. No scheduler is contacted; `REAL_HPC` is false in `support`.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from tests.premium.T09 import support
from tests.premium.T09.support import ScriptedTargetCluster

from ravel.backends.slurm import parallel_quota
from ravel.config import Settings
from ravel.domain.dag import DagNode
from ravel.domain.enums import JobState, NodeStatus, NodeType
from ravel.execution.backends import (
    DispatchState,
    JobHandle,
    JoinTimeoutError,
    ParallelSubmissionCoordinator,
    ReadyNode,
    join_required,
)
from ravel.execution.loop import ProjectLoop, Situation


def _candidate(
    node_id: str,
    workspace,
    *,
    status: NodeStatus = NodeStatus.READY,
    dependencies: tuple[str, ...] = (),
    review_allowed: bool = True,
    budget_allowed: bool = True,
) -> ReadyNode:
    return ReadyNode(
        node_id=node_id,
        request=support.request(node_id, workspace=workspace),
        status=status,
        dependencies=dependencies,
        review_allowed=review_allowed,
        budget_allowed=budget_allowed,
    )


def _job_id(handle: JobHandle) -> str:
    return handle.backend_job_ref.split(":")[2]


def _finish(cluster: ScriptedTargetCluster, handle: JobHandle) -> None:
    cluster.jobs[_job_id(handle)]["state"] = "COMPLETED"


def test_quota_two_submits_two_and_queues_the_third(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    candidates = [
        _candidate(node, support.workspace(tmp_path, node_id=node))
        for node in (support.NODE, support.OTHER_NODE, "node-0003")
    ]
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)

    results = coordinator.dispatch(candidates)

    assert [result.state for result in results] == [
        DispatchState.SUBMITTED,
        DispatchState.SUBMITTED,
        DispatchState.QUEUED,
    ]
    assert [result.node_id for result in results] == [
        support.NODE,
        support.OTHER_NODE,
        "node-0003",
    ]
    assert len(cluster.jobs) == 2
    assert cluster.sbatch_calls() == 2
    assert coordinator.in_flight == 2
    assert coordinator.available == 0
    assert "quota 2 is fully in flight" in results[2].detail


def test_a_completion_and_release_admits_the_queued_third(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    first = _candidate(support.NODE, support.workspace(tmp_path, node_id=support.NODE))
    second = _candidate(
        support.OTHER_NODE, support.workspace(tmp_path, node_id=support.OTHER_NODE)
    )
    third = _candidate("node-0003", support.workspace(tmp_path, node_id="node-0003"))
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)
    coordinator.dispatch([first, second])

    handle = coordinator.handles[support.NODE]
    _finish(cluster, handle)
    coordinator.release(support.NODE)
    results = coordinator.dispatch([third])

    assert results[0].state is DispatchState.SUBMITTED
    assert len(cluster.jobs) == 3
    assert cluster.sbatch_calls() == 3
    assert set(coordinator.handles) == {support.OTHER_NODE, "node-0003"}


def test_quota_one_runs_them_serially(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    nodes = (support.NODE, support.OTHER_NODE, "node-0003")
    candidates = [
        _candidate(node, support.workspace(tmp_path, node_id=node)) for node in nodes
    ]
    coordinator = ParallelSubmissionCoordinator(backend, quota=1)

    first_pass = coordinator.dispatch(candidates)
    assert [result.state for result in first_pass] == [
        DispatchState.SUBMITTED,
        DispatchState.QUEUED,
        DispatchState.QUEUED,
    ]
    assert len(cluster.jobs) == 1
    assert cluster.sbatch_calls() == 1

    _finish(cluster, coordinator.handles[nodes[0]])
    coordinator.release(nodes[0])
    second_pass = coordinator.dispatch(candidates[1:])
    assert [result.state for result in second_pass] == [
        DispatchState.SUBMITTED,
        DispatchState.QUEUED,
    ]
    assert len(cluster.jobs) == 2
    assert cluster.sbatch_calls() == 2

    _finish(cluster, coordinator.handles[nodes[1]])
    coordinator.release(nodes[1])
    third_pass = coordinator.dispatch(candidates[2:])
    assert third_pass[0].state is DispatchState.SUBMITTED
    assert len(cluster.jobs) == 3
    assert cluster.sbatch_calls() == 3


def test_the_second_pass_does_not_resubmit_an_already_submitted_node(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    candidate = _candidate(
        support.NODE, support.workspace(tmp_path, node_id=support.NODE)
    )
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)

    first = coordinator.dispatch([candidate])[0]
    second = coordinator.dispatch([candidate])[0]

    assert first.state is DispatchState.SUBMITTED
    assert second.state is DispatchState.SUBMITTED
    assert second.handle == first.handle
    assert "already submitted" in second.detail
    assert cluster.sbatch_calls() == 1
    assert len(cluster.jobs) == 1


def test_not_ready_candidates_do_not_consume_a_slot(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)
    candidates = [
        _candidate(
            support.NODE,
            support.workspace(tmp_path, node_id=support.NODE),
            status=NodeStatus.RUNNING,
        ),
        _candidate(
            support.OTHER_NODE,
            support.workspace(tmp_path, node_id=support.OTHER_NODE),
            dependencies=("node-0001",),
        ),
        _candidate(
            "node-0003",
            support.workspace(tmp_path, node_id="node-0003"),
            review_allowed=False,
        ),
        _candidate(
            "node-0004",
            support.workspace(tmp_path, node_id="node-0004"),
            budget_allowed=False,
        ),
    ]

    results = coordinator.dispatch(candidates, completed=())

    assert [result.state for result in results] == [DispatchState.NOT_READY] * 4
    assert "not READY" in results[0].detail
    assert "dependencies not complete: node-0001" in results[1].detail
    assert "Review has not allowed" in results[2].detail
    assert "compute budget" in results[3].detail
    assert cluster.jobs == {}
    assert cluster.sbatch_calls() == 0
    assert coordinator.in_flight == 0
    assert coordinator.available == 2


def test_a_node_with_met_dependencies_is_submitted(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    candidate = _candidate(
        support.OTHER_NODE,
        support.workspace(tmp_path, node_id=support.OTHER_NODE),
        dependencies=("node-0001",),
    )
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)

    result = coordinator.dispatch([candidate], completed=(support.NODE,))[0]

    assert result.state is DispatchState.SUBMITTED
    assert result.handle is not None
    assert len(cluster.jobs) == 1


def test_an_unknown_submission_holds_its_slot_until_reconciled(tmp_path) -> None:
    cluster = ScriptedTargetCluster(failures=["lost_no_job"])
    backend = support.backend(cluster)
    unsafe = _candidate(
        support.NODE, support.workspace(tmp_path, node_id=support.NODE)
    )
    waiting = _candidate(
        support.OTHER_NODE, support.workspace(tmp_path, node_id=support.OTHER_NODE)
    )
    coordinator = ParallelSubmissionCoordinator(backend, quota=1)

    first_pass = coordinator.dispatch([unsafe, waiting])

    assert first_pass[0].state is DispatchState.UNKNOWN
    assert first_pass[0].detail
    assert first_pass[1].state is DispatchState.QUEUED
    assert coordinator.in_flight == 1
    assert coordinator.available == 0
    assert cluster.jobs == {}

    # Only an observation may release the slot. A confirmed absence (both
    # probes said no job exists) is such an observation.
    coordinator.resolve_uncertain(support.NODE)
    assert coordinator.in_flight == 0
    second_pass = coordinator.dispatch([waiting])
    assert second_pass[0].state is DispatchState.SUBMITTED
    assert len(cluster.jobs) == 1


def test_resolving_to_a_job_id_turns_the_slot_into_a_handle(tmp_path) -> None:
    cluster = ScriptedTargetCluster(failures=["lost_no_job"])
    backend = support.backend(cluster)
    unsafe = _candidate(
        support.NODE, support.workspace(tmp_path, node_id=support.NODE)
    )
    other = _candidate(
        support.OTHER_NODE, support.workspace(tmp_path, node_id=support.OTHER_NODE)
    )
    queued = _candidate("node-0003", support.workspace(tmp_path, node_id="node-0003"))
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)
    coordinator.dispatch([unsafe, other])

    coordinator.resolve_uncertain(support.NODE, job_id="9999")

    handle = coordinator.handles[support.NODE]
    assert handle.backend_job_ref == "9999"
    assert handle.state is JobState.SUBMITTED
    assert handle.backend_state == "RECONCILED"
    assert support.NODE not in coordinator.uncertain
    assert coordinator.in_flight == 2
    assert coordinator.dispatch([queued])[0].state is DispatchState.QUEUED
    with pytest.raises(KeyError):
        coordinator.resolve_uncertain("node-0003")


def test_a_refusal_does_not_consume_a_slot(tmp_path) -> None:
    cluster = ScriptedTargetCluster(sbatch_rejection="partition")
    backend = support.backend(cluster)
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)
    candidates = [
        _candidate(node, support.workspace(tmp_path, node_id=node))
        for node in (support.NODE, support.OTHER_NODE)
    ]

    results = coordinator.dispatch(candidates)

    assert [result.state for result in results] == [
        DispatchState.REFUSED,
        DispatchState.REFUSED,
    ]
    assert "PARTITION_REJECTED" in results[0].detail
    assert cluster.jobs == {}
    assert coordinator.in_flight == 0
    assert coordinator.available == 2


def test_a_ready_node_whose_request_names_another_node_is_refused(tmp_path) -> None:
    backend = support.backend(ScriptedTargetCluster())
    candidate = ReadyNode(
        node_id=support.NODE,
        request=support.request(
            support.OTHER_NODE,
            workspace=support.workspace(tmp_path, node_id=support.OTHER_NODE),
        ),
    )
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)

    with pytest.raises(ValueError, match="wrong node id"):
        coordinator.dispatch([candidate])


def test_quota_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        ParallelSubmissionCoordinator(
            support.backend(ScriptedTargetCluster()), quota=0
        )


def test_parallel_quota_is_t06_s_allowance_bounded_by_the_target() -> None:
    settings = Settings()

    assert parallel_quota(settings.action_compute_per_project) == (
        settings.action_compute_per_project
    )
    assert parallel_quota(settings.action_compute_per_project, 1) == 1
    assert parallel_quota(4, 2) == 2
    assert parallel_quota(3, None) == 3
    with pytest.raises(ValueError):
        parallel_quota(0)
    with pytest.raises(ValueError):
        parallel_quota(2, 0)


def test_join_waits_only_for_the_required_jobs(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)
    candidates = [
        _candidate(node, support.workspace(tmp_path, node_id=node))
        for node in (support.NODE, support.OTHER_NODE)
    ]
    coordinator.dispatch(candidates)
    required_handle = coordinator.handles[support.NODE]
    unrelated_handle = coordinator.handles[support.OTHER_NODE]
    _finish(cluster, required_handle)
    cluster.jobs[_job_id(unrelated_handle)]["state"] = "RUNNING"
    mark = len(cluster.commands)

    outcome = join_required(
        backend,
        coordinator.handles,
        [support.NODE],
        deadline_seconds=0.0,
        sleep=lambda _seconds: None,
    )

    assert outcome.required == (support.NODE,)
    assert outcome.completed
    assert outcome.polls == 1
    asked = cluster.commands_since(mark)
    assert any(_job_id(required_handle) in command for command in asked)
    assert all(_job_id(unrelated_handle) not in command for command in asked)


def test_join_reports_every_required_status(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    coordinator = ParallelSubmissionCoordinator(backend, quota=2)
    candidates = [
        _candidate(node, support.workspace(tmp_path, node_id=node))
        for node in (support.NODE, support.OTHER_NODE)
    ]
    coordinator.dispatch(candidates)
    for handle in coordinator.handles.values():
        _finish(cluster, handle)

    outcome = join_required(
        backend,
        coordinator.handles,
        deadline_seconds=0.0,
        sleep=lambda _seconds: None,
    )

    assert outcome.ended
    assert outcome.completed
    assert set(outcome.statuses) == {support.NODE, support.OTHER_NODE}
    assert set(outcome.required) == {support.NODE, support.OTHER_NODE}


def test_join_times_out_when_a_required_job_has_not_ended(tmp_path) -> None:
    cluster = ScriptedTargetCluster()
    backend = support.backend(cluster)
    request = support.request(workspace=support.workspace(tmp_path))
    handle = backend.submit(request)

    with pytest.raises(JoinTimeoutError) as raised:
        join_required(
            backend,
            {support.NODE: handle},
            deadline_seconds=0.0,
            sleep=lambda _seconds: None,
        )

    assert raised.value.state == "JOIN_TIMEOUT"
    assert raised.value.waiting == (support.NODE,)


def test_join_refuses_a_required_node_without_a_handle() -> None:
    backend = support.backend(ScriptedTargetCluster())

    with pytest.raises(KeyError):
        join_required(backend, {}, ["node-0001"])


class _Seat:
    """An executor seat that measures how many starts overlap."""

    def __init__(self) -> None:
        self.active = 0
        self.peak = 0
        self.started: list[str] = []

    async def start(self, node: DagNode) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.started.append(node.node_id)
        await asyncio.sleep(0.05)
        self.active -= 1

    async def act(self, node: DagNode, situation: Situation) -> None:
        _ = node, situation


class _Decision:
    """The Master/Review port shape; the start path never calls it."""

    async def act(self, situation: Situation) -> None:
        _ = situation


def _ready_situation(count: int) -> Situation:
    nodes = tuple(
        DagNode.create(
            project_id="project-0001",
            node_type=NodeType.RESEARCH,
            objective=f"Survey topic {index}.",
            created_by="master",
        ).model_copy(update={"status": NodeStatus.READY})
        for index in range(count)
    )
    return Situation(
        project=SimpleNamespace(),  # type: ignore[arg-type]
        nodes=nodes,
        pre_run={},
        open_deviations=(),
    )


@pytest.mark.asyncio
async def test_the_production_loop_starts_at_most_the_project_quota() -> None:
    """The loop, not only the coordinator, is what a supervisor actually runs."""
    seat = _Seat()
    loop = ProjectLoop(
        database=SimpleNamespace(),  # type: ignore[arg-type]
        project_id="project-0001",
        master=_Decision(),
        review=_Decision(),
        compute_worker=seat,
        experimental_worker=seat,
        research=seat,
        executor_start_concurrency=2,
    )
    await loop._executor_starts(_ready_situation(3))
    assert seat.peak == 2
    assert len(seat.started) == 3

    serial = _Seat()
    serial_loop = ProjectLoop(
        database=SimpleNamespace(),  # type: ignore[arg-type]
        project_id="project-0001",
        master=_Decision(),
        review=_Decision(),
        compute_worker=serial,
        experimental_worker=serial,
        research=serial,
        executor_start_concurrency=1,
    )
    await serial_loop._executor_starts(_ready_situation(3))
    assert serial.peak == 1
