"""How often `ProjectLoop` asks Master and Review a question nobody answers.

Dispatch for the execution seats is one turn per `(node, status)` episode; the
four questions — the decision, the pre-flight clearance, the final verdict, the
ending — are not episodes but conditions, and a condition holds for as long as
it holds. Without a budget the loop asks again on every round, which is a model
call per poll for as long as the project waits: the cost defect Phase 10's turn
policy exists to close.

What the budget is kept under is what these tests are about, because that is
where the behaviour is: a question is identified by *what is being asked*, so an
answer that changes the situation brings a new question with a fresh budget,
and a question that stops being asked takes its spent budget with it.
"""

from __future__ import annotations

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, ReviewOutcome
from ravel.domain.project import Project
from ravel.execution.loop import ProjectLoop, Situation


class _Counting:
    """A power that takes every turn it is given and does nothing with it."""

    def __init__(self) -> None:
        self.turns = 0

    async def act(self, situation: Situation) -> None:
        _ = situation
        self.turns += 1

    def close(self) -> None:
        """Nothing to close: a counter holds no runtime."""


class _Seat:
    """An execution seat that records what it is handed.

    None of these tests is about the seats. One of them clears a node, though,
    and a cleared node is exactly what a seat is handed, so the seat is here to
    make that a recorded fact rather than a swallowed one.
    """

    def __init__(self) -> None:
        self.started: list[str] = []
        self.turns: list[tuple[str, str]] = []

    async def start(self, node: DagNode) -> None:
        self.started.append(node.node_id)

    async def act(self, node: DagNode, situation: Situation) -> None:
        _ = situation
        self.turns.append((node.node_id, node.status.value))


def _project() -> Project:
    return Project(title="screen", objective="test", created_by="u1")


#: What `ProjectLoop` holds a seat for, spelled out here rather than read off
#: it: this is the fact the loop's own seating is asserted against, and a test
#: that derived it from the thing under test would pass whatever the two agreed
#: on.
_SEATED = frozenset({NodeType.COMPUTATION, NodeType.EXPERIMENT, NodeType.RESEARCH})


def _waiting(project: Project) -> DagNode:
    """A READY node that owes a pre-flight review nobody has submitted."""
    node = DagNode.create(
        project_id=project.project_id,
        node_type=NodeType.COMPUTATION,
        objective="test node",
        created_by="u1",
    )
    return node.model_copy(update={"status": NodeStatus.READY})


def _ready(project: Project, node_type: NodeType) -> DagNode:
    """A READY node of any type, cleared or not depending on what it owes."""
    node = DagNode.create(
        project_id=project.project_id,
        node_type=node_type,
        objective="test node",
        created_by="u1",
    )
    return node.model_copy(update={"status": NodeStatus.READY})


def _situation(
    project: Project,
    nodes: list[DagNode],
    *,
    pre_run: dict[str, ReviewOutcome | None] | None = None,
    executable: frozenset[NodeType] | None = None,
) -> Situation:
    return Situation(
        project=project,
        nodes=tuple(nodes),
        pre_run=pre_run if pre_run is not None else {},
        open_deviations=(),
        executable=executable if executable is not None else frozenset(NodeType),
    )


def _loop(
    *,
    master: _Counting,
    review: _Counting,
    budget: int = 3,
    compute_worker: _Seat | None = None,
) -> ProjectLoop:
    return ProjectLoop(
        database=None,  # type: ignore[arg-type]
        project_id="p-1",
        master=master,
        review=review,
        compute_worker=compute_worker or _Seat(),
        experimental_worker=_Seat(),
        research=_Seat(),
        max_turns_per_question=budget,
    )


async def test_a_question_is_asked_a_bounded_number_of_times() -> None:
    """The condition holds for ten rounds; the question is put three times.

    This is the whole of the policy, and the failure it prevents is a bill: a
    Review that takes every turn and writes nothing used to be asked once per
    round for as long as the node waited.
    """
    project = _project()
    node = _waiting(project)
    review = _Counting()
    loop = _loop(master=_Counting(), review=review)
    situation = _situation(project, [node], pre_run={node.node_id: None})

    for _ in range(10):
        await loop._advance(situation)

    assert review.turns == 3, "the question was asked more often than its budget"
    assert loop._unanswered, (
        "the loop did not record the question as unanswered, so nothing counts "
        "these rounds as stalled and the project never stops waiting"
    )


async def test_the_ending_is_asked_under_the_same_budget() -> None:
    """Master's ending is a question like any other, and spends the same budget.

    A project whose DAG has nothing left to do is one that wants concluding, and
    a Master that does not conclude — a model that answered in prose, or one
    that refused the ending and said nothing — must not be asked once per round
    for the life of the deployment.
    """
    project = _project()
    master = _Counting()
    loop = _loop(master=master, review=_Counting())
    situation = _situation(project, [])
    assert not situation.has_work_to_do and not situation.unfinished, (
        "this situation is supposed to be one with only an ending left in it"
    )

    for _ in range(6):
        await loop._advance(situation)

    assert master.turns == 3


async def test_a_question_that_ends_its_episode_gets_a_fresh_budget() -> None:
    """A node cleared and later waiting again is a question asked anew.

    The budget is pruned to the questions the situation is actually raising, and
    the pruning is by *identity*: a cleared node is a different thing to be
    asked about than an uncleared one, so the round in which it was not being
    asked ends the episode and the next one starts with a full budget. Keeping
    the spent budget instead would end a project that had simply moved on.
    """
    project = _project()
    node = _waiting(project)
    review = _Counting()
    compute = _Seat()
    loop = _loop(master=_Counting(), review=review, compute_worker=compute)
    waiting = _situation(project, [node], pre_run={node.node_id: None})
    cleared = _situation(project, [node], pre_run={node.node_id: ReviewOutcome.PASS})

    for _ in range(3):
        await loop._advance(waiting)
    assert review.turns == 3

    await loop._advance(cleared)  # nothing to ask: the node is cleared
    assert review.turns == 3, "a cleared node was still put to Review"
    assert compute.started == [node.node_id], (
        "the round that cleared the node is the round the seat is handed it"
    )

    await loop._advance(waiting)
    assert review.turns == 4, (
        "the second episode inherited the first one's spent budget, so a project "
        "that moved on and came back can never be reviewed again"
    )


async def test_a_question_whose_budget_is_spent_is_not_asked_again() -> None:
    """Once spent, the question is dropped until the situation changes."""
    project = _project()
    node = _waiting(project)
    review = _Counting()
    loop = _loop(master=_Counting(), review=review, budget=1)
    situation = _situation(project, [node], pre_run={node.node_id: None})

    await loop._advance(situation)
    await loop._advance(situation)

    assert review.turns == 1
    assert loop._unanswered == {"pre-flight:" + node.node_id}, (
        "the key the budget is kept under is not the one the round asks under"
    )


# ── The decision question: what a project waits on Master for ───────────────
#
# A plan can contain a node no seat can run. `NODE_EXECUTOR` assigns DECISION
# and HYPOTHESIS to Master, and Master's part in the loop is to be asked rather
# than handed a task — so a READY node of either type is work with no executor.
# It produces no event, no wait and no failure, which is why it has to be
# *said*: counted as ordinary work it is a project that can never finish (no
# seat will end it) and can never end (the ending waits on every node having
# finished). It is the state the Phase 10 live run reached, and these are the
# three facts that close it.


def test_a_loop_is_seated_for_exactly_the_types_it_can_begin() -> None:
    """The loop's seating is its dispatch: three seats, three node types."""
    loop = _loop(master=_Counting(), review=_Counting())

    assert loop._seated == _SEATED, (
        "the node types the loop says it can run are not the ones it holds a "
        "seat for, so a situation is narrowed by a different fact than the one "
        "dispatch uses"
    )


def test_a_node_no_seat_can_run_is_master_s_question_and_not_a_task() -> None:
    """A READY DECISION node is neither runnable nor waiting on Review.

    Both of the other answers are wrong in a way that looks reasonable. Handing
    it to an execution seat would be handing somebody a task whose transition
    the DAG refuses; leaving it in `needs_pre_flight` would have Review writing
    a checkpoint for a node nobody will ever run. The true answer is that
    nothing here may proceed, which is Master's.
    """
    project = _project()
    decision = _ready(project, NodeType.DECISION)
    situation = _situation(project, [decision], executable=_SEATED)

    assert situation.unexecutable == (decision,)
    assert not situation.runnable
    assert not situation.needs_pre_flight
    assert situation.needs_decision, (
        "a READY node no seat can execute was not put to Master, so the project "
        "holds a task that will never move and nobody is ever told"
    )
    assert situation.has_work_to_do


def test_a_reader_that_runs_nothing_narrows_nothing() -> None:
    """The Gateway reads a situation without claiming any node type.

    It runs no project, so it has no seat to exclude and its answer to "what is
    waiting" must not depend on which seats some other process holds. The
    default says that: the same node the loop calls unexecutable is ordinary
    READY work to a reader that has not said what it can run.
    """
    project = _project()
    decision = _ready(project, NodeType.DECISION)
    situation = _situation(project, [decision])

    assert not situation.unexecutable
    assert situation.runnable == (decision,), (
        "the default `executable` is not every node type, so a reader that runs "
        "nothing is silently narrowing what it reports as work"
    )


async def test_a_plan_with_no_executor_is_put_to_master() -> None:
    """The loop asks Master about it rather than waiting for a node to move."""
    project = _project()
    decision = _ready(project, NodeType.DECISION)
    master = _Counting()
    loop = _loop(master=master, review=_Counting())
    situation = _situation(project, [decision], executable=loop._seated)

    await loop._advance(situation)

    assert master.turns == 1, (
        "the round did not ask Master about a node nothing can execute, so the "
        "project waits on a turn nobody was given"
    )


async def test_cancelling_the_unexecutable_node_is_what_lets_the_project_end() -> None:
    """Master's answer is the plan's, and once it is given the project can end.

    The two halves are one fact. While the node is READY the project is not
    ready to conclude — there is something unfinished and Master is being asked
    about it — and the moment it is CANCELLED both stop being true and the
    ending question is asked instead. This is the whole of the live run's
    21 minutes of silence, as a unit test.
    """
    project = _project()
    decision = _ready(project, NodeType.DECISION)
    cancelled = decision.model_copy(update={"status": NodeStatus.CANCELLED})
    master = _Counting()
    loop = _loop(master=master, review=_Counting())
    waiting = _situation(project, [decision], executable=loop._seated)
    ended = _situation(project, [cancelled], executable=loop._seated)

    assert not loop._ready_to_conclude(waiting), (
        "the loop would conclude a project on the strength of a node it has "
        "not resolved"
    )
    await loop._advance(waiting)
    assert master.turns == 1

    assert loop._ready_to_conclude(ended)
    await loop._advance(ended)

    assert master.turns == 2, (
        "the project stopped and only an ending was left, and Master was not "
        "asked for one — so the project has stopped without having ended"
    )
    assert loop._unanswered == set(), "the ending is a question, not a stall"
