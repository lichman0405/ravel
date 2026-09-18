"""The Project, and the roadmap above the executable DAG.

A Project is the unit of isolation: its own DAG, its own evidence, its own
artifacts, its own Master. It is not a harness session — sessions come and go,
and the project is what survives them.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import ProjectStatus
from ravel.domain.ids import DisplayPrefix, display_id, new_id
from ravel.domain.state_machines import TransitionCheck, can_transition_project


class Project(Record):
    """One research project.

    The only fields that change are `status`, `updated_at`, and `paused_reason`,
    and they change through `transition`, which enforces the lifecycle. A
    project's contracts, its DAG, and its evidence live in the immutable records
    that reference it; a project row is an index into those, not a summary of
    them.
    """

    project_id: str = Field(default_factory=new_id)
    display_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.PROJECT))
    title: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    status: ProjectStatus = ProjectStatus.CREATED
    created_by: str
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    paused_reason: str | None = None

    def transition(self, target: ProjectStatus) -> Project:
        """Return a copy in the target status, or raise.

        Raises:
            TransitionError: The transition is not legal from the current status.
        """
        can_transition_project(self.status, target).raise_if_denied()
        update: dict[str, object] = {"status": target, "updated_at": utcnow()}
        if target is not ProjectStatus.PAUSED:
            update["paused_reason"] = None
        return self.model_copy(update=update)

    def check_transition(self, target: ProjectStatus) -> TransitionCheck:
        """Whether this project may move to a status."""
        return can_transition_project(self.status, target)


class RoadmapPhase(Record):
    """A broad future phase. Deliberately coarse.

    The roadmap says where the project is going; the DAG says what to do next.
    Only the near part of the roadmap is expanded into executable nodes, which
    is what keeps planning honest when early results change the plan.
    """

    phase_id: str = Field(default_factory=new_id)
    project_id: str
    name: str = Field(min_length=1)
    intent: str = ""
    order: int = Field(ge=0)
    expanded: bool = False
    created_at: datetime = Field(default_factory=utcnow)


class Roadmap(Record):
    """An ordered set of phases for one project."""

    roadmap_id: str = Field(default_factory=lambda: display_id(DisplayPrefix.ROADMAP))
    project_id: str
    phases: tuple[RoadmapPhase, ...] = ()
    created_at: datetime = Field(default_factory=utcnow)

    @model_validator(mode="after")
    def _phases_are_ordered_and_unique(self) -> Roadmap:
        orders = [phase.order for phase in self.phases]
        if orders != sorted(orders):
            raise ValueError("roadmap phases must be ordered by their `order` field")
        if len(set(orders)) != len(orders):
            raise ValueError("two roadmap phases share an `order` value")
        return self

    def expanded_phases(self) -> tuple[RoadmapPhase, ...]:
        """The phases Master has committed to concrete nodes."""
        return tuple(phase for phase in self.phases if phase.expanded)
