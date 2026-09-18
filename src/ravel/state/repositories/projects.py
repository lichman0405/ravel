"""The project registry: creating projects and moving them through their life cycle.

This is the one repository that is not project-scoped, because it is the thing
that mints project identifiers. Everything it hands back carries a scope, and
every other repository requires one.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ravel.domain.enums import ProjectStatus
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.project import Project, RoadmapPhase
from ravel.state.mapping import build_row, from_row
from ravel.state.outbox import emit
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.tables import ProjectRow, RoadmapPhaseRow


class ProjectRegistry:
    """Create and look up projects, and move their status."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        *,
        title: str,
        objective: str,
        created_by: str,
        actor_type: ActorType = ActorType.USER,
    ) -> Project:
        """Open a project in CREATED status and record that it happened."""
        project = Project(title=title, objective=objective, created_by=created_by)
        self.session.add(build_row(ProjectRow, project))
        emit(
            self.session,
            project_id=project.project_id,
            event_type=ProjectEventType.PROJECT_CREATED,
            actor_type=actor_type,
            actor_id=created_by,
            payload={"title": title, "display_id": project.display_id},
        )
        return project

    def get(self, project_id: str) -> Project:
        """One project by identifier.

        Raises:
            NotFound: No such project.
        """
        row = self.session.get(ProjectRow, project_id)
        if row is None:
            raise NotFound(f"no project {project_id!r}")
        return from_row(Project, row)

    def find(self, project_id: str) -> Project | None:
        """One project by identifier, or `None`."""
        row = self.session.get(ProjectRow, project_id)
        return from_row(Project, row) if row is not None else None

    def list(self, *, status: ProjectStatus | None = None) -> list[Project]:
        """Every project, oldest first, optionally filtered by status."""
        statement = select(ProjectRow).order_by(ProjectRow.created_at)
        if status is not None:
            statement = statement.where(ProjectRow.status == status.value)
        return [from_row(Project, row) for row in self.session.execute(statement).scalars()]

    def transition(
        self,
        project_id: str,
        target: ProjectStatus,
        *,
        actor_id: str,
        actor_type: ActorType = ActorType.SYSTEM,
        reason: str | None = None,
    ) -> Project:
        """Move a project to a new status and record the change.

        The database enforces the transition table as well, so an illegal move
        is refused by `Project.transition` first and by a trigger if it ever
        gets past it.

        Re-asserting the current status is a no-op: a retried activity that
        reports the state already reached writes nothing and emits nothing,
        rather than putting a duplicate in the stream.
        """
        project = self.get(project_id)
        if project.status is target:
            return project

        moved = project.transition(target)
        if reason is not None and target is ProjectStatus.PAUSED:
            moved = moved.model_copy(update={"paused_reason": reason})

        row = self.session.get(ProjectRow, project_id)
        assert row is not None  # `get` above already proved it
        for column, value in moved.model_dump(mode="python").items():
            setattr(row, column, value)

        emit(
            self.session,
            project_id=project_id,
            event_type=ProjectEventType.PROJECT_STATUS_CHANGED,
            actor_type=actor_type,
            actor_id=actor_id,
            payload={
                "from": project.status.value,
                "to": target.value,
                "reason": reason,
            },
        )
        return moved


class RoadmapRepository(ProjectScopedRepository[RoadmapPhase]):
    """The coarse phases above the executable DAG."""

    row_type = RoadmapPhaseRow
    record_type = RoadmapPhase

    def _order_by(self) -> Any:
        return RoadmapPhaseRow.order

    def phases(self) -> list[RoadmapPhase]:
        """Every phase, in the order Master put them."""
        return self.all()

    def expanded(self) -> list[RoadmapPhase]:
        """The phases Master has committed to concrete nodes."""
        return self.all(expanded=True)
