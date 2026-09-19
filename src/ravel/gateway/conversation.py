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
beyond the two data types the turn produces, and the adapter below is the only
class that knows a real runtime exists. What *starts* one is
`ravel.gateway.runtime`, on the other side of this port: a Gateway that is
never spoken to never starts a runtime, and a test can hand the Gateway a
scripted Master without a model being involved — which is what lets the TUI's
end-to-end test drive a real Gateway and a real database while Master is
scripted, exactly as the headless loop's test does. What is being tested is the
Gateway's half, and a test that needed a model in the loop would be testing the
model.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ravel.dsh.agents import MasterConversation
from ravel.execution.loop import Situation

__all__ = ["Answer", "HarnessMaster", "MasterFactory", "MasterPort"]


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
