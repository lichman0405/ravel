"""Turning a message somebody typed into one Master turn.

`docs/08` puts "natural-language conversation with Master" under the owner's
view and the Gateway under everything, so this module is the join: it reads the
project's authoritative state, hands Master the state and the person's words,
and returns what Master said.

**The question is written down before the turn is run.** Not after, and not in
the same transaction. A Master turn can take minutes and can fail; if the
record were written with the answer, a turn that died would leave no trace that
anyone had asked, and the transcript would show a person who said nothing
rather than a question that went unanswered. Those are different facts about a
project and the record has to be able to tell them apart.

**The answer is written down only if there is one.** A turn that did not
complete produced no reply, and inventing an empty message for it would make
"Master said nothing" indistinguishable from "Master was asked and never
answered". The transcript says the first; the turn's own report says the
second.

**The port is a protocol, not a runtime.** Nothing here imports the harness
beyond the two data types the turn produces, and the factory that reaches a
real runtime is built lazily — a Gateway that is never spoken to never starts
one. That is also what lets the TUI's end-to-end test drive a real Gateway and
a real database while Master is scripted, exactly as the headless loop's test
does: what is being tested is the Gateway's half, and a test that needed a
model in the loop would be testing the model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ravel.config import Settings
from ravel.domain.roles import AgentRole
from ravel.dsh.agents import MasterConversation
from ravel.dsh.pool import DshRuntimePool, create_pool
from ravel.execution.loop import Situation

__all__ = ["Answer", "HarnessMaster", "MasterFactory", "MasterPort", "harness_master"]


@dataclass(frozen=True, slots=True)
class Answer:
    """What one conversation turn produced, as a route needs it.

    Two fields rather than the harness's `TurnOutcome`, because those are the
    two the Gateway has any use for and the rest — the session identifier, the
    event log, the finish reason — is either harness-internal or something
    `docs/09` says must not reach a client.
    """

    text: str
    completed: bool


class MasterPort(Protocol):
    """One project's Master, reached by the thing a person said."""

    async def respond(self, situation: Situation, message: str) -> Answer:
        """Run one turn and return the reply."""
        ...


#: How the Gateway gets a project's Master. A factory rather than an object
#: because the conversation belongs to a project, and a Gateway serves several.
MasterFactory = Callable[[str], MasterPort]


@dataclass
class HarnessMaster:
    """A `MasterPort` over a real DSH session."""

    conversation: MasterConversation

    async def respond(self, situation: Situation, message: str) -> Answer:
        outcome = await self.conversation.respond(situation, message)
        return Answer(text=outcome.response.strip(), completed=outcome.completed)


def harness_master(settings: Settings) -> MasterFactory:
    """A factory that starts one runtime pool, the first time it is needed.

    The pool is created on first use rather than here, so importing the
    application does not require a DSH home to exist and a deployment that only
    ever serves reads never pays for one. The conversation per project is held
    for the same reason the pool holds its runtimes: Master's turns have
    continuity within a project for as long as the process lasts, and no longer.
    """
    holder: dict[str, DshRuntimePool] = {}
    conversations: dict[str, HarnessMaster] = {}

    def master_of(project_id: str) -> MasterPort:
        existing = conversations.get(project_id)
        if existing is not None and existing.conversation.live:
            return existing

        pool = holder.get("pool")
        if pool is None:
            pool = create_pool(settings)
            holder["pool"] = pool

        started = HarnessMaster(
            conversation=MasterConversation(
                pool=pool,
                project_id=project_id,
                role=AgentRole.MASTER,
                # Written into the runtime's brief once, at start. It says which
                # channel opened the scope, because that is a fact about the
                # authority the process was launched under and not something a
                # later turn may revise.
                brief={"channel": "user conversation"},
            )
        )
        conversations[project_id] = started
        return started

    return master_of
