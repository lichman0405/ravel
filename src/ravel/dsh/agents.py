"""The two agent powers, reached as DSH sessions.

`ProjectLoop` sequences a project and holds none of the powers it sequences.
Two of the three are agents — Master, who decides, and Review, who judges — and
this module is what connects them: a harness runtime scoped to
`(project_id, role)`, one session, and a turn per round.

**A turn's input is the situation, written out.** The agent is told what
PostgreSQL says: where the project is, which nodes are in which state, and what
is waiting on it. It is not told what to decide, because that would be this
module deciding, and it is not reminded of what a previous turn concluded,
because the authoritative state is the record and a session that has drifted
from it is a session whose memory is the wrong one. What the agent reads first
is the project state itself; this brief is what tells it where to look.

**A turn's effect is what the agent wrote, and nothing else.** Nothing here
reads the model's prose, counts its tool calls, or treats a confident sentence
as a decision. The tools the session holds write through the same services the
rest of RAVEL uses, so a turn that changed nothing changed nothing — and the
loop's stall detector is what notices, which is the one thing a turn's text
cannot be trusted to report.

**Sessions are disposable.** One is opened per scope and reused while its
runtime lives, so Master's turns have continuity within a project. Nothing
depends on that: the harness creates a session on first use and cannot reopen
one in a later process, so a runtime that is reaped costs the agent its
short-term memory and nothing else. Recovery reads the database.

**Two things drive a turn, and they share everything but the input.**
`HarnessAgent` is the loop's port: it is handed a `Situation` and decides what
the project needs. `MasterConversation` is a person talking: it is handed the
same `Situation` plus what somebody typed, and answers them. Both hold a
`RoleSession` — the runtime, the session on it, and how a turn is run — because
that part does not depend on why the turn is being run. What they do *not*
share is a prompt, and that is deliberate: the loop's turn exists to move a
project, and a person's turn exists to answer a person, and a single prompt
doing both would be one that half-fits each.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus
from ravel.domain.roles import AgentRole
from ravel.dsh.pool import DshRuntimePool
from ravel.dsh.runtime import RoleRuntime, TurnOutcome
from ravel.execution.loop import Situation

logger = logging.getLogger(__name__)

__all__ = ["HarnessAgent", "MasterConversation", "RoleSession", "WorkerAgent"]


@dataclass
class RoleSession:
    """One role's session in one project, on the pool's runtime.

    The half of an agent that has nothing to do with what the turn is *for*:
    which runtime this scope has, which session on it is ours, and how a turn
    is run on it. Two things drive turns — the project loop, and a person
    talking to Master — and both need exactly this much and no more.
    """

    pool: DshRuntimePool
    project_id: str
    role: AgentRole
    #: Written into the runtime's `brief.json` when the runtime starts, which
    #: happens once per process: it describes the authority the agent was
    #: launched under, and rewriting it under a live agent would let the agent's
    #: view drift from its grant.
    brief: dict[str, Any] | None = None
    _runtime: RoleRuntime | None = field(default=None, init=False, repr=False)
    _session_id: str | None = field(default=None, init=False, repr=False)
    _turns: int = field(default=0, init=False, repr=False)

    @property
    def turns(self) -> int:
        """How many turns this session has run, for health reporting."""
        return self._turns

    @property
    def session_id(self) -> str | None:
        """The live session's identifier, or `None` before the first turn."""
        return self._session_id

    @property
    def live(self) -> bool:
        """Whether a runtime is currently held for this scope."""
        return self._runtime is not None and not self._runtime.is_closed

    def close(self) -> None:
        """Shut down this scope's runtime, as a project ending does."""
        self.pool.close_scope(self.project_id, self.role)
        self._runtime = None
        self._session_id = None

    async def run(self, prompt: str) -> TurnOutcome:
        """Run one turn with this text as its input, off the event loop's thread.

        Off the thread rather than on it: the SDK's turn call blocks until the
        session goes idle, and a caller that could not do anything else while
        an agent thought would be one that cannot poll the work it already
        started.
        """
        runtime, session_id = self._session()
        outcome: TurnOutcome = await asyncio.to_thread(runtime.run_turn, session_id, prompt)
        self._turns += 1
        return outcome

    def _session(self) -> tuple[RoleRuntime, str]:
        """The live runtime and this agent's session on it.

        A session identifier only means anything to the process that minted it,
        so one is minted whenever the runtime changes — which is what a reap
        looks like from here. The binding registry records the same thing from
        the other side: it drops a scope's bindings when the scope's runtime is
        closed, because a binding to a dead session is a falsified record.
        """
        runtime = self.pool.runtime(self.project_id, self.role, self.brief)
        session_id = self._session_id
        if session_id is None or runtime is not self._runtime:
            session_id = runtime.new_session_id()
            self._runtime = runtime
            self._session_id = session_id
            self.pool.bindings.bind(session_id, self.project_id, self.role)
        return runtime, session_id


@dataclass
class HarnessAgent(RoleSession):
    """One role's turns in one project, as the loop's ports see it.

    Implements `MasterPort` and `ReviewPort` with the same method, because the
    difference between the two is the role the runtime booted with — its
    behavioral contract, its tool roster, and the authority its MCP server
    checks — rather than anything the loop does with the result. A second class
    would be this one with a different string in it.
    """

    async def act(self, situation: Situation) -> None:
        """Run one turn, with the situation as its input."""
        outcome = await self.run(self._prompt(situation))
        if not outcome.completed:
            # Not raised: a turn that failed is a round that changed nothing,
            # and the loop counts those. Raising here would end a project
            # because a model timed out on one round.
            logger.warning(
                "harness turn did not complete: project=%s role=%s session=%s "
                "finish_reason=%s",
                self.project_id,
                self.role.value,
                self.session_id,
                outcome.finish_reason,
            )
        logger.info(
            "harness turn: project=%s role=%s turns=%d tool_calls=%d completed=%s",
            self.project_id,
            self.role.value,
            self._turns,
            len(outcome.tool_calls),
            outcome.completed,
        )

    # ── The turn's input ────────────────────────────────────────────────────

    def _prompt(self, situation: Situation) -> str:
        """What the agent is told at the start of its turn."""
        match self.role:
            case AgentRole.MASTER:
                return self._master_prompt(situation)
            case AgentRole.REVIEW:
                return self._review_prompt(situation)
        raise ValueError(
            f"{self.role.value} is not a role the project loop calls; the loop "
            "sequences Master and Review, and workers are reached by their runs"
        )

    def _master_prompt(self, situation: Situation) -> str:
        project = situation.project
        lines = [
            f"A turn of yours. Project {project.display_id} ({project.status.value}).",
            f"Objective: {project.objective}",
            "",
            "* What the project state holds",
            *self._census(situation),
            "",
            "* What is waiting on you",
        ]
        waiting = self._waiting_on_master(situation)
        lines.extend(waiting or ["Nothing in the DAG is waiting on a decision."])

        if situation.open_deviations:
            lines.extend(["", "* The escalations themselves"])
            lines.extend(self._deviation_lines(situation))

        if not situation.nodes:
            lines.extend(
                [
                    "",
                    "The DAG is empty: no work has been planned. The project's first "
                    "stage is yours to commit — or, if there is nothing worth "
                    "running, yours to end before it starts.",
                ]
            )
        elif not situation.unfinished and not situation.has_work_to_do:
            lines.extend(
                [
                    "",
                    "Every node has ended and nothing is left to run. Whether this "
                    "project has succeeded, failed, or cannot be concluded either "
                    "way is yours to say, and the record is the only thing a reader "
                    "will have.",
                ]
            )
        lines.extend(
            [
                "",
                "Read the authoritative state before you act, and record a Decision "
                "Record for anything you change. Do not report a decision you did "
                "not write: a turn that changes nothing is read as one.",
            ]
        )
        return "\n".join(lines)

    def _review_prompt(self, situation: Situation) -> str:
        pre_flight = situation.needs_pre_flight
        final = tuple(
            node for node in situation.nodes if node.status is NodeStatus.REVIEWING
        )
        lines = [
            f"A turn of yours. Project {situation.project.display_id} "
            f"({situation.project.status.value}).",
            f"Objective: {situation.project.objective}",
            "",
        ]
        if pre_flight:
            lines.append("* Waiting on a pre-flight checkpoint")
            lines.extend(
                f"- {node.display_id} {node.node_type.value} ({node.status.value}): "
                f"{node.objective}"
                f" [acceptance {node.acceptance_contract_ref}, "
                f"contract {node.execution_contract_ref}]"
                for node in pre_flight
            )
            lines.extend(
                [
                    "Nothing runs until each of these has a PRE_RUN verdict, so a "
                    "node held back is a node waiting on this turn.",
                ]
            )
        if final:
            lines.append("* Waiting on a final verdict")
            lines.extend(self._final_review_lines(final))
        if not pre_flight and not final:
            lines.append(
                "No node is waiting on a checkpoint. Say so and stop rather than "
                "reviewing work that has not asked for it."
            )
        lines.extend(
            [
                "",
                "Judge against the frozen criteria the node carries, criterion by "
                "criterion, and diagnose rather than only scoring. Your verdict is "
                "recorded as it is written; nothing here reads your prose.",
            ]
        )
        return "\n".join(lines)

    # ── The turn's input, in pieces ─────────────────────────────────────────

    @staticmethod
    def _census(situation: Situation) -> list[str]:
        counts: dict[str, int] = {}
        for node in situation.nodes:
            counts[node.status.value] = counts.get(node.status.value, 0) + 1
        if not counts:
            return ["The DAG is empty: no work has been planned yet."]
        return [
            f"{count} {status}" for status, count in sorted(counts.items())
        ] + [f"{len(situation.nodes)} node(s) in total"]

    @staticmethod
    def _waiting_on_master(situation: Situation) -> list[str]:
        waiting: list[str] = []
        for node in situation.nodes:
            if node.status is NodeStatus.WAITING_DECISION:
                waiting.append(
                    f"- {node.display_id} stopped and is waiting for a decision "
                    f"about its contract"
                )
            elif node.status is NodeStatus.BLOCKED:
                waiting.append(
                    f"- {node.display_id} is BLOCKED: its dependencies can no "
                    f"longer all be satisfied, so it can never run as planned"
                )
        names = {node.node_id: node.display_id for node in situation.nodes}
        for deviation in situation.open_deviations:
            waiting.append(
                f"- an unanswered escalation on "
                f"{names.get(deviation.node_id, deviation.node_id)}: a worker "
                f"asked for {deviation.requested_action!r}"
            )
        return waiting

    @staticmethod
    def _deviation_lines(situation: Situation) -> list[str]:
        names = {node.node_id: node.display_id for node in situation.nodes}
        return [
            f"- {names.get(deviation.node_id, deviation.node_id)}, raised by "
            f"{deviation.raised_by}: the worker asked for "
            f"{deviation.requested_action!r}, and the contract refused it "
            f"because {deviation.description}"
            for deviation in situation.open_deviations
        ]

    @staticmethod
    def _final_review_lines(final: tuple[DagNode, ...]) -> list[str]:
        lines: list[str] = []
        for node in final:
            artifacts = ", ".join(node.artifact_refs) or "nothing recorded"
            lines.append(
                f"- {node.display_id} {node.node_type.value}: {node.objective} "
                f"[acceptance {node.acceptance_contract_ref}, execution "
                f"{node.execution_contract_ref}, artifacts: {artifacts}]"
            )
        lines.append(
            "Each has handed its result over and is holding there until a verdict "
            "moves it. A PASS lets the branch behind it proceed; FAIL and PARTIAL "
            "are outcomes Master replans around, and neither is a reason to soften "
            "the criteria."
        )
        return lines


@dataclass
class WorkerAgent(RoleSession):
    """A task-scoped Worker reached as a DSH session.

    The loop hands this agent one in-flight node at a time. The agent reads the
    frozen Execution Contract and the record of what has happened, then uses the
    Worker tools to ask the contract about anything the backend or operator
    requested, or to send a message to the lab. It does not judge results, does
    not change the plan, and does not answer scientific questions itself.

    The actual backend calls stay in Temporal activities: this agent decides
    what is permitted and what must be escalated; durable execution carries out
    the work and survives this agent being restarted.
    """

    async def act(self, node: DagNode, situation: Situation) -> None:
        """Run one monitoring or communication turn for the given node."""
        outcome = await self.run(self._prompt(node, situation))
        if not outcome.completed:
            logger.warning(
                "worker turn did not complete: project=%s role=%s node=%s session=%s "
                "finish_reason=%s",
                self.project_id,
                self.role.value,
                node.display_id,
                self.session_id,
                outcome.finish_reason,
            )
        logger.info(
            "worker turn: project=%s role=%s node=%s turns=%d tool_calls=%d completed=%s",
            self.project_id,
            self.role.value,
            node.display_id,
            self._turns,
            len(outcome.tool_calls),
            outcome.completed,
        )

    def _prompt(self, node: DagNode, situation: Situation) -> str:
        """What the Worker is told at the start of its turn."""
        project = situation.project
        lines = [
            f"A turn of yours. Project {project.display_id} ({project.status.value}).",
            f"You are serving as {self.role.display_name} for one task.",
            "",
            f"Node: {node.display_id} ({node.node_type.value})",
            f"Node ID: {node.node_id}",
            f"Status: {node.status.value}",
            f"Objective: {node.objective}",
            f"Execution contract: {node.execution_contract_ref}",
        ]
        if node.status is NodeStatus.WAITING_EXTERNAL:
            lines.extend(
                [
                    "",
                    "The task is waiting for something outside RAVEL. If you need to "
                    "communicate with the lab, do so through the contract and your tools.",
                ]
            )
        lines.extend(
            [
                "",
                "What to do this turn:",
                "1. Read the frozen Execution Contract for this node.",
                "2. Read the record of what has happened so far.",
                "3. If the backend or operator has asked for something, use request_action "
                "to ask the contract whether it is permitted. RAVEL answers; you do not.",
                "4. If you are the Experimental Worker and need to send a message within "
                "the contract, use send_message.",
                "5. If the task is progressing and needs no intervention, say so and stop.",
                "",
                "Do not change the plan, do not judge whether a result passes, and do not "
                "answer a scientific question yourself. A request the contract does not "
                "explicitly permit is an escalation to Master.",
            ]
        )
        return "\n".join(lines)


@dataclass
class MasterConversation(RoleSession):
    """A person talking to Master, with the project state in front of it.

    `docs/08` gives the owner "natural-language conversation with Master", and
    this is the whole of what that is: a turn whose input is what somebody
    typed, preceded by the same authoritative state every other Master turn
    begins with. The user's words are not a command and are not parsed — they
    are the one part of the situation a person supplies, and Master decides
    what, if anything, to do about them. A sentence asking for a node to be
    added changes nothing unless Master writes the decision and the node, which
    is the same bar the loop's turns are held to.

    **The authority is the session's, not the message's.** Nothing here
    elevates the turn because a human asked for it. Master's tools check the
    same scope-bound grant they check on any other turn, so an owner who wants
    something outside the Authority Envelope gets a conversation about it —
    or an approval request — rather than a node.

    **What comes back is text, and only text.** The reply is recorded as what
    Master said. Anything Master *did* is already in PostgreSQL, written
    through the tools, and is read from there rather than from a transcript: a
    sentence describing a change that was never made is a sentence, and the
    record is the record.
    """

    async def respond(self, situation: Situation, message: str) -> TurnOutcome:
        """Run one turn with the person's message as its input."""
        outcome = await self.run(self._prompt(situation, message))
        if not outcome.completed:
            # Not raised, for the reason `HarnessAgent.act` does not raise it:
            # a turn that failed is a turn that produced no answer, and the
            # transcript should show one that did not finish rather than the
            # request failing after the question was already recorded.
            logger.warning(
                "master conversation turn did not complete: project=%s session=%s "
                "finish_reason=%s",
                self.project_id,
                self.session_id,
                outcome.finish_reason,
            )
        logger.info(
            "master conversation: project=%s turns=%d tool_calls=%d completed=%s",
            self.project_id,
            self._turns,
            len(outcome.tool_calls),
            outcome.completed,
        )
        return outcome

    def _prompt(self, situation: Situation, message: str) -> str:
        """The situation, and then what the person said.

        The order is the point. State first, message second, because the
        message is the only thing here a person wrote and it must not read as
        an instruction that outranks the record — Master answers *this project*
        as it stands, not the project the message assumes.
        """
        project = situation.project
        lines = [
            f"Somebody is talking to you. Project {project.display_id} "
            f"({project.status.value}).",
            f"Objective: {project.objective}",
            "",
            "* What the project state holds",
            *HarnessAgent._census(situation),
        ]
        waiting = HarnessAgent._waiting_on_master(situation)
        if waiting:
            lines.extend(["", "* What is waiting on you", *waiting])
        if situation.open_deviations:
            lines.extend(
                ["", "* The escalations themselves", *HarnessAgent._deviation_lines(situation)]
            )
        lines.extend(
            [
                "",
                "* What they said",
                message,
                "",
                "Answer them. What you change, you change through your tools, and "
                "you record a Decision Record for anything you change — a reply "
                "that describes a change you did not make is a sentence, not a "
                "change. If answering honestly means saying that the project state "
                "is not what they think it is, say that.",
            ]
        )
        return "\n".join(lines)
