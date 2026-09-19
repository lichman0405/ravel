"""The transcript with Master, and why it is a table rather than a session log.

`docs/04` says a DSH session is not a project, and a DSH session does not
outlive its process. So the conversation is stored where the rest of the
project's state is, and these tests assert the properties that make reading it
back worthwhile: that it says who said what, that a reply belongs to the
question it answers, that it reads in the order things were said, and that
nothing rewrites it afterwards.

The last of those is the one the database enforces rather than this code. A
transcript that could be edited would be a claim about the present; the
trigger attached to the table is what makes it a record of the past, and the
reason a later reader can act on it.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from ravel.domain.clock import utcnow
from ravel.domain.events import ActorType
from ravel.domain.identity import MasterMessage
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.conversation import ConversationRepository
from ravel.state.repositories.identity import UserRepository

pytestmark = pytest.mark.integration

QUESTION = "Which of these two hypotheses should the DAG test first?"


def _owner(database: Database) -> str:
    """The project fixture's owner, read back the way a request would find them."""
    with database.read_only() as session:
        owner = UserRepository(session).by_username("owner")
        assert owner is not None
        return owner.user_id


# ── What was said ───────────────────────────────────────────────────────────


def test_a_message_keeps_who_said_it_and_what_they_said(
    database: Database, project: Project
) -> None:
    """The round trip the TUI depends on: a name, a role, and the words.

    `author_type` is stored as well as `author_id` because the two answer
    different questions. The identifier says which account; the type says
    whether a person or an agent was speaking, which is what a reader needs in
    order to know whether the sentence is an instruction or an answer.
    """
    with database.transaction() as session:
        ConversationRepository(session, project.project_id).said(
            body=QUESTION,
            author_id=_owner(database),
            author_type=ActorType.USER,
        )

    with database.read_only() as session:
        transcript = ConversationRepository(session, project.project_id).transcript()

    assert len(transcript) == 1
    assert transcript[0].body == QUESTION
    assert transcript[0].author_type is ActorType.USER
    assert transcript[0].author_id == _owner(database)
    assert transcript[0].project_id == project.project_id


def test_a_reply_belongs_to_the_question_it_answers(database: Database, project: Project) -> None:
    """A turn is stated, not inferred from a clock.

    Master may spend several tool calls before it says anything, and a second
    question may arrive in the meantime. Which reply answers which question is
    therefore not recoverable from timestamps, so the caller that answers
    supplies the turn it is answering.
    """
    asked_by = _owner(database)
    with database.transaction() as session:
        conversation = ConversationRepository(session, project.project_id)
        question = conversation.said(
            body=QUESTION, author_id=asked_by, author_type=ActorType.USER
        )
    with database.transaction() as session:
        conversation = ConversationRepository(session, project.project_id)
        conversation.said(
            body="The one with the cheaper falsification.",
            author_id="master",
            author_type=ActorType.AGENT,
            turn_id=question.turn_id,
        )
        conversation.said(body="And a second question.", author_id=asked_by,
                          author_type=ActorType.USER)

    with database.read_only() as session:
        conversation = ConversationRepository(session, project.project_id)
        exchange = conversation.all(turn_id=question.turn_id)
        everything = conversation.transcript()

    assert [message.body for message in exchange] == [
        QUESTION,
        "The one with the cheaper falsification.",
    ]
    assert len(everything) == 3, "an unrelated question joined the turn"


def test_an_utterance_without_a_turn_opens_one(database: Database, project: Project) -> None:
    """A caller that does not supply a turn gets a new one rather than sharing."""
    with database.transaction() as session:
        conversation = ConversationRepository(session, project.project_id)
        first = conversation.said(body="One.", author_id="a", author_type=ActorType.USER)
        second = conversation.said(body="Two.", author_id="a", author_type=ActorType.USER)

    assert first.turn_id != second.turn_id


# ── The order it reads in ───────────────────────────────────────────────────


def test_the_transcript_reads_in_the_order_things_were_said(
    database: Database, project: Project
) -> None:
    """Ordering is by when a thing was said, not by when this repository wrote it.

    The two differ whenever a message is recorded late — a backfill, a
    transcription of something said before the process started, or simply two
    writes that arrive out of order. Sorting by insertion would place such a
    message at the end, where it would read as the most recent thing anybody
    said. `created_at` is the only column that knows better, so the rows here
    are written newest-first and are asserted to read oldest-first.
    """
    said_at = utcnow()
    with database.transaction() as session:
        conversation = ConversationRepository(session, project.project_id)
        for offset, body in ((2, "said third"), (0, "said first"), (1, "said second")):
            conversation.add(
                MasterMessage(
                    project_id=project.project_id,
                    author_id="a",
                    author_type=ActorType.USER,
                    body=body,
                    created_at=said_at + timedelta(seconds=offset),
                )
            )

    with database.read_only() as session:
        transcript = ConversationRepository(session, project.project_id).transcript()

    assert [message.body for message in transcript] == [
        "said first",
        "said second",
        "said third",
    ]


# ── What is not a message ───────────────────────────────────────────────────


def test_an_empty_message_is_refused(database: Database, project: Project) -> None:
    """Pressing enter on an empty input is not something anybody said.

    Refused by the domain type rather than by a length check at each caller, so
    the TUI, a future CLI, and anything else that writes a message all inherit
    the same rule instead of each having to remember it.
    """
    with database.transaction() as session:
        conversation = ConversationRepository(session, project.project_id)
        with pytest.raises(ValidationError):
            conversation.said(body="", author_id="a", author_type=ActorType.USER)

    with database.read_only() as session:
        assert ConversationRepository(session, project.project_id).transcript() == []


# ── What the database refuses ───────────────────────────────────────────────


def test_a_message_cannot_be_edited_or_unsaid(database: Database, project: Project) -> None:
    """Append-only, enforced by PostgreSQL rather than by this repository.

    The repository offers no update method, but that is a statement about this
    code. The trigger is a statement about the table, and it is what lets a
    reader treat the transcript as what was actually said — including when what
    was said turned out to be wrong, which is precisely when somebody would
    otherwise be tempted to tidy it up.
    """
    with database.transaction() as session:
        ConversationRepository(session, project.project_id).said(
            body=QUESTION, author_id="a", author_type=ActorType.USER
        )

    with database.transaction() as session, pytest.raises(IntegrityError):
        session.execute(sa.text("UPDATE master_messages SET body = 'something else'"))
    with database.transaction() as session, pytest.raises(IntegrityError):
        session.execute(sa.text("DELETE FROM master_messages"))

    with database.read_only() as session:
        transcript = ConversationRepository(session, project.project_id).transcript()
    assert [message.body for message in transcript] == [QUESTION]


# ── Whose conversation it is ────────────────────────────────────────────────


def test_a_project_cannot_read_another_projects_conversation(
    database: Database, project: Project, other_project: Project
) -> None:
    """The transcript is project state, so it is scoped like the rest of it.

    Asserted here as well as for the base class because this is the one read
    where a leak is not merely a correctness bug: a lab user shown another
    project's conversation with Master would learn the direction of research
    they were not admitted to.
    """
    with database.transaction() as session:
        ConversationRepository(session, project.project_id).said(
            body=QUESTION, author_id="a", author_type=ActorType.USER
        )

    with database.read_only() as session:
        foreign = ConversationRepository(session, other_project.project_id)
        assert foreign.transcript() == []
        with pytest.raises(NotFound):
            foreign.get(body=QUESTION)
