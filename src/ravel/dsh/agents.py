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

__all__ = ["HarnessAgent"]


@dataclass
class HarnessAgent:
    """One role's turns in one project, as the loop's ports see it.

    Implements `MasterPort` and `ReviewPort` with the same method, because the
    difference between the two is the role the runtime booted with — its
    behavioral contract, its tool roster, and the authority its MCP server
    checks — rather than anything the loop does with the result. A second class
    would be this one with a different string in it.
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
        """How many turns this port has run, for health reporting."""
        return self._turns

    @property
    def session_id(self) -> str | None:
        """The live session's identifier, or `None` before the first turn."""
        return self._session_id

    async def act(self, situation: Situation) -> None:
        """Run one turn, with the situation as its input.

        Off the event loop's thread rather than on it: the SDK's turn call
        blocks until the session goes idle, and a project loop that could not
        do anything else while an agent thought would be a loop that cannot
        poll the work it already started.
        """
        runtime, session_id = self._session()
        prompt = self._prompt(situation)
        outcome: TurnOutcome = await asyncio.to_thread(
            runtime.run_turn, session_id, prompt
        )
        self._turns += 1
        if not outcome.completed:
            # Not raised: a turn that failed is a round that changed nothing,
            # and the loop counts those. Raising here would end a project
            # because a model timed out on one round.
            logger.warning(
                "harness turn did not complete: project=%s role=%s session=%s "
                "finish_reason=%s",
                self.project_id,
                self.role.value,
                session_id,
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

    # ── The session ─────────────────────────────────────────────────────────

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

    def close(self) -> None:
        """Shut down this scope's runtime, as a project ending does."""
        self.pool.close_scope(self.project_id, self.role)
        self._runtime = None
        self._session_id = None

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
