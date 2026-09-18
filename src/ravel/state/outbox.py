"""The transactional outbox — events written with the change they describe.

Every state change and its event go into the *same* transaction. Two properties
follow, and both are gate items:

- A rolled-back transaction emits nothing. Roll back a node transition and the
  `NODE_STARTED` event goes with it, because the counter row and the event row
  are in the same transaction as the node update.
- A committed change always has its event. There is no window in which the
  tables have moved and the stream has not, which is what makes the stream safe
  for the TUI to render as a live view.

Sequence numbers are allocated from `project_event_counters` with a row lock
rather than from a sequence object. A PostgreSQL sequence is deliberately
non-transactional — it advances even when the transaction rolls back — which
would leave gaps, and a gap is exactly the signal a consumer uses to detect
that it missed an event. The counter rolls back with everything else.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ravel.domain.events import ActorType, ProjectEvent, ProjectEventType
from ravel.state.mapping import build_row, from_row
from ravel.state.tables import ProjectEventRow

_ENSURE_COUNTER = text(
    """
    INSERT INTO project_event_counters (project_id, next_seq)
    VALUES (:project_id, 1)
    ON CONFLICT (project_id) DO NOTHING
    """
)

#: `next_seq` is the *next* number to hand out, so the number this statement
#: returns is `next_seq - 1`. The `UPDATE` takes a row lock, which serializes
#: concurrent writers to one project — the single writer the sequence needs.
_TAKE_SEQ = text(
    """
    UPDATE project_event_counters
       SET next_seq = next_seq + 1
     WHERE project_id = :project_id
    RETURNING next_seq - 1
    """
)


def next_event_seq(session: Session, project_id: str) -> int:
    """Reserve the next sequence number in a project's stream.

    Must be called inside the transaction that writes the event, so a rollback
    returns the number rather than burning a hole in the sequence.

    The flush is not decoration. `Session.execute(text(...))` does **not**
    autoflush ORM-pending objects — measured, not assumed — so a caller that
    has just staged a new project would reach the counter's foreign key before
    the project row existed. Flushing here makes the ordering explicit rather
    than dependent on a behaviour that does not hold for textual SQL.
    """
    session.flush()
    session.execute(_ENSURE_COUNTER, {"project_id": project_id})
    reserved = session.execute(_TAKE_SEQ, {"project_id": project_id}).scalar_one()
    return int(reserved)


def emit(
    session: Session,
    *,
    project_id: str,
    event_type: ProjectEventType,
    actor_type: ActorType,
    actor_id: str,
    payload: dict[str, Any] | None = None,
    event: ProjectEvent | None = None,
) -> ProjectEvent:
    """Append one event to a project's stream and return it, sequenced.

    Either pass the fields, or pass a fully built `event` whose `project_id`,
    `event_type`, `actor_type`, `actor_id`, and `payload` are reused. The
    sequence and identifier always come from here.

    The event is added to the session's pending writes; it reaches PostgreSQL
    when the caller's transaction commits, and not before.
    """
    if event is not None:
        project_id = event.project_id
        event_type = event.event_type
        actor_type = event.actor_type
        actor_id = event.actor_id
        payload = event.payload

    recorded = ProjectEvent(
        project_id=project_id,
        seq=next_event_seq(session, project_id),
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        payload=payload or {},
    )
    session.add(build_row(ProjectEventRow, recorded))
    return recorded


def events_since(session: Session, project_id: str, after_seq: int = 0) -> list[ProjectEvent]:
    """A project's events after a sequence number, oldest first.

    The TUI reconnects with the last sequence it rendered; a gap between that
    and the first row returned here is how it knows it missed something.
    """
    rows = (
        session.query(ProjectEventRow)
        .filter(ProjectEventRow.project_id == project_id, ProjectEventRow.seq > after_seq)
        .order_by(ProjectEventRow.seq)
        .all()
    )
    return [from_row(ProjectEvent, row) for row in rows]


def last_event_seq(session: Session, project_id: str) -> int:
    """The highest sequence written for a project, or zero if none has been."""
    highest = session.execute(
        select(func.max(ProjectEventRow.seq)).where(ProjectEventRow.project_id == project_id)
    ).scalar_one_or_none()
    return int(highest or 0)
