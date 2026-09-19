"""What a person said to Master, and what Master said back.

The transcript is a project-scoped, append-only table rather than a view over
the harness's session log, for the reason `domain.identity.MasterMessage`
gives: a DSH session does not survive its process, so a conversation kept only
there would be lost exactly when somebody wants to read it.

Nothing here decides what Master means. A caller records an utterance; whether
any of them were wise is not this repository's business.
"""

from __future__ import annotations

from ravel.domain.events import ActorType
from ravel.domain.identity import MasterMessage
from ravel.domain.ids import new_id
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.tables import MasterMessageRow


class ConversationRepository(ProjectScopedRepository[MasterMessage]):
    """One project's transcript with Master, in the order it happened.

    Ordering is the base class's default — `created_at`, oldest first — and is
    left alone deliberately. A transcript read in any other order is not a
    transcript.
    """

    row_type = MasterMessageRow
    record_type = MasterMessage

    def said(
        self,
        *,
        body: str,
        author_id: str,
        author_type: ActorType,
        turn_id: str | None = None,
    ) -> MasterMessage:
        """Record one utterance.

        `turn_id` is passed in rather than generated per message because a
        reply belongs to the question it answers: a caller that has just
        recorded a person's message hands the same `turn_id` to Master's reply,
        and the two are then one exchange rather than two unrelated rows that
        happened to land in the same second. Omitting it opens a new exchange.
        """
        message = MasterMessage(
            project_id=self.project_id,
            author_id=author_id,
            author_type=author_type,
            body=body,
            turn_id=turn_id or new_id(),
        )
        return self.add(message)

    def transcript(self) -> list[MasterMessage]:
        """Everything said in this project, oldest first."""
        return self.all()
