"""Worker dispatch in ProjectLoop."""

from __future__ import annotations

import asyncio

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, ProjectStatus, ReviewOutcome
from ravel.domain.execution import DeviationRecord
from ravel.domain.project import Project
from ravel.execution.loop import ProjectLoop, Situation


class _FakeWorker:
    def __init__(self, name: str) -> None:
        self.name = name
        self.calls: list[tuple[str, str]] = []
        self.started: list[str] = []
        self.fail = False
        self.fail_start = False

    async def start(self, node: DagNode) -> None:
        if self.fail_start:
            raise RuntimeError("the session is gone")
        self.started.append(node.node_id)

    async def act(self, node: DagNode, situation: Situation) -> None:
        _ = situation
        self.calls.append((node.node_id, node.status.value))
        if self.fail:
            raise RuntimeError("the session is gone")


def _project() -> Project:
    return Project(title="screen", objective="test", created_by="u1")


def _node(project_id: str, node_type: NodeType, status: NodeStatus) -> DagNode:
    node = DagNode.create(
        project_id=project_id,
        node_type=node_type,
        objective="test node",
        created_by="u1",
    )
    return node.model_copy(update={"status": status})


def _situation(
    project: Project,
    nodes: list[DagNode],
    deviations: list[DeviationRecord] | None = None,
) -> Situation:
    return Situation(
        project=project,
        nodes=tuple(nodes),
        pre_run={},
        open_deviations=tuple(deviations or ()),
    )


def _loop(
    *,
    compute_worker: _FakeWorker | None = None,
    experimental_worker: _FakeWorker | None = None,
    research: _FakeWorker | None = None,
) -> ProjectLoop:
    return ProjectLoop(
        database=None,  # type: ignore[arg-type]
        project_id="p-1",
        master=None,  # type: ignore[arg-type]
        review=None,  # type: ignore[arg-type]
        compute_worker=compute_worker or _FakeWorker("compute"),
        experimental_worker=experimental_worker or _FakeWorker("experimental"),
        research=research or _FakeWorker("research"),
    )


def _dispatched(loop: ProjectLoop, situation: Situation) -> dict[str, str]:
    """Which node goes to which seat, named by the seat it was given."""
    seats = {
        id(loop.compute_worker): "compute",
        id(loop.experimental_worker): "experimental",
        id(loop.research): "research",
    }
    return {
        node.node_id: seats[id(seat)]
        for node, seat, _episode in loop._turn_list(situation)
    }


def test_waiting_experiment_goes_to_the_experimental_worker() -> None:
    project = _project()
    node = _node(project.project_id, NodeType.EXPERIMENT, NodeStatus.WAITING_EXTERNAL)
    loop = _loop(experimental_worker=_FakeWorker("experimental"))

    assert _dispatched(loop, _situation(project, [node])) == {node.node_id: "experimental"}


def test_running_computation_goes_to_the_compute_worker() -> None:
    project = _project()
    node = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.RUNNING)
    loop = _loop(compute_worker=_FakeWorker("compute"))

    assert _dispatched(loop, _situation(project, [node])) == {node.node_id: "compute"}


def test_a_running_experiment_goes_to_the_experimental_worker() -> None:
    """A lab that is running is a lab with a question to report."""
    project = _project()
    node = _node(project.project_id, NodeType.EXPERIMENT, NodeStatus.RUNNING)
    loop = _loop(experimental_worker=_FakeWorker("experimental"))

    assert _dispatched(loop, _situation(project, [node])) == {node.node_id: "experimental"}


def test_an_open_deviation_is_not_also_a_worker_turn() -> None:
    """Master owns the question; a Worker turn on it would be a second actor."""
    project = _project()
    node = _node(project.project_id, NodeType.EXPERIMENT, NodeStatus.WAITING_DECISION)
    deviation = DeviationRecord(
        project_id=project.project_id,
        node_id=node.node_id,
        execution_contract_ref="c-1",
        requested_action="SET pressure",
        description="out of range",
        permitted=False,
        raised_by="backend",
    )
    compute = _FakeWorker("compute")
    experimental = _FakeWorker("experimental")
    loop = _loop(compute_worker=compute, experimental_worker=experimental)

    situation = _situation(project, [node], [deviation])

    assert loop._turn_list(situation) == []
    assert situation.needs_decision, "the round is Master's, and the loop takes it first"


def test_nodes_that_are_not_live_get_no_turn() -> None:
    project = _project()
    ready = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.READY)
    reviewing = _node(project.project_id, NodeType.EXPERIMENT, NodeStatus.REVIEWING)
    done = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.PASSED)
    loop = _loop(
        compute_worker=_FakeWorker("compute"),
        experimental_worker=_FakeWorker("experimental"),
    )

    assert loop._turn_list(_situation(project, [ready, reviewing, done])) == []


def test_a_running_research_task_goes_to_the_research_seat() -> None:
    """Research executes a RESEARCH node, so a running one is its to serve."""
    project = _project()
    research = _node(project.project_id, NodeType.RESEARCH, NodeStatus.RUNNING)
    loop = _loop(research=_FakeWorker("research"))

    assert _dispatched(loop, _situation(project, [research])) == {
        research.node_id: "research"
    }


def test_a_node_whose_executor_is_not_a_seat_is_left_alone() -> None:
    """A HYPOTHESIS node is Master's, and Master is not dispatched a node."""
    project = _project()
    hypothesis = _node(project.project_id, NodeType.HYPOTHESIS, NodeStatus.RUNNING)
    loop = _loop(
        compute_worker=_FakeWorker("compute"),
        experimental_worker=_FakeWorker("experimental"),
    )

    assert loop._turn_list(_situation(project, [hypothesis])) == []


def test_a_live_node_is_served_once_per_episode() -> None:
    """One turn per `(node, status)`: a turn per poll would be a model bill."""
    project = _project()
    node = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.RUNNING)
    loop = _loop(compute_worker=_FakeWorker("compute"))
    situation = _situation(project, [node])

    first = loop._turn_list(situation)
    assert len(first) == 1
    loop._served.update(key for _node_, _seat, key in first)

    assert loop._turn_list(situation) == []


def test_a_new_episode_is_served_again() -> None:
    """A node that leaves a live state and comes back is a second episode."""
    project = _project()
    node = _node(project.project_id, NodeType.EXPERIMENT, NodeStatus.WAITING_EXTERNAL)
    loop = _loop(experimental_worker=_FakeWorker("experimental"))

    waiting = loop._turn_list(_situation(project, [node]))
    loop._served.update(key for _node_, _seat, key in waiting)

    running = _node(project.project_id, NodeType.EXPERIMENT, NodeStatus.RUNNING)
    assert len(loop._turn_list(_situation(project, [running]))) == 1

    # Back to waiting: the old key was pruned while the node was RUNNING, so
    # this is a new episode for the same node rather than a repeat.
    again = loop._turn_list(_situation(project, [node]))
    assert len(again) == 1


async def test_a_turn_is_recorded_even_when_the_worker_raises() -> None:
    """A session that failed still looked; asking it again every poll is worse."""
    project = _project()
    node = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.RUNNING)
    worker = _FakeWorker("compute")
    worker.fail = True
    loop = _loop(compute_worker=worker)
    situation = _situation(project, [node])

    await loop._executor_turns(situation)

    assert worker.calls == [(node.node_id, NodeStatus.RUNNING.value)]
    assert loop._turn_list(situation) == []


def test_a_cleared_node_is_started_by_the_worker_that_owns_it() -> None:
    """Phase 10H: work begins at the Worker's turn, not at the loop's.

    The loop's whole part is noticing that the DAG has cleared a node; the run
    exists because the seat that owns that node was asked to start one.
    """
    project = _project()
    node = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.READY)
    compute = _FakeWorker("compute")
    loop = _loop(compute_worker=compute)
    situation = Situation(
        project=project,
        nodes=(node,),
        pre_run={node.node_id: ReviewOutcome.PASS},
        open_deviations=(),
    )

    assert situation.runnable == (node,), "the node is not cleared to run"
    asyncio.run(loop._executor_starts(situation))

    assert compute.started == [node.node_id]


def test_a_node_that_is_not_cleared_is_not_started() -> None:
    """The pre-flight gate is the DAG's, and the loop does not reach past it."""
    project = _project()
    node = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.READY)
    compute = _FakeWorker("compute")
    loop = _loop(compute_worker=compute)
    situation = _situation(project, [node])

    assert situation.needs_pre_flight == (node,)
    assert situation.runnable == ()

    asyncio.run(loop._executor_starts(situation))

    assert compute.started == []


def test_a_node_being_started_is_not_also_a_monitoring_turn() -> None:
    """READY and RUNNING are different episodes with different turns."""
    project = _project()
    node = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.READY)
    loop = _loop(compute_worker=_FakeWorker("compute"))
    situation = Situation(
        project=project,
        nodes=(node,),
        pre_run={node.node_id: ReviewOutcome.PASS},
        open_deviations=(),
    )

    assert loop._turn_list(situation) == []


def test_a_worker_that_cannot_start_the_node_leaves_it_ready() -> None:
    """A failed start is a round that changed nothing, not a lost node.

    The node stays READY, so the next round hands it over again — which is the
    same recovery a Worker that died between two rounds gets.
    """
    project = _project()
    node = _node(project.project_id, NodeType.COMPUTATION, NodeStatus.READY)
    compute = _FakeWorker("compute")
    compute.fail_start = True
    loop = _loop(compute_worker=compute)
    situation = Situation(
        project=project,
        nodes=(node,),
        pre_run={node.node_id: ReviewOutcome.PASS},
        open_deviations=(),
    )

    asyncio.run(loop._executor_starts(situation))

    assert situation.runnable == (node,), "the DAG moved the node without a run"


def test_the_project_status_does_not_change_the_dispatch() -> None:
    """Dispatch reads node status; where the project is is Master's business."""
    project = (
        _project()
        .transition(ProjectStatus.CONTRACT_DEFINED)
        .transition(ProjectStatus.EXECUTING)
    )
    node = _node(project.project_id, NodeType.EXPERIMENT, NodeStatus.WAITING_EXTERNAL)
    loop = _loop(experimental_worker=_FakeWorker("experimental"))

    assert _dispatched(loop, _situation(project, [node])) == {node.node_id: "experimental"}
