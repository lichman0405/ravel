"""The live view, driven directly against a real database.

The feed is where the stream's behaviour lives — the anchor, the replay, the
poll, the heartbeat — and testing it here rather than through a socket is not a
convenience. The property that matters most is one a socket test cannot show:
an event committed by a *different process* arrives. Here the test writes it
from its own thread while the feed is running, which is the same thing that
happens when a Temporal worker moves a node.

The cadence is millisecond-scale so the suite runs rather than sleeps. That is
what the parameter is for; nothing about the frames depends on which numbers
are used.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from tests.integration.gateway.conftest import account

from ravel.domain.enums import ProjectStatus, UserRole
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.project import Project
from ravel.gateway.stream import PAGE, Cadence, EventFeed
from ravel.state.database import Database
from ravel.state.outbox import emit
from ravel.state.repositories.projects import ProjectRegistry

pytestmark = pytest.mark.integration

#: Fast enough that a test polls several times a second, slow enough that the
#: loop is not a busy-wait. The heartbeat is deliberately shorter than the
#: timeout a test uses, so "nothing came" is observable without waiting on the
#: production fifteen seconds.
FAST = Cadence(poll_seconds=0.01, heartbeat_seconds=0.05)

#: Creating a project emits `PROJECT_CREATED`, so a fresh project's stream is
#: not empty — sequence 1 is the fact that the project exists. Every count
#: below starts from here rather than from zero, because a stream that began at
#: zero would be a stream that had lost the first thing that happened.
BORN = 1


class Watcher:
    """A feed driven by hand, so a test can write between frames.

    An async generator consumed one frame at a time rather than collected in
    one call, because the interesting case is what arrives *after* the feed has
    started — and a `list(...)` of it would never return.
    """

    def __init__(self, feed: EventFeed) -> None:
        self._frames = feed.frames()

    async def next(self, timeout: float = 5.0) -> dict[str, Any]:
        """The next frame, or a failure naming the wait rather than hanging."""
        async with asyncio.timeout(timeout):
            return await anext(self._frames)

    async def close(self) -> None:
        await self._frames.aclose()

    async def __aenter__(self) -> Watcher:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        await self.close()


def _note(database: Database, project: Project, body: str) -> None:
    """Write one event, the way any service would: in its own transaction.

    A raw `emit` rather than a service call, because the stream's contract is
    "whatever is in the outbox" and this is the narrowest way to hold it to
    that. One test below does go through a service, so the two together say
    both that the feed reads the outbox and that real changes reach it.
    """
    with database.transaction() as session:
        emit(
            session,
            project_id=project.project_id,
            event_type=ProjectEventType.MASTER_CHECKPOINTED,
            actor_type=ActorType.AGENT,
            actor_id="master",
            payload={"note": body},
        )


def behind(database: Database, project: Project, after_seq: int = 0) -> EventFeed:
    """A feed reading from `after_seq` — the client that has some catching up."""
    return EventFeed(
        database=database, project_id=project.project_id, after_seq=after_seq, cadence=FAST
    )


def following(database: Database, project: Project) -> EventFeed:
    """A feed caught up to now, so only what happens next is delivered.

    What a client that has just drawn the current projection asks for, and what
    a test wants when it is about to write something and watch it arrive.
    """
    head = EventFeed(database=database, project_id=project.project_id).anchor()["head_seq"]
    return behind(database, project, after_seq=head)


# ── The anchor ──────────────────────────────────────────────────────────────


async def test_the_first_frame_says_where_the_stream_is(
    database: Database, project: Project
) -> None:
    """A client that reconnects learns whether it is behind before it reads.

    `head_seq` is the whole point of the anchor: a client holding sequence *n*
    can compare it against what it has and decide whether to replay into the
    view it already drew or to redraw. Without it the only way to find out is
    to wait and guess.
    """
    _note(database, project, "first")
    _note(database, project, "second")

    anchor = EventFeed(database=database, project_id=project.project_id).anchor()

    assert anchor["type"] == "anchor"
    assert anchor["project_id"] == project.project_id
    assert anchor["display_id"] == project.display_id
    assert anchor["status"] == project.status.value
    assert anchor["head_seq"] == BORN + 2


async def test_a_project_nothing_has_been_done_to_holds_only_its_own_creation(
    database: Database, project: Project
) -> None:
    """An anchor is never zero for a project that exists, and that is right.

    The stream's first event is the project coming into being. It is not a
    detail of the fixture: it is what makes the head sequence an honest position
    rather than an offset, and a client that drew a fresh project and then asked
    for what it had missed would otherwise be told it had missed nothing.
    """
    anchor = EventFeed(database=database, project_id=project.project_id).anchor()

    assert anchor["head_seq"] == BORN
    assert anchor["status"] == ProjectStatus.CREATED.value


# ── Replay ──────────────────────────────────────────────────────────────────


async def test_a_reconnecting_client_is_sent_what_it_missed(
    database: Database, project: Project
) -> None:
    """`after_seq` is the last sequence the client rendered, and the stream
    resumes strictly after it — so a client that reconnects holding 3 is sent 4
    and onward and is never sent 3 twice."""
    for body in ("a", "b", "c"):
        _note(database, project, body)

    async with Watcher(behind(database, project, after_seq=BORN + 2)) as watcher:
        assert (await watcher.next())["head_seq"] == BORN + 3
        fourth = await watcher.next()

    assert fourth["type"] == "event"
    assert fourth["seq"] == BORN + 3
    assert fourth["payload"] == {"note": "c"}


async def test_a_client_that_claims_more_than_exists_is_sent_it_anyway(
    database: Database, project: Project
) -> None:
    """A client holding a sequence beyond the head is sent the stream again.

    The claim is not believed, and the alternative reading — settle the client
    at the head and send it nothing — is the one that fails quietly: the client
    asked for a position this project does not have, which means its cursor is
    wrong, and leaving it there would mean it never sees what it missed and
    never finds out.
    """
    _note(database, project, "only")

    async with Watcher(behind(database, project, after_seq=999)) as watcher:
        assert (await watcher.next())["head_seq"] == BORN + 1
        first = await watcher.next()
        second = await watcher.next()

    assert [first["seq"], second["seq"]] == [BORN, BORN + 1]


async def test_a_backlog_is_drained_a_page_at_a_time(
    database: Database, project: Project
) -> None:
    """More events than fit in one read are all delivered, in order.

    The page size exists because the stream only grows, so an unbounded read is
    a request whose cost is a fact about the project rather than about what the
    caller asked for. What it must not do is drop the rest.
    """
    for index in range(PAGE + 7):
        _note(database, project, f"note-{index}")

    async with Watcher(behind(database, project)) as watcher:
        await watcher.next()
        sequences = [(await watcher.next())["seq"] for _ in range(PAGE + 7)]

    assert sequences == list(range(BORN, BORN + PAGE + 7))


# ── Following ───────────────────────────────────────────────────────────────


async def test_an_event_written_by_somebody_else_arrives(
    database: Database, project: Project
) -> None:
    """The reason the stream tails PostgreSQL instead of a queue in this process.

    The change is made by the test's own thread while the feed is already
    running, which is what a worker in another process looks like from here —
    and the feed sees it because the record is what it reads, not because it
    was told.
    """
    async with Watcher(following(database, project)) as watcher:
        assert (await watcher.next())["type"] == "anchor"

        _note(database, project, "while you were watching")

        frame = await watcher.next()

    assert frame["type"] == "event"
    assert frame["seq"] == BORN + 1
    assert frame["actor_id"] == "master"
    assert frame["payload"] == {"note": "while you were watching"}


async def test_a_real_change_reaches_the_stream(
    database: Database, project: Project
) -> None:
    """A service writing through its own repository, not a raw `emit`.

    The test above holds the feed to the outbox; this one holds the outbox to
    the services, so that "the stream shows what happened" is a claim about
    RAVEL rather than about a helper in this file.
    """
    async with Watcher(following(database, project)) as watcher:
        await watcher.next()
        with database.transaction() as session:
            ProjectRegistry(session).transition(
                project.project_id,
                ProjectStatus.CONTRACT_DEFINED,
                actor_id="master",
                actor_type=ActorType.AGENT,
            )
        frame = await watcher.next()

    assert frame["event_type"] == ProjectEventType.PROJECT_STATUS_CHANGED.value
    assert frame["payload"]["to"] == ProjectStatus.CONTRACT_DEFINED.value


async def test_a_quiet_project_does_not_look_like_a_dead_socket(
    database: Database, project: Project
) -> None:
    """Heartbeats, because silence has two meanings and a client must tell them
    apart. A paused project emits nothing for days and is not broken; a hung
    connection emits nothing and is. Without a heartbeat the two are the same
    screen."""
    async with Watcher(following(database, project)) as watcher:
        await watcher.next()
        quiet = await watcher.next()

    assert quiet["type"] == "heartbeat"
    assert quiet["head_seq"] == BORN


async def test_a_heartbeat_does_not_advance_the_stream(
    database: Database, project: Project
) -> None:
    """It reports where the stream is; it is not part of the stream.

    A heartbeat that carried a sequence of its own would make the client's
    `after_seq` move on frames that are not events, and the next reconnect
    would ask for a position in a stream of nothing.
    """
    _note(database, project, "one")

    async with Watcher(following(database, project)) as watcher:
        assert (await watcher.next())["head_seq"] == BORN + 1
        beat = await watcher.next()

    assert beat == {"type": "heartbeat", "head_seq": BORN + 1}


# ── What a frame is, and is not ─────────────────────────────────────────────


async def test_a_frame_is_an_event_and_nothing_else(
    database: Database, project: Project
) -> None:
    """The frame's top level is fixed, so the socket cannot grow a field by
    accident. Everything an event has to say is in `as_wire`, and anything that
    is not there is not on the wire."""
    async with Watcher(following(database, project)) as watcher:
        await watcher.next()
        _note(database, project, "one")
        frame = await watcher.next()

    assert set(frame) == {
        "type",
        "event_id",
        "project_id",
        "seq",
        "event_type",
        "actor_type",
        "actor_id",
        "payload",
        "created_at",
    }


async def test_the_stream_is_the_projects_and_not_anothers(
    database: Database, project: Project, other_project: Project
) -> None:
    """A project's events are read by project, so a member of one is never sent
    another's — the same boundary `ProjectScopedRepository` draws everywhere
    else, drawn here by the query rather than by a filter after it."""
    _note(database, other_project, "not yours")

    async with Watcher(following(database, project)) as watcher:
        assert (await watcher.next())["head_seq"] == BORN
        quiet = await watcher.next()

    assert quiet["type"] == "heartbeat"


async def test_a_member_who_is_not_the_owner_sees_the_same_stream(
    database: Database, project: Project
) -> None:
    """The stream is the read surface every member shares, and the feed takes a
    project and no user, which is what says so: authority is decided at the
    socket, once, and what is sent afterwards does not depend on who is
    watching."""
    account(database, username="lab", role=UserRole.LAB_USER, project=project)

    async with Watcher(following(database, project)) as watcher:
        await watcher.next()
        _note(database, project, "one")
        frame = await watcher.next()

    assert frame["seq"] == BORN + 1
