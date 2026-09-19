"""Talking to Master, and reading what was said.

This is the route the whole product is arranged around — `docs/08` puts "Master
must feel present" first among the owner's screen priorities — and it is also
the one place where a user's words reach an agent. Three things follow from
that, and each of them is a decision rather than a detail:

**The turn runs inside the request.** A Master turn is bounded by
`dsh_turn_timeout_seconds`, and the alternative — accepting the message,
answering `202`, and running the turn on a background task — would mean a
Gateway that holds work nothing is waiting for, that loses it on restart, and
that has to invent a way to tell a client the turn it asked for is finally
done. The client is a terminal with a person in front of it; the honest thing
is to take a few seconds and answer.

**Nothing the model says is parsed.** The reply is stored as what Master said
and returned as text. Whether Master changed anything is answered by reading
PostgreSQL, not by reading the reply — the tools Master holds write through the
same services everything else uses, and a sentence describing a change that was
never made changes nothing.

**A non-owner may read the transcript and may not add to it.** Directing the
research is the owner's, and what a person says to Master is how the research
gets directed — so `say` is a director's route and `read` is every member's.
A lab user can see the conversation they are part of; they cannot open one.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from ravel.domain.events import ActorType
from ravel.domain.ids import new_id
from ravel.domain.roles import AgentRole
from ravel.execution.loop import read_situation
from ravel.gateway.deps import DirectorDep, GatewayStateDep, PrincipalDep
from ravel.state.repositories.conversation import ConversationRepository

router = APIRouter(prefix="/projects", tags=["conversation"])

#: How much of a transcript one read returns when the caller does not say.
DEFAULT_MESSAGES = 200
MAX_MESSAGES = 2000

#: Who a reply is attributed to. The role value rather than a session or a
#: model name: "Master said this" is the claim the transcript makes, and which
#: runtime happened to answer is not part of it.
MASTER = AgentRole.MASTER.value


class MessageRequest(BaseModel):
    """What a person says. The only user-authored text that reaches an agent."""

    body: str = Field(min_length=1, max_length=20_000)


class Turn(BaseModel):
    """One exchange: what was asked, and what came back.

    `answer` is `None` when the turn did not produce one. That is a real state
    and not an error — a harness turn can fail, or be reaped mid-flight — and
    the question is still in the transcript, so the client can show a question
    that went unanswered instead of a question that was never asked.
    """

    turn_id: str
    question: dict[str, Any]
    answer: dict[str, Any] | None


@router.get("/{project_id}/messages", summary="The conversation with Master")
def read_messages(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_MESSAGES)] = DEFAULT_MESSAGES,
    turn_id: Annotated[
        str | None, Query(description="Only the messages belonging to one exchange.")
    ] = None,
) -> list[dict[str, Any]]:
    """The transcript, oldest first.

    Oldest first because a transcript read in any other order is not a
    transcript — the reason `ConversationRepository` leaves the ordering alone.
    `limit` keeps the most recent messages rather than the first ones, which is
    what a client opening a long conversation wants.
    """
    with state.database.read_only() as session:
        messages = ConversationRepository(session, standing.project_id).transcript()
    if turn_id is not None:
        messages = [message for message in messages if message.turn_id == turn_id]
    return [message.model_dump(mode="json") for message in messages[-limit:]]


@router.post("/{project_id}/messages", summary="Say something to Master")
async def say(
    standing: DirectorDep, state: GatewayStateDep, request: MessageRequest
) -> Turn:
    """Record what was said, run a Master turn, and record the reply.

    The order is the whole design. The question is committed before the turn
    starts, so a turn that dies leaves an unanswered question rather than no
    question; and the answer is committed only if the turn produced one, so
    "Master said nothing" and "Master never answered" stay different facts.

    Raises:
        HTTPException: 409 if the project's status does not let Master act on
            it. Read by the turn's own tools rather than checked here — what
            Master may do is the Authority Envelope's and the DAG's business,
            and a route that pre-judged it would be a second copy of both.
    """
    turn_id = new_id()
    with state.database.transaction() as session:
        question = ConversationRepository(session, standing.project_id).said(
            body=request.body,
            author_id=standing.user_id,
            author_type=ActorType.USER,
            turn_id=turn_id,
        )

    situation = read_situation(state.database, standing.project_id)
    outcome = await state.master_of(standing.project_id).respond(situation, request.body)

    answer: dict[str, Any] | None = None
    if outcome.text:
        with state.database.transaction() as session:
            reply = ConversationRepository(session, standing.project_id).said(
                body=outcome.text,
                author_id=MASTER,
                author_type=ActorType.AGENT,
                turn_id=turn_id,
            )
        answer = reply.model_dump(mode="json")

    return Turn(
        turn_id=turn_id,
        question=question.model_dump(mode="json"),
        answer=answer,
    )


__all__ = ["DEFAULT_MESSAGES", "MAX_MESSAGES", "MessageRequest", "Turn", "router"]
