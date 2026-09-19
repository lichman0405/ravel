"""The project loop: who acts next, and how RAVEL knows the project is stuck.

A project is not one workflow. It is a DAG whose nodes run when their
dependencies are settled, reviews that clear them to run and accept what they
produce, and Master's decisions when a Worker stops or the work is over. Each
of those is durable and already implemented; what is missing from them is the
*sequencing* — and the sequencing is this file.

**The loop holds none of the three powers.** It does not decide what work
exists, it does not decide whether a result is acceptable, and it does not
execute anything. Those arrive as three ports (`MasterPort`, `ReviewPort`,
`ExecutionPort`): the first two are DSH sessions in production, and the third is
Temporal, which is what runs a node. What the loop does is read PostgreSQL — the
authoritative state — work out which of the three has something to do, and hand
it the situation.

**What the loop does do is start runs.** That is deliberately not one of the
three powers, and the distinction is worth stating: a node is READY because the
DAG's own transitions put it there, with its contracts frozen and its pre-flight
review passed. Starting it executes a plan Master already recorded; it does not
choose one. The loop never creates, cancels, or re-points a node, never writes a
review, and never records a decision — the ports do, through the same services
their MCP tools wrap, so nothing here is a second path to those writes.

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
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, ProjectStatus, ReviewOutcome
from ravel.domain.execution import DeviationRecord
from ravel.domain.project import Project
from ravel.domain.state_machines import TERMINAL_PROJECT_STATUSES, requires_frozen_criteria
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DeviationRepository

__all__ = [
    "ExecutionPort",
    "LoopHalted",
    "MasterPort",
    "ProjectLoop",
    "ProjectRun",
    "ReviewPort",
    "Situation",
]


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
    def runnable(self) -> tuple[DagNode, ...]:
        """READY nodes cleared to run, which is what the runtime may start.

        Cleared means exactly what `DagNode.can_enter_running` means by it: a
        node type that owes a pre-flight review has one, and it passed. A READY
        node without one is `needs_pre_flight` instead — handing it to the
        runtime would produce a refused transition inside an activity, which
        reads as a runtime fault rather than as the wait it is.
        """
        return tuple(
            node
            for node in self.nodes
            if node.status is NodeStatus.READY and self._cleared(node)
        )

    @property
    def needs_pre_flight(self) -> tuple[DagNode, ...]:
        """READY nodes still waiting on the checkpoint that clears them."""
        return tuple(
            node
            for node in self.nodes
            if node.status is NodeStatus.READY and not self._cleared(node)
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

        Three ways a project waits on Master, and they arrive from different
        places: a Worker that stopped (`WAITING_DECISION`, which is what raising
        a deviation moves the node to), a question nobody has answered yet (the
        deviation itself — the node can be terminal while the question is not),
        and a branch that can never be satisfied. All three are the same fact
        from the loop's side: nothing here may proceed.
        """
        if self.open_deviations:
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
class ExecutionPort(Protocol):
    """Execution: whatever actually runs a node."""

    async def start(self, node: DagNode, *, actor_id: str) -> None:
        """Start the node's run, or do nothing if one is already under way."""
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
    execution: ExecutionPort
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
    _rounds: int = field(default=0, init=False, repr=False)

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
                # project for the crime of doing its work slowly.
                if not situation.in_flight:
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
        pre-flight checkpoint, the run, the final verdict, and finally the
        ending. A node cannot be in two of those states at once, so the order is
        a convenience rather than a precedence — but it is the order a reader
        expects, and a loop that reviewed before clearing would be describing a
        path the DAG refuses.

        The last branch is the one that ends the project. It fires only when the
        situation has nothing else in it at all — no run in flight, nothing
        waiting on Review, no branch waiting on Master — which is exactly the
        position from which an ending is a statement about results rather than a
        hope. Master may still refuse it (A20's checks are in the service, next
        to the write); the loop does not pre-empt them, because a loop that
        decided when an ending was justified would be holding the decision.
        """
        if situation.needs_decision:
            await self.master.act(situation)
            return
        if situation.needs_pre_flight:
            await self.review.act(situation)
        for node in situation.runnable:
            await self.execution.start(node, actor_id="scheduler")
        if self._awaiting_final_verdict(situation):
            await self.review.act(situation)
        if self._ready_to_conclude(situation):
            await self.master.act(situation)

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

        One transaction, and a read-only one: the loop must never hold a write
        transaction open across an agent turn, which can take minutes.
        """
        self._tick()
        with self.database.read_only() as session:
            project = ProjectRegistry(session).get(self.project_id)
            dag = DagRepository(session, self.project_id)
            nodes = tuple(dag.nodes())
            return Situation(
                project=project,
                nodes=nodes,
                pre_run={
                    node.node_id: dag.latest_pre_run_outcome(node.node_id)
                    for node in nodes
                },
                open_deviations=tuple(
                    DeviationRepository(session, self.project_id).open()
                ),
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
