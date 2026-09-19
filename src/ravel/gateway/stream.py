"""The live view of a project, read from the record rather than from a bus.

`docs/08` asks the Gateway's WebSocket for three things — the event stream, the
status, and Master's streaming response — and `docs/08` §7 asks that a client
which reconnects can replay from the last sequence it rendered. This module is
the first two.

**The stream tails the outbox.** It does not subscribe to anything, because
there is nothing in this process to subscribe to: events are written by
whichever transaction makes the change, and in a real deployment that is a
Temporal worker in a different process from the Gateway. A `dict` of
`asyncio.Queue`s here would show a client the events *this* process happened to
write, which is a stream that silently omits most of the project — and a live
view that is wrong in a way nobody can see is worse than no live view.
PostgreSQL is where the events are, so PostgreSQL is what the stream reads.

**Missing an event is not a state a client can reach.** Nothing prunes the
event table in V0, so a client's `after_seq` is always still replayable and the
answer to "am I behind?" is yes-or-no rather than "yes, and the rest is gone".
`ProjectEvent.as_wire` already calls its output "a JSON-safe view for the
WebSocket projection"; this is the projection it names.

**Nothing here reaches the harness.** The frames are domain events carrying
domain payloads, which is what makes the socket safe to hand a member: an event
says a node moved and who moved it, and the session that decided to move it is
not part of the record. `docs/09` says DSH session identifiers do not leave the
runtime, and the frame's shape is what keeps that true — the top level is
`as_wire`'s fixed eight fields, so nothing can reach it that `as_wire` does not
name, and the payload is written by the service that owns the change.

That is a property of the writers, not of this module, and it is worth being
exact about it: the vocabulary in `schemas/project_events.yaml` has
`AGENT_SESSION_STARTED` and `AGENT_SESSION_ENDED`, and V0 emits neither. If one
is ever emitted with a session identifier in its payload, that identifier will
be on this socket. Whoever writes it should put the *role* in the payload and
nothing else, for the reason those events exist at all: what a reader of the
stream needs to know is that Master started thinking, not which harness session
has the thought.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from ravel.domain.events import ProjectEvent
from ravel.state.database import Database
from ravel.state.outbox import events_since, last_event_seq
from ravel.state.repositories.projects import ProjectRegistry

#: How many events one read returns. The replay after a reconnect walks the
#: backlog a page at a time rather than in one query, because a project that
#: has been running for a month has a stream whose length is a fact about the
#: project and not about what the client asked for.
PAGE = 500


@dataclass(frozen=True, slots=True)
class Cadence:
    """How often the stream looks, and how often it says something.

    Two numbers rather than one because they answer different questions.
    `poll_seconds` is how long a change can sit in PostgreSQL before a watching
    client sees it; `heartbeat_seconds` is how long a client may hear nothing
    before it should conclude the connection is dead rather than the project
    quiet. Neither is a timeout — a project that is paused emits nothing for
    days, and that is not a broken socket.
    """

    poll_seconds: float = 0.5
    heartbeat_seconds: float = 15.0


DEFAULT_CADENCE = Cadence()


def read_anchor(database: Database, project_id: str) -> dict[str, Any]:
    """What the client needs before the first event: where the stream is now.

    `head_seq` is the point of it. A client that reconnects holding sequence
    *n* learns from the anchor whether it is behind, and by how much, before it
    has received a single event — which is what lets it decide whether to
    replay into the view it already has or to redraw from scratch.
    """
    with database.read_only() as session:
        project = ProjectRegistry(session).get(project_id)
        head = last_event_seq(session, project_id)
    return {
        "type": "anchor",
        "project_id": project_id,
        "display_id": project.display_id,
        "status": project.status.value,
        "head_seq": head,
    }


def read_page(database: Database, project_id: str, after_seq: int) -> list[ProjectEvent]:
    """One page of events after `after_seq`, oldest first."""
    with database.read_only() as session:
        return events_since(session, project_id, after_seq=after_seq, limit=PAGE)


class EventFeed:
    """One client's view of a project's stream: anchor, replay, then follow.

    A plain async generator over frames, with no FastAPI in it, so that what the
    socket does with them — send them, and stop when the client goes away — is
    the only thing the route has to get right. A test drives `frames` directly
    against a real database and asserts that an event committed by somebody
    else arrives, which is the property the route cannot demonstrate on its own.
    """

    def __init__(
        self,
        *,
        database: Database,
        project_id: str,
        after_seq: int = 0,
        cadence: Cadence | None = None,
    ) -> None:
        self.database = database
        self.project_id = project_id
        self.after_seq = max(0, after_seq)
        self.cadence = cadence or DEFAULT_CADENCE

    def anchor(self) -> dict[str, Any]:
        """The projection the client starts from."""
        return read_anchor(self.database, self.project_id)

    async def frames(self) -> AsyncGenerator[dict[str, Any], None]:
        """The anchor, then every event after it, then each one as it lands.

        The reads run off the event loop's thread, and that is not
        fastidiousness: this generator is alive for as long as a client is
        watching, and a synchronous query here would stall every other request
        the Gateway is serving — including the Master turn the person is
        waiting on while they watch.

        An `AsyncGenerator` rather than an `AsyncIterator` in the annotation
        because a caller that stops reading — a socket whose peer went away —
        has to `aclose` it, and only the narrower type has that.
        """
        anchor = await asyncio.to_thread(self.anchor)
        # A client may claim to hold more than exists, and the claim is not
        # believed: a position past the head is not a position in this stream,
        # so it is read as "start again" rather than clamped down to the head.
        # Clamping is the tempting version and it is the wrong one — it settles
        # the client at the head and leaves it silently missing everything
        # before, which is the exact failure the cursor exists to prevent.
        # Re-sending from the beginning costs one replay, once, in a case that
        # is already anomalous, and cannot leave the client wrong.
        if self.after_seq > anchor["head_seq"]:
            self.after_seq = 0
        yield anchor

        last_spoke = time.monotonic()
        while True:
            page = await asyncio.to_thread(
                read_page, self.database, self.project_id, self.after_seq
            )
            if page:
                last_spoke = time.monotonic()
                for event in page:
                    # `max` rather than assignment: the page is ordered, so the
                    # last one wins, and writing it this way says that a
                    # sequence never goes backwards even if a page were not.
                    self.after_seq = max(self.after_seq, event.seq or 0)
                    yield {"type": "event", **event.as_wire()}
                # Look again straight away rather than sleeping: a backlog is
                # drained at the speed of the database, and a burst of events
                # should arrive as a burst instead of one per poll.
                continue

            if time.monotonic() - last_spoke >= self.cadence.heartbeat_seconds:
                last_spoke = time.monotonic()
                yield {"type": "heartbeat", "head_seq": self.after_seq}
                continue

            await asyncio.sleep(self.cadence.poll_seconds)


__all__ = ["DEFAULT_CADENCE", "PAGE", "Cadence", "EventFeed", "read_anchor", "read_page"]
