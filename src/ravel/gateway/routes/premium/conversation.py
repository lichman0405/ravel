"""The versioned transcript, and a message that cannot be recorded twice.

The write is the interesting half. A terminal client that loses its connection
after sending, or a desktop client whose retry fires while the first request is
still running, must not create two questions for one thing a person said. The
duplicate rule is the `client_message_id`: the caller chooses it once per
message, and the server derives the turn identifier from it together with the
project and the user.

The derivation is deterministic, and the insert is serialized by a transaction
-scoped advisory lock keyed by that identifier. Two concurrent first attempts
therefore produce one question: the second waits, then finds the row the first
committed. A replay that arrives after the turn finished returns the stored
question and answer without asking Master again. A replay that arrives while
the first turn is still running returns the stored question with `answer:
null` — the honest state of an exchange that has been asked and not yet
answered — rather than starting a second turn.

**Nothing the model says is parsed.** The reply is stored as what Master said.
Whether the project changed is answered by reading PostgreSQL.
"""

from __future__ import annotations

import hashlib
from typing import Annotated

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from ravel.domain.events import ActorType
from ravel.domain.identity import MasterMessage
from ravel.execution.loop import read_situation
from ravel.gateway.deps import DirectorDep, GatewayState, GatewayStateDep, PrincipalDep
from ravel.gateway.routes.premium.support import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    paginate,
)
from ravel.state.repositories.conversation import ConversationRepository

router = APIRouter(tags=["v1-conversation"])

#: Who a reply is attributed to, matching the legacy route: the role, not a
#: harness session and not a model name.
MASTER = "master"


class ClientMessage(BaseModel):
    """What a person says, and the identifier their client uses to retry it."""

    body: str = Field(min_length=1, max_length=20_000)
    client_message_id: str = Field(
        min_length=1,
        max_length=200,
        description="Chosen once by the client; a retry with the same value is not a new message.",
    )


class TurnView(BaseModel):
    """One exchange, and whether this response replayed a stored one."""

    turn_id: str
    question: MasterMessage
    answer: MasterMessage | None
    deduplicated: bool


@router.get(
    "/projects/{project_id}/conversation",
    response_model=Page[MasterMessage],
    summary="The conversation with Master, oldest first",
)
def read_conversation(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
    turn_id: Annotated[
        str | None, Query(description="Only the messages belonging to one exchange.")
    ] = None,
) -> Page[MasterMessage]:
    """A page of the transcript in the order it happened.

    Oldest first, because a transcript read backwards is not a transcript. A
    client that wants the newest messages starts from the newest page and walks
    backwards with the cursor it receives, rather than asking this route for a
    different order.
    """
    with state.database.read_only() as session:
        messages = ConversationRepository(session, standing.project_id).transcript()
    if turn_id is not None:
        messages = [message for message in messages if message.turn_id == turn_id]
    return paginate(
        messages,
        key=lambda message: (message.created_at.isoformat(), message.message_id),
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/projects/{project_id}/conversation",
    response_model=TurnView,
    summary="Say something to Master, once per client_message_id",
)
async def say(
    standing: DirectorDep, state: GatewayStateDep, request: ClientMessage
) -> TurnView:
    """Record the question, run one Master turn, record the answer if there is one.

    The question is committed before the turn starts, so a turn that dies leaves
    an unanswered question rather than no question. The advisory lock covers the
    check-and-insert only; it is not held across the model turn, because holding
    a transaction open for minutes would block every writer to the project for a
    reason that has nothing to do with them.
    """
    turn_id = _turn_id(standing.project_id, standing.user_id, request.client_message_id)
    messages, fresh = _record_question(
        state, standing.project_id, standing.user_id, request.body, turn_id
    )
    question, answer = _split(messages)

    if not fresh:
        return TurnView(
            turn_id=turn_id,
            question=question,
            answer=answer,
            deduplicated=True,
        )

    outcome = await state.master_of(standing.project_id).respond(
        read_situation(state.database, standing.project_id), request.body
    )

    if outcome.text:
        with state.database.transaction() as session:
            reply = ConversationRepository(session, standing.project_id).said(
                body=outcome.text,
                author_id=MASTER,
                author_type=ActorType.AGENT,
                turn_id=turn_id,
            )
        answer = reply

    return TurnView(
        turn_id=turn_id,
        question=question,
        answer=answer,
        deduplicated=False,
    )


def _turn_id(project_id: str, user_id: str, client_message_id: str) -> str:
    """The deterministic exchange identifier for one client message.

    Prefixed so a reader can tell a derived turn from a random one, and hashed
    so a client-chosen string never becomes a visible identifier in the
    transcript. The project and user are part of the input because two people
    — or the same person in two projects — may independently choose the same
    client identifier, and those are different messages.
    """
    material = "\x1f".join((project_id, user_id, client_message_id))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return f"trn_{digest[:32]}"


def _record_question(
    state: GatewayState,
    project_id: str,
    user_id: str,
    body: str,
    turn_id: str,
) -> tuple[list[MasterMessage], bool]:
    """Store the question unless this turn already has one.

    Returns the messages already in this exchange and whether this call created
    the question. The advisory lock is transaction-scoped and keyed by the turn
    identifier: a second request for the same client message waits for the
    first insert to commit and then sees it, instead of inserting a duplicate.
    """
    with state.database.transaction() as session:
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"ravel-turn:{turn_id}"},
        )
        repository = ConversationRepository(session, project_id)
        existing = repository.all(turn_id=turn_id)
        if existing:
            return existing, False
        asked = repository.said(
            body=body,
            author_id=user_id,
            author_type=ActorType.USER,
            turn_id=turn_id,
        )
        return [asked], True


def _split(messages: list[MasterMessage]) -> tuple[MasterMessage, MasterMessage | None]:
    """The question and, if it exists, the answer, from one exchange.

    An exchange has at most one question (the insert above guarantees it) and at
    most one answer V0 writes. The question is selected by author type rather
    than by position so a future system message in the same turn cannot be
    mistaken for the person's words.
    """
    question = next(
        (message for message in messages if message.author_type is ActorType.USER),
        messages[0],
    )
    answer = next(
        (
            message
            for message in reversed(messages)
            if message.author_type is ActorType.AGENT
        ),
        None,
    )
    return question, answer


__all__ = ["ClientMessage", "TurnView", "router"]
