"""The project event stream.

Every change to a project emits one event, and events are sequenced per project
so a consumer can tell whether it missed one. The TUI reconnects from
`last_event_seq`; nothing else treats this stream as authoritative, because the
state it describes is already in the tables.

Events are written in the same transaction as the change they describe — the
transactional outbox. A rolled-back transaction therefore emits nothing, which
is the property `tests/integration/state` asserts.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.ids import new_id


class ProjectEventType(StrEnum):
    """The V0 event vocabulary, from `schemas/project_events.yaml`."""

    PROJECT_CREATED = "PROJECT_CREATED"
    PROJECT_STATUS_CHANGED = "PROJECT_STATUS_CHANGED"
    MEMBER_ADDED = "MEMBER_ADDED"
    MEMBER_REVOKED = "MEMBER_REVOKED"
    MASTER_STARTED = "MASTER_STARTED"
    MASTER_CHECKPOINTED = "MASTER_CHECKPOINTED"
    MASTER_RECOVERED = "MASTER_RECOVERED"
    NODE_CREATED = "NODE_CREATED"
    NODE_READY = "NODE_READY"
    NODE_STARTED = "NODE_STARTED"
    NODE_WAITING = "NODE_WAITING"
    NODE_COMPLETED = "NODE_COMPLETED"
    NODE_FAILED = "NODE_FAILED"
    NODE_CANCELLED = "NODE_CANCELLED"
    REVIEW_SUBMITTED = "REVIEW_SUBMITTED"
    DECISION_CREATED = "DECISION_CREATED"
    DAG_MUTATED = "DAG_MUTATED"
    DEVIATION_REPORTED = "DEVIATION_REPORTED"
    ARTIFACT_REGISTERED = "ARTIFACT_REGISTERED"
    EVIDENCE_REGISTERED = "EVIDENCE_REGISTERED"
    APPROVAL_REQUESTED = "APPROVAL_REQUESTED"
    APPROVAL_RESOLVED = "APPROVAL_RESOLVED"
    BACKEND_STATUS_CHANGED = "BACKEND_STATUS_CHANGED"
    AGENT_SESSION_STARTED = "AGENT_SESSION_STARTED"
    AGENT_SESSION_ENDED = "AGENT_SESSION_ENDED"


class ActorType(StrEnum):
    """Who caused an event."""

    USER = "USER"
    AGENT = "AGENT"
    SYSTEM = "SYSTEM"
    BACKEND = "BACKEND"


class ProjectEvent(Record):
    """One entry in a project's stream.

    `seq` is assigned by the database, not by the caller: the sequence is the
    thing consumers rely on to detect a gap, so it must come from the single
    writer that can guarantee it is contiguous.
    """

    event_id: str = Field(default_factory=new_id)
    project_id: str
    seq: int | None = Field(default=None, ge=1)
    event_type: ProjectEventType
    actor_type: ActorType
    actor_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)

    def as_wire(self) -> dict[str, Any]:
        """A JSON-safe view for the WebSocket projection."""
        return {
            "event_id": self.event_id,
            "project_id": self.project_id,
            "seq": self.seq,
            "event_type": self.event_type.value,
            "actor_type": self.actor_type.value,
            "actor_id": self.actor_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }
