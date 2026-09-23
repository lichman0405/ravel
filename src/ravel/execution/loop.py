"""The project loop: who acts next, and how RAVEL knows the project is stuck.

A project is not one workflow. It is a DAG whose nodes run when their
dependencies are settled, reviews that clear them to run and accept what they
produce, and Master's decisions when a Worker stops or the work is over. Each
of those is durable and already implemented; what is missing from them is the
*sequencing* — and the sequencing is this file.

**The loop holds none of the three powers.** It does not decide what work
exists, it does not decide whether a result is acceptable, and it does not
execute anything. Those arrive as three ports (`MasterPort`, `ReviewPort`,
`ExecutorPort`): the first two are DSH sessions in production, and the third is
the seat that executes a node — a Compute Worker, an Experimental Worker, or
Research, whichever `NODE_EXECUTOR` names for the node's type. What the loop
does is read PostgreSQL — the authoritative state — work out which of the three
has something to do, and hand it the situation.

**The executor is the execution role, and the loop is not.** A node runs
because the agent whose node it is began it, through its own authorized tool —
`start_execution` for a Worker, which hands the work to the Execution Service,
and `begin_research` for Research, whose work is the reading itself. The loop
asks for that turn and does not take it. This is Phase 10H's chain, and it is
why there is no execution port here: a loop that could start a run would be a
second way for work to begin, which is the one thing an execution seat is in
the roster to prevent. What the loop still owns is *sequencing* — a seat is
handed a READY node only when the DAG's own transitions have put it there, with
its contract bound and its pre-flight review passed, so the turn executes a
plan Master already recorded rather than choosing one.

**A seat is handed turns, not powers.** It reads a live node's frozen contract
and record, and it is where a `request_action` or a `send_message` comes from.
It decides nothing: a request the contract does not permit is recorded as a
deviation, and the next round is Master's. The loop gives it one turn per
`(node, status)` episode rather than one per round, because a turn that found
nothing to say has nothing to say a poll later.

**A question that goes unanswered is asked a bounded number of times.** What a
Worker's episode rule does for execution, `max_turns_per_question` does for the
three questions that are not tied to a node's episode: whether the project can
be ended, whether a result passes, whether a node may run, and what Master
decides. Those are asked again while their condition holds, which is what makes
a turn that failed recoverable — and which, for a turn that *succeeded* and
submitted nothing, is a model call per round for as long as the condition
lasts. So a question is identified by what is being asked, asked at most
`max_turns_per_question` times, and then dropped: the loop stops asking, counts
its rounds as unmoved, and its stall detector ends the run. A re-drive is then
a bounded retry rather than a spin, and an unattended project's cost is bounded
by its questions rather than by how long nobody is watching.

The loop never creates, cancels, or re-points a node, never writes a review,
and never records a decision — the ports do, through the same services their
MCP tools wrap, so nothing here is a second path to those writes.

**And it ticks the DAG's readiness.** A node whose dependency has passed should
be READY, and one whose dependency has failed for good should be BLOCKED; both
follow from the graph, and neither is written by the transition that settled the
fan-in. `DagRepository.refresh_readiness` derives them, and the loop is what
calls it — once per round, before reading. This is the scheduler's tick rather
than a fourth power: it decides nothing, and a round that finds nothing to
promote changes nothing.

**Stalling is a state, not a hang.** A turn that changes nothing is recorded and
counted. After `max_stalled_rounds` of them the loop stops and says so, rather
than spinning: an agent that cannot make progress is a fact about the project,
and a loop that would wait forever hides it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType, ProjectStatus, ReviewOutcome
from ravel.domain.execution import DeviationRecord
from ravel.domain.project import Project
from ravel.domain.state_machines import TERMINAL_PROJECT_STATUSES, requires_frozen_criteria
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DeviationRepository

logger = logging.getLogger(__name__)

__all__ = [
    "ExecutorPort",
    "LoopHalted",
    "MasterPort",
    "ProjectLoop",
    "ProjectRun",
    "ReviewPort",
    "Situation",
    "read_situation",
]


def read_situation(
    database: Database,
    project_id: str,
    *,
    executable: frozenset[NodeType] = frozenset(NodeType),
) -> Situation:
    """One project's authoritative state, as of one read.

    Module-level rather than a method because it has two callers with nothing
    else in common: the loop, once per round, and the Gateway, once per thing a
    person says to Master. Both are asking the same question — where is this
    project, and what is waiting on it — and a second reader written beside the
    second caller is how the two would come to disagree about what the project
    is doing.

    Read-only, and in a transaction of its own. An agent turn can take minutes
    and nothing may hold a write transaction across one.
    """
    with database.read_only() as session:
        project = ProjectRegistry(session).get(project_id)
        dag = DagRepository(session, project_id)
        nodes = tuple(dag.nodes())
        return Situation(
            project=project,
            nodes=nodes,
            pre_run={
                node.node_id: dag.latest_pre_run_outcome(node.node_id) for node in nodes
            },
            open_deviations=tuple(DeviationRepository(session, project_id).open()),
            executable=executable,
        )


def _key(what: str, *groups: list[str]) -> str:
    """A question's identity: what is being asked, and about what.

    Built from identifiers rather than from the round, so that two rounds
    asking the same thing produce the same key and a round asking something
    else does not inherit the first one's budget. The name is in front because
    this ends up in a log line a person reads when a project stops.
    """
    named = sorted({name for group in groups for name in group})
    return f"{what}:{','.join(named)}" if named else what


@dataclass(frozen=True, slots=True)
class Situation:
    """One project's authoritative state, as of one read.

    Assembled fresh for every round rather than cached: the whole point of the
    loop is that a turn's effect is discovered by reading PostgreSQL again, and
    a situation carried across rounds would let the loop act on a plan a turn
    has already replaced.

    Every field is a fact read from the database. The derived views below are
    the *questions the loop asks of it*, kept here so that the three ports and
    the loop agree about what "something to do" means — a second definition of
    "needs review" would be a second thing to keep in step.
    """

    project: Project
    nodes: tuple[DagNode, ...]
    #: Node id -> the verdict of its most recent PRE_RUN review, or `None`.
    pre_run: dict[str, ReviewOutcome | None]
    #: The escalations no decision has answered yet, as records rather than as
    #: ids: what a Worker asked for and why the contract refused it is the whole
    #: of what Master is being asked to answer, and a port handed a list of
    #: identifiers would have to read the database again to learn it.
    open_deviations: tuple[DeviationRecord, ...]
    #: The node types the process that read this can actually drive. A READY
    #: node of a type no seat holds is not work: nothing can move it, so
    #: handing it to anybody would be handing them a task with no executor —
    #: and counting it as work is what leaves a project unable to finish and
    #: unable to end, which is the state the live run reached.
    #:
    #: The default is every type, and that is a statement rather than a
    #: convenience: a reader that has not said what it can run is not the
    #: process running this project (the Gateway reads a situation to answer a
    #: person, and runs nothing), so it has no opinion to contribute and the
    #: question is not its to narrow. `ProjectLoop` names its own seats.
    executable: frozenset[NodeType] = frozenset(NodeType)

    @property
    def is_finished(self) -> bool:
        return self.project.status in TERMINAL_PROJECT_STATUSES

    @property
    def unfinished(self) -> tuple[DagNode, ...]:
        return tuple(node for node in self.nodes if not node.is_terminal)

    @property
    def in_flight(self) -> tuple[DagNode, ...]:
        """Nodes something else is already working on.

        RUNNING is a Worker's, REVIEWING is Review's, WAITING_EXTERNAL is a
        lab's. None of them is the loop's, and a loop that also acted on them
        would be a second actor on a node whose owner is already acting. They
        are progress, not work to hand out: the loop's job with these is to
        wait, which is why they count as something to do rather than as
        something to do *now*.
        """
        return tuple(
            node
            for node in self.nodes
            if node.status
            in (
                NodeStatus.RUNNING,
                NodeStatus.REVIEWING,
                NodeStatus.WAITING_EXTERNAL,
            )
        )

    @property
    def unexecutable(self) -> tuple[DagNode, ...]:
        """READY nodes of a type nothing in this wiring has a seat for.

        A node type is in `executable` when the process reading this holds the
        seat that begins it. Being assigned to a role is not the same as a
        session in that role having an execution path into the node: a DECISION
        node is Master's, and Master's part in the loop is to be *asked*, not
        to be handed a task. So these are not work and nobody will ever be
        given them — they sit here until Master cancels them or replaces them
        with work that runs.

        Why a type is here rather than in `runnable` is
        `unexecutable_reason`, and it is stated for the type rather than for
        the reader: a node nobody can be given is a fact about the plan, and
        whoever is told about it — Master in its prompt, a person reading the
        project state — is owed the same sentence. Nothing said it until
        KNOWN_LIMITATIONS L-27 was resolved, and what that cost is in the
        record.
        """
        return tuple(
            node
            for node in self.nodes
            if node.status is NodeStatus.READY and node.node_type not in self.executable
        )

    @property
    def runnable(self) -> tuple[DagNode, ...]:
        """READY nodes cleared to run, which is what the runtime may start.

        Cleared means exactly what `DagNode.can_enter_running` means by it: a
        node type that owes a pre-flight review has one, and it passed. A READY
        node without one is `needs_pre_flight` instead — handing it to the
        runtime would produce a refused transition inside an activity, which
        reads as a runtime fault rather than as the wait it is.

        A node of a type no seat holds is neither: it is `unexecutable`, which
        is a question for Master rather than a wait for anybody.
        """
        return tuple(
            node
            for node in self.nodes
            if node.status is NodeStatus.READY
            and node.node_type in self.executable
            and self._cleared(node)
        )

    @property
    def needs_pre_flight(self) -> tuple[DagNode, ...]:
        """READY nodes still waiting on the checkpoint that clears them."""
        return tuple(
            node
            for node in self.nodes
            if node.status is NodeStatus.READY
            and node.node_type in self.executable
            and not self._cleared(node)
        )

    @property
    def blocked(self) -> tuple[DagNode, ...]:
        """Nodes whose fan-in can no longer be satisfied.

        A BLOCKED node is the DAG saying "this can never run". Nothing in the
        runtime will move it — `refresh_readiness` moves it only if a
        dependency later passes — so it waits for Master to replan or to end
        the project, and the loop's part in that is to hand Master the state.
        """
        return tuple(node for node in self.nodes if node.status is NodeStatus.BLOCKED)

    @property
    def needs_decision(self) -> bool:
        """Whether Master has something waiting that only Master may do.

        Four ways a project waits on Master, and they arrive from different
        places: a Worker that stopped (`WAITING_DECISION`, which is what raising
        a deviation moves the node to), a question nobody has answered yet (the
        deviation itself — the node can be terminal while the question is not),
        a branch that can never be satisfied, and a node nothing in this wiring
        can execute. All four are the same fact from the loop's side: nothing
        here may proceed.

        The fourth is the one that has to be *said* rather than assumed: a node
        nobody can run produces no event, no wait, and no failure — it is a
        READY node in a plan, which reads as work in progress from every other
        angle. Left out of this question it is also a project that can never
        end, because `_ready_to_conclude` asks whether anything is unfinished
        and a task with no executor never becomes finished. Asking Master about
        it is the only way it gets resolved, and `cancel_dag_node` is how.
        """
        if self.open_deviations:
            return True
        if self.unexecutable:
            return True
        return any(
            node.status in (NodeStatus.WAITING_DECISION, NodeStatus.BLOCKED)
            for node in self.nodes
        )

    @property
    def has_work_to_do(self) -> bool:
        """Whether any of the three powers has a reason to act right now."""
        return bool(
            self.needs_decision
            or self.needs_pre_flight
            or self.runnable
            or self.in_flight
        )

    def _cleared(self, node: DagNode) -> bool:
        if not requires_frozen_criteria(node.node_type):
            return True
        return self.pre_run.get(node.node_id) is ReviewOutcome.PASS


# ── The three powers, as ports ──────────────────────────────────────────────
#
# Protocols rather than a base class: an implementation is any object with the
# method, so the harness-backed one, a scripted one in a test, and a future one
# that talks to a human operator are all the same thing to the loop.


@runtime_checkable
class MasterPort(Protocol):
    """Decision: everything that changes the plan or ends the project."""

    async def act(self, situation: Situation) -> None:
        """Make whatever decision the situation calls for, and record it.

        Called when a Worker is waiting on Master, and when the project has
        stopped moving but has not ended. Both are Master's alone: the first is
        A12's escalation, the second is A20's ending — and a loop that decided
        either would be the merge of two powers the role model separates.
        """
        ...


@runtime_checkable
class ReviewPort(Protocol):
    """Review: the verdicts that clear a node to run and accept its result."""

    async def act(self, situation: Situation) -> None:
        """Judge the nodes waiting on a verdict.

        The pre-flight checkpoint is the one the loop depends on for
        sequencing — a node that owes one cannot run — but a final verdict is
        Review's too, and a port that answered only the first would leave a
        node in REVIEWING forever.
        """
        ...


@runtime_checkable
class ExecutorPort(Protocol):
    """The execution role for one node type: begin a task, then serve it.

    Not named for the Workers, because it is not only theirs: `NODE_EXECUTOR`
    gives each node type the role that executes it, and Research is the
    executor of a RESEARCH node. The two methods are the same for all three
    seats — a task begins as the act of the seat that owns it, and a live task
    is served by that seat — which is why one port covers them and why the
    loop's dispatch does not care which seat it is holding.
    """

    async def start(self, node: DagNode) -> None:
        """Begin the node's task, if the DAG says it may run.

        The seat's own act, through its own authorized tool, and the entry
        point Phase 10H names: work begins here or it does not begin. The
        tool refuses a node that is not cleared to run, so this is a request
        rather than an authority — the port is handed a node the loop has
        already established is READY with its pre-flight review passed, and
        what it may do about that is still the DAG's answer.
        """
        ...

    async def act(self, node: DagNode, situation: Situation) -> None:
        """One turn on a task already in flight: monitoring, or speaking."""
        ...


class LoopHalted(RuntimeError):
    """The loop stopped without the project reaching an ending."""


@dataclass(frozen=True, slots=True)
class ProjectRun:
    """What a loop run ended with."""

    project_id: str
    status: ProjectStatus
    rounds: int
    #: Rounds in which nothing about the project changed. Non-zero is not a
    #: failure — a round that started a run changes nothing until the run
    #: finishes — but a run that *ended* stalled is.
    stalled_rounds: int
    #: True when the loop stopped because it ran out of rounds or of patience,
    #: rather than because the project ended.
    halted: bool = False

    @property
    def finished(self) -> bool:
        return self.status in TERMINAL_PROJECT_STATUSES


@dataclass
class ProjectLoop:
    """Sequences one project, until it ends or stops moving."""

    database: Database
    project_id: str
    master: MasterPort
    review: ReviewPort
    #: The three execution seats. Required, and each one is what begins its own
    #: kind of node: a loop that could run a node without the agent that
    #: executes it would be the bypass Phase 10H exists to close, so there is
    #: no wiring in which a seat is absent — only wiring in which it is
    #: scripted. One per node type `NODE_EXECUTOR` assigns to a role.
    compute_worker: ExecutorPort
    experimental_worker: ExecutorPort
    research: ExecutorPort
    #: How long to wait between rounds. Every round re-reads PostgreSQL, so this
    #: is the project's reaction time rather than a sleep: a node that finishes
    #: in an activity is noticed on the next round.
    poll_seconds: float = 0.5
    #: The most rounds a run may take. A ceiling rather than a policy: a project
    #: that needs more rounds than this is one whose shape the caller should
    #: look at, and an unbounded loop would hide that behind a hang.
    max_rounds: int = 200
    #: How many rounds in a row may change nothing before the loop gives up.
    #: Rounds spent waiting on work in flight are not counted — see the counter
    #: in `run` — so this is rounds in which the project was not waiting on
    #: anything and still did not move, which at the default poll is half a
    #: minute of a project that has genuinely stopped.
    max_stalled_rounds: int = 60
    #: How many times the loop will ask one question before it stops asking it.
    #: A question is not a round — see the module docstring and `_ask` — so this
    #: is a budget per thing being asked, not a rate. Three is enough for a turn
    #: that failed on a timeout to be retried and for a model that answered in
    #: prose to be asked again; past that, the answer is not coming, and asking
    #: a fourth time is a model call that buys nothing.
    max_turns_per_question: int = 3
    _rounds: int = field(default=0, init=False, repr=False)
    #: The `(node, status)` episodes an execution seat has already been given a
    #: turn for. Dispatch bookkeeping, not a record — see `_turn_list`.
    _served: set[tuple[str, str]] = field(default_factory=set, init=False, repr=False)
    #: Question key -> how many turns it has been asked for. Pruned to the
    #: questions this round is actually asking, so a question that goes away and
    #: comes back — a node that fails, is replanned, and lands in REVIEWING
    #: again — starts a fresh budget rather than inheriting a spent one.
    _asked: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    #: Questions the loop has stopped asking because their budget is spent. Read
    #: by the stall counter in `run`, which is what turns a spent question into
    #: an ending rather than into a silence.
    _unanswered: set[str] = field(default_factory=set, init=False, repr=False)

    async def run(self) -> ProjectRun:
        """Drive the project until it ends, or until it stops moving.

        Returns:
            The ending, or — when the loop halted — the status it halted at,
            with `halted` set. A halt is not an exception: the caller asked for
            a project to be driven, and "it stopped moving" is an answer about
            the project rather than a fault in the loop.
        """
        stalled = 0
        previous: Situation | None = None

        while self._rounds < self.max_rounds:
            situation = self._read()
            self._rounds += 1

            if situation.is_finished:
                return self._end(situation)
            if situation.project.status is ProjectStatus.PAUSED:
                # A pause is a human's decision about the project, and a loop
                # that drove through it would be overriding the one control the
                # operator has. Resuming is what restarts this.
                return self._end(situation, stalled=stalled, halted=True)
            if previous is not None and self._unchanged(previous, situation):
                # Waiting is not stalling. A run in flight changes nothing for
                # as long as it takes, and counting those rounds would halt a
                # project for the crime of doing its work slowly. A question
                # nobody has answered is the exception: something may well be in
                # flight, and none of it is progress — `_advance` has stopped
                # asking, so no round will change this.
                if not situation.in_flight or self._unanswered:
                    stalled += 1
                if stalled >= self.max_stalled_rounds:
                    return self._end(situation, stalled=stalled, halted=True)
            else:
                stalled = 0
            previous = situation

            await self._advance(situation)
            await asyncio.sleep(self.poll_seconds)

        return self._end(self._read(), stalled=stalled, halted=True)

    async def step(self) -> Situation:
        """Read the state and let whoever has something to do do it, once.

        The loop's body without the loop, for a caller that wants to drive the
        project itself — a test that asserts one round in isolation, or a TUI
        that advances the project while a human watches.
        """
        situation = self._read()
        self._rounds += 1
        await self._advance(situation)
        return situation

    # ── One round ───────────────────────────────────────────────────────────

    async def _advance(self, situation: Situation) -> None:
        """Give each power the turn the situation says it owes.

        **Master first, and alone.** A Worker that has stopped is blocking the
        branch behind it, and a review submitted while a decision is outstanding
        would be a verdict on a node whose terms Master is about to change. So a
        project waiting on Master ends the round: everything else waits for the
        answer, which is what "waiting on Master" means everywhere else in RAVEL.

        The order below is otherwise the order of a node's own life: the
        pre-flight checkpoint, the Worker's turn to start it, the Worker's turn
        to monitor it, the final verdict, and finally the ending. A node cannot
        be in two of those states at once, so the order is a convenience rather
        than a precedence — but it is the order a reader expects, and a loop
        that reviewed before clearing would be describing a path the DAG
        refuses.

        **Starting a node is the executor's turn, not the loop's.** The two
        dispatches are one method call apart but two different acts: `start` is
        handed a node the DAG says may run and is where the task begins, and
        `act` is handed a node already in flight. They are separate calls
        because they are separate episodes — a READY node has not been served
        by the turn that will serve it while RUNNING — and because collapsing
        them would make "the seat looked at a live task" and "the seat began
        one" the same event.

        The last branch is the one that ends the project. It fires only when the
        situation has nothing else in it at all — no run in flight, nothing
        waiting on Review, no branch waiting on Master — which is exactly the
        position from which an ending is a statement about results rather than a
        hope. Master may still refuse it (A20's checks are in the service, next
        to the write); the loop does not pre-empt them, because a loop that
        decided when an ending was justified would be holding the decision.

        **The four dispatches that are not execution seats go through `_ask`.**
        They are the loop's questions to Master and Review, and each one carries
        a budget rather than being repeated for as long as its condition holds.
        """
        # The questions this round is asking, and the end of the ones it is not:
        # a question that is no longer being asked has ended its episode, and
        # its budget goes with it. Computed before anything is asked, so that a
        # question answered in this round is not pruned by its own answer.
        #
        # The pruning is over the keys the budget is *kept* under, which are the
        # values of `asking` rather than its names: `asking` maps a question's
        # name to the identity it is being asked under, and it is the identity
        # that goes stale when the thing being asked about changes.
        asking = self._questions(situation)
        being_asked = set(asking.values())
        self._asked = {
            key: count for key, count in self._asked.items() if key in being_asked
        }
        self._unanswered = set()

        if situation.needs_decision:
            await self._ask(asking["decision"], self.master.act, situation)
            return
        if situation.needs_pre_flight:
            await self._ask(asking["pre-flight"], self.review.act, situation)
        await self._executor_starts(situation)
        await self._executor_turns(situation)
        if self._awaiting_final_verdict(situation):
            await self._ask(asking["verdict"], self.review.act, situation)
        if self._ready_to_conclude(situation):
            await self._ask(asking["ending"], self.master.act, situation)

    # ── The questions, and their budget ─────────────────────────────────────

    def _questions(self, situation: Situation) -> dict[str, str]:
        """The questions this round has to ask, each with the key it is asked under.

        A question is identified by *what is being asked*, not by the round it
        is asked in. The ending is one question per project; a final verdict is
        one per set of nodes sitting in REVIEWING; a pre-flight clearance is one
        per set of nodes held back; a decision is one per set of things waiting
        on Master, escalations included. Those keys are what make a spent budget
        mean "nobody is answering this" rather than "this has taken a while": a
        turn that answered changes the set, the key, and the budget with it.

        Only the questions the situation actually raises are returned, which is
        also what prunes the budget: a key absent here belongs to a question
        that is over.
        """
        questions: dict[str, str] = {}
        if situation.needs_decision:
            questions["decision"] = _key(
                "decision",
                [
                    node.node_id
                    for node in situation.nodes
                    if node.status in (NodeStatus.WAITING_DECISION, NodeStatus.BLOCKED)
                ],
                [node.node_id for node in situation.unexecutable],
                [deviation.deviation_id for deviation in situation.open_deviations],
            )
        if situation.needs_pre_flight:
            questions["pre-flight"] = _key(
                "pre-flight", [node.node_id for node in situation.needs_pre_flight]
            )
        if self._awaiting_final_verdict(situation):
            questions["verdict"] = _key(
                "verdict",
                [
                    node.node_id
                    for node in situation.nodes
                    if node.status is NodeStatus.REVIEWING
                ],
            )
        if self._ready_to_conclude(situation):
            # One per project, and the only question with nothing in it: an
            # ending is about the project, and there is one of those.
            questions["ending"] = _key("ending")
        return questions

    async def _ask(
        self,
        key: str,
        answer: Callable[[Situation], Awaitable[None]],
        situation: Situation,
    ) -> None:
        """Put one question to the power that answers it, within its budget.

        Asked again while there is budget, because a turn can fail on a timeout
        or come back with prose instead of a write, and neither is a reason to
        end a project. Silently dropped once the budget is spent: the loop
        records that the question went unanswered, which the stall counter reads
        as a round that moved nothing, and the run ends with the question named
        in the log. That is the whole of the escalation — RAVEL's word for "this
        project has stopped" is the halt, and the caller re-driving the loop is
        the bounded retry.
        """
        asked = self._asked.get(key, 0)
        if asked >= self.max_turns_per_question:
            if asked == self.max_turns_per_question:
                logger.warning(
                    "no answer after %d turns, no longer asking: project=%s question=%s",
                    asked,
                    self.project_id,
                    key,
                )
            self._unanswered.add(key)
            return
        self._asked[key] = asked + 1
        await answer(situation)

    async def _executor_starts(self, situation: Situation) -> None:
        """Hand each node the DAG has cleared to run to the seat that executes it.

        `situation.runnable` is the DAG's answer to "may this node run", and it
        is the whole of the loop's part: it does not decide that a node should
        run, it notices that a node *may*. What happens next is that seat's
        turn, and the task exists because that turn used its own begin tool.

        A node whose type has no seat in this wiring is left where it is rather
        than started by something else — there is no something else, which is
        the point. In the wiring RAVEL ships, every executor in `NODE_EXECUTOR`
        that runs work has a seat; a loop missing one is a wiring fault, and a
        node that quietly ran anyway would hide it.

        A turn that raised is not retried here. The node is still READY, so the
        next round hands it over again, which is the same recovery a seat that
        died between two rounds gets: the DAG does not move, and the turn that
        finds the node READY begins the task it was always going to begin.
        """
        for node in situation.runnable:
            seat = self._executor_for(node.node_type)
            if seat is None:
                continue
            try:
                await seat.start(node)
            except Exception:
                logger.exception(
                    "seat could not start node %s in project %s",
                    node.display_id,
                    self.project_id,
                )

    async def _executor_turns(self, situation: Situation) -> None:
        """Dispatch execution seats for live nodes that have not been served yet.

        A seat's turn is a monitoring or reading turn, not the work itself. A
        Worker's backend calls stay in Temporal and Research's reading happens
        in its own session; either way the turn is where a `request_action`, a
        `send_message` or a `submit_research_record` comes from. What it cannot
        do is decide: a request the contract does not permit becomes a deviation
        for Master.

        A turn that raised is recorded as served anyway, because it did look.
        The alternative is a failed runtime being asked again every poll, which
        spends a turn per round to learn the same thing — and the project's own
        stall detector is what ends a project whose agent cannot act.
        """
        turns = self._turn_list(situation)
        if not turns:
            return
        results = await asyncio.gather(
            *(seat.act(node, situation) for node, seat, _episode in turns),
            return_exceptions=True,
        )
        for (node, _seat, episode), result in zip(turns, results, strict=True):
            self._served.add(episode)
            if isinstance(result, Exception):
                logger.warning(
                    "seat turn failed: project=%s node=%s error=%s",
                    self.project_id,
                    node.display_id,
                    result,
                    exc_info=result,
                )

    def _turn_list(
        self, situation: Situation
    ) -> list[tuple[DagNode, ExecutorPort, tuple[str, str]]]:
        """Which live nodes owe a seat a turn, and the episode each turn is for.

        **A live node is a node in someone's hands**: RUNNING, which a backend
        or a research session is working on, or WAITING_EXTERNAL, which a lab is
        holding. Each of those is an *episode*, and the seat gets one turn per
        episode — enough to look at the contract and the record, ask RAVEL about
        anything the backend reported, or carry a research task on, and no more.

        Two things this deliberately does not do. It does not dispatch on an
        open deviation: a deviation is Master's turn, and the loop takes that
        turn first — routing it to a seat as well would be two roles acting on
        one question. And it does not re-ask on every poll: an agent that has
        already looked at an episode and found nothing to say has nothing to say
        one poll later, and a turn per round for the length of a run is a model
        bill rather than monitoring.

        The episode key is `(node, status)`, and it is the loop's own
        bookkeeping rather than a record — a scheduler's dedupe, like the round
        counter, and not scientific state. A restart costs one extra monitoring
        turn per live node; a turn that changes nothing changes nothing, which
        is why that is affordable.
        """
        live = {
            (node.node_id, node.status.value): node
            for node in situation.nodes
            if node.status in (NodeStatus.RUNNING, NodeStatus.WAITING_EXTERNAL)
        }
        # A node that left the live states has ended its episode; a node that
        # comes back to one has started a new episode and is served again.
        self._served &= live.keys()

        turns: list[tuple[DagNode, ExecutorPort, tuple[str, str]]] = []
        for episode, node in live.items():
            if episode in self._served:
                continue
            seat = self._executor_for(node.node_type)
            if seat is not None:
                turns.append((node, seat, episode))
        return turns

    def _executor_for(self, node_type: NodeType) -> ExecutorPort | None:
        """The seat that executes this kind of node, as the domain assigns it.

        Read off `NODE_EXECUTOR` conceptually rather than by importing the
        table: what the loop holds is three seats, and which one a node type
        belongs to is the same fact the domain states as `executor_role`. A
        node type with no seat here is one nothing in this wiring executes.
        """
        if node_type is NodeType.COMPUTATION:
            return self.compute_worker
        if node_type is NodeType.EXPERIMENT:
            return self.experimental_worker
        if node_type is NodeType.RESEARCH:
            return self.research
        return None

    @staticmethod
    def _ready_to_conclude(situation: Situation) -> bool:
        """Whether the project has stopped and only an ending is left.

        Every node has ended and none of the three powers has anything to do.
        Note what this is *not*: it is not "the project succeeded". The loop
        knows the project has stopped, which is a fact about the DAG; whether
        what stopped was a success is Master's to say.
        """
        return not situation.has_work_to_do and not situation.unfinished

    @staticmethod
    def _awaiting_final_verdict(situation: Situation) -> bool:
        """Whether Review owes a verdict on a result rather than on a plan.

        A node lands in REVIEWING when its run hands its result over, and it
        stays there until a verdict moves it. The loop reads that state but does
        not wait for it: the run committed its Execution Record before the
        transition, so a verdict submitted on the next round names the same
        record a verdict submitted now would.
        """
        return any(node.status is NodeStatus.REVIEWING for node in situation.nodes)

    def _tick(self) -> list[DagNode]:
        """Derive every node's readiness from the graph, in its own transaction.

        Its own transaction, and a short one, because it is a write: a node
        whose fan-in has settled moves, and the move is announced in the event
        stream like any other. Called before the read below rather than inside
        it, so the situation a round acts on is the situation *after* the tick —
        a node promoted this round is one the round may start.
        """
        with self.database.transaction() as session:
            return DagRepository(session, self.project_id).refresh_readiness()

    def _read(self) -> Situation:
        """Read the project's authoritative state, readiness included.

        The tick first, then a read in one transaction of its own. That order
        is the loop's and not `read_situation`'s, because the tick is a write
        and this is the method that knows a round begins here.
        """
        self._tick()
        return read_situation(
            self.database, self.project_id, executable=self._seated
        )

    @property
    def _seated(self) -> frozenset[NodeType]:
        """The node types this loop holds a seat for.

        Derived from `_executor_for` rather than written down a second time:
        the two are the same fact — which node types have somebody to begin
        them — and a list beside the mapping is a list that can disagree with
        it. A node type is in when the mapping answers with a seat, and the
        mapping is what the rest of the loop dispatches from.
        """
        return frozenset(
            node_type
            for node_type in NodeType
            if self._executor_for(node_type) is not None
        )

    @staticmethod
    def _unchanged(before: Situation, after: Situation) -> bool:
        """Whether a round moved the project at all.

        Compares the two things a turn can change: where the project is, and
        where its nodes are. Deliberately not the decisions, the events, or the
        deviations — a turn that only wrote one of those has not moved the
        project, and counting it as progress is how a loop stops noticing that
        it is stuck.
        """
        if before.project.status is not after.project.status:
            return False
        return [node.status for node in before.nodes] == [
            node.status for node in after.nodes
        ]

    def _end(
        self, situation: Situation, *, stalled: int = 0, halted: bool = False
    ) -> ProjectRun:
        return ProjectRun(
            project_id=self.project_id,
            status=situation.project.status,
            rounds=self._rounds,
            stalled_rounds=stalled,
            halted=halted,
        )
