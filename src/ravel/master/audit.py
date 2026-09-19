"""The final audit trail: everything a reader needs to disagree with the ending.

A20 asks for a project to reach one of four endings *with a final audit trail*.
The trail is assembled here rather than stored anywhere, and that is the
design rather than an omission: every part of it is already an immutable
record in PostgreSQL — the decisions, the reviews, the executions, the
deviations, the event stream, the DAG itself — and a stored summary would be a
second copy of those that could disagree with them. A reader checking whether
a project's ending was justified should be reading the records the ending was
measured against, not a document written by whoever declared it.

**What this adds is the frame.** The parts are all reachable one repository at
a time; what is not otherwise reachable is the answer to "was this project
finished", which is a question about the relationship between the project's
status, its nodes, and its unanswered escalations. That is computed here, in
the open, so the TUI and the final report ask one function rather than each
re-deriving it.

Nothing here writes. It is safe to call on a running project, and it is the
same call before and after the ending — which is what makes it usable as the
thing a reader consults to decide whether the project is finished.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ravel.domain.contracts import ProjectSuccessContract
from ravel.domain.dag import DagNode
from ravel.domain.decisions import DecisionRecord, ReviewRecord
from ravel.domain.enums import ProjectStatus
from ravel.domain.events import ProjectEvent
from ravel.domain.execution import DeviationRecord, ExecutionRecord
from ravel.domain.project import Project, RoadmapPhase
from ravel.domain.state_machines import TERMINAL_NODE_STATUSES, TERMINAL_PROJECT_STATUSES
from ravel.state.outbox import events_since
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import SuccessContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry, RoadmapRepository
from ravel.state.repositories.records import (
    DecisionRepository,
    DeviationRepository,
    ExecutionRepository,
    ReviewRepository,
)

__all__ = ["ProjectAudit"]


@dataclass(frozen=True, slots=True)
class ProjectAudit:
    """One project's authoritative state, gathered for a reader.

    Frozen, and every field a tuple or a scalar: this is a snapshot of the
    records as they were when it was assembled, and a mutable view of them
    would invite a caller to treat a derived answer as the state itself.
    """

    project: Project
    success_contract: ProjectSuccessContract | None
    roadmap: tuple[RoadmapPhase, ...]
    nodes: tuple[DagNode, ...]
    decisions: tuple[DecisionRecord, ...]
    reviews: tuple[ReviewRecord, ...]
    executions: tuple[ExecutionRecord, ...]
    deviations: tuple[DeviationRecord, ...]
    events: tuple[ProjectEvent, ...]

    @classmethod
    def assemble(cls, session: Session, project_id: str) -> ProjectAudit:
        """Gather everything, in one read of the project's records.

        Raises:
            NotFound: No such project.
        """
        projects = ProjectRegistry(session)
        try:
            success: ProjectSuccessContract | None = SuccessContractRepository(
                session, project_id
            ).latest()
        except NotFound:
            # Absent rather than an error: a project that has not defined
            # success yet is a legitimate state, and a reader asking what a
            # running project looks like should not have to catch for it.
            success = None

        return cls(
            project=projects.get(project_id),
            success_contract=success,
            roadmap=tuple(RoadmapRepository(session, project_id).phases()),
            nodes=tuple(DagRepository(session, project_id).nodes()),
            decisions=tuple(DecisionRepository(session, project_id).all()),
            reviews=tuple(ReviewRepository(session, project_id).all()),
            executions=tuple(ExecutionRepository(session, project_id).all()),
            deviations=tuple(DeviationRepository(session, project_id).all()),
            events=tuple(events_since(session, project_id)),
        )

    # ── The questions a reader asks of it ───────────────────────────────────

    @property
    def unfinished(self) -> tuple[DagNode, ...]:
        """Nodes whose ending has not been recorded."""
        return tuple(
            node for node in self.nodes if node.status not in TERMINAL_NODE_STATUSES
        )

    @property
    def open_deviations(self) -> tuple[DeviationRecord, ...]:
        """Escalations no decision has answered."""
        return tuple(
            deviation for deviation in self.deviations if deviation.is_open
        )

    @property
    def passed(self) -> tuple[DagNode, ...]:
        """Nodes whose result was accepted."""
        return tuple(node for node in self.nodes if node.succeeded)

    @property
    def is_finished(self) -> bool:
        """Whether the project has reached an ending.

        The project's own status, not a summary of the DAG: a project whose
        nodes have all ended has stopped *working*, which is a different fact
        from having been concluded, and only the second is an ending.
        """
        return self.project.status in TERMINAL_PROJECT_STATUSES

    @property
    def is_concludable(self) -> bool:
        """Whether the project could be concluded now, and not terminated.

        The same three conditions `MasterService.conclude` enforces, stated
        once here for readers that want to explain a wait rather than to
        perform the act. It is a *readable* form of the rule and not the
        enforcement — the enforcement is in the service, next to the write,
        because a check that lives where it can be skipped is not a check.
        """
        if self.project.status is ProjectStatus.CREATED:
            return False
        if self.is_finished:
            return False
        return (
            self.success_contract is not None
            and not self.unfinished
            and not self.open_deviations
        )
