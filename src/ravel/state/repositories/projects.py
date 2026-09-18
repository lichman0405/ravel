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
from ravel.domain.roles import AgentRole
from ravel.state.mapping import build_row, from_row
from ravel.state.outbox import emit
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.repositories.dag import require_master
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
    """The coarse phases above the executable DAG.

    The roadmap is the coarse half of the plan and is written by Master, like
    the DAG it sits above. What it does *not* carry is any record of which
    phases have been expanded into nodes: that is a fact about the DAG, derived
    by `ravel.domain.planning`, and storing it here would be a second copy that
    could disagree with the first.
    """

    row_type = RoadmapPhaseRow
    record_type = RoadmapPhase

    def _order_by(self) -> Any:
        return RoadmapPhaseRow.order

    def phases(self) -> list[RoadmapPhase]:
        """Every phase, in the order Master put them."""
        return self.all()

    def register(self, phase: RoadmapPhase, *, role: AgentRole) -> RoadmapPhase:
        """Add a phase to the roadmap.

        A phase carries no `DecisionRecord` of its own. A decision records what
        changed among the *nodes* — the concrete commitments — and a phase on
        its own commits none; the decision requirement attaches to the nodes
        that expand it, in `ravel.state.services.dag`.

        Raises:
            PermissionError: The actor is not Master.
            ProjectScopeError: The phase belongs to another project.
        """
        require_master(role)
        self.add(phase)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.DAG_MUTATED,
            actor_type=ActorType.AGENT,
            actor_id=role.value,
            payload={
                "change": "REGISTER_PHASE",
                "phase_id": phase.phase_id,
                "name": phase.name,
                "order": phase.order,
            },
        )
        return phase

    def phase(self, name: str) -> RoadmapPhase:
        """One phase by name.

        Raises:
            NotFound: This project has no such phase.
        """
        return self.get(name=name)
