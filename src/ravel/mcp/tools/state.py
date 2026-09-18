"""Master's window onto the project, and the checkpoint it writes for itself.

Every handler here reads or writes PostgreSQL. None of them reads the runtime
brief: the brief is a launch-time snapshot of what RAVEL believed when it
started the session, and a Master that made decisions from it would be deciding
from a memory of the project rather than from the project. `read_project_state`
exists precisely so the agent can ask the record of truth instead of trusting
its own recollection of the conversation.
"""

from __future__ import annotations

from typing import Any

from ravel.domain.enums import NodeStatus
from ravel.domain.identity import MasterCheckpoint
from ravel.domain.roles import AgentRole
from ravel.mcp.context import ToolContext, as_json, require_master
from ravel.mcp.registry import tools_for
from ravel.state.outbox import last_event_seq
from ravel.state.repositories.identity import (
    AgentIdentityRepository,
    ApprovalRepository,
    CheckpointRepository,
)
from ravel.state.repositories.projects import ProjectRegistry, RoadmapRepository
from ravel.state.services.dag import DagMutationService

#: The statuses counted separately in the state summary. Anything not listed is
#: reported under its own name, so a new status cannot be silently folded into
#: a bucket that would misdescribe it.
_AT_A_GLANCE = (
    NodeStatus.PLANNED,
    NodeStatus.READY,
    NodeStatus.RUNNING,
    NodeStatus.REVIEWING,
)


def whoami(context: ToolContext) -> Any:
    """Report this session's project, role, and available tools."""

    async def whoami() -> dict[str, Any]:
        """Report the project and role this session is serving, and its tools."""
        return {
            "project_id": context.project_id,
            "role": context.role.value,
            "role_name": context.role.display_name,
            "tools": list(tools_for(context.role)),
            "may_mutate_dag": context.scope.is_master,
        }

    return whoami


def read_project_state(context: ToolContext) -> Any:
    """Read the authoritative project state."""

    async def read_project_state() -> dict[str, Any]:
        """Read this project's authoritative state from the database.

        Returns the project's own record, its roadmap and where in it the
        project currently is, a summary of the DAG by status, and how many
        approvals are waiting on a human. Use this rather than any earlier
        description of the project, including your own.
        """
        with context.read() as session:
            project = ProjectRegistry(session).get(context.project_id)
            phases = RoadmapRepository(session, context.project_id).phases()
            service = DagMutationService(session, context.project_id)
            horizon = service.horizon()
            work = service.phase_work()
            nodes = service.dag.nodes()
            pending = ApprovalRepository(session, context.project_id).pending()
            checkpoint = CheckpointRepository(session, context.project_id).latest()
            seq = last_event_seq(session, context.project_id)

        counts: dict[str, int] = {}
        for node in nodes:
            counts[node.status.value] = counts.get(node.status.value, 0) + 1

        return {
            "project_id": project.project_id,
            "display_id": project.display_id,
            "title": project.title,
            "objective": project.objective,
            "status": project.status.value,
            "created_at": project.created_at.isoformat(),
            "roadmap": [
                {
                    "name": phase.name,
                    "order": phase.order,
                    "intent": phase.intent,
                    "nodes": work[phase.name].nodes if phase.name in work else 0,
                    "unfinished": work[phase.name].unfinished if phase.name in work else 0,
                }
                for phase in phases
            ],
            "current_phase": horizon.current.name if horizon.current else None,
            "reachable_phases": list(horizon.reachable_names),
            "dag": {
                "nodes": len(nodes),
                "by_status": counts,
                "at_a_glance": {
                    status.value: counts.get(status.value, 0) for status in _AT_A_GLANCE
                },
                "ready_to_run": [n.display_id for n in nodes if n.status is NodeStatus.READY],
            },
            "approvals_pending": len(pending),
            "latest_checkpoint": as_json(checkpoint) if checkpoint is not None else None,
            "last_event_seq": seq,
        }

    return read_project_state


def list_pending_approvals(context: ToolContext) -> Any:
    """List the questions waiting on a human."""

    async def list_pending_approvals() -> dict[str, Any]:
        """List approvals that no human has answered yet.

        Reaching for this is how you avoid asking twice, and how you tell
        waiting-on-a-human apart from waiting-on-yourself.
        """
        require_master(context, "list_pending_approvals")
        with context.read() as session:
            pending = ApprovalRepository(session, context.project_id).pending()
        return {"approvals": as_json(pending), "count": len(pending)}

    return list_pending_approvals


def write_master_checkpoint(context: ToolContext) -> Any:
    """Write the checkpoint a recovery would read."""

    async def write_master_checkpoint(
        current_focus: str,
        active_hypotheses: list[str] | None = None,
        pending_questions: list[str] | None = None,
        waiting_on: list[str] | None = None,
        recent_decision_refs: list[str] | None = None,
        important_context_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write down where your work stands, so a replacement session can continue it.

        Record what you are working on, what you are waiting for, and which
        decisions and context a successor should read. This is a navigational
        aid, not a record of truth: everything in it must be re-derivable from
        the project state, which is why the event sequence it was written at is
        stamped on it for the recovery to replay from.
        """
        require_master(context, "write_master_checkpoint")
        with context.write() as session:
            identity = AgentIdentityRepository(session, context.project_id).ensure(
                AgentRole.MASTER
            )
            checkpoint = MasterCheckpoint(
                project_id=context.project_id,
                master_identity_id=identity.identity_id,
                current_focus=current_focus,
                active_hypotheses=tuple(active_hypotheses or ()),
                pending_questions=tuple(pending_questions or ()),
                waiting_on=tuple(waiting_on or ()),
                recent_decision_refs=tuple(recent_decision_refs or ()),
                important_context_refs=tuple(important_context_refs or ()),
                last_event_seq=last_event_seq(session, context.project_id),
            )
            written = CheckpointRepository(session, context.project_id).write(
                checkpoint, actor_id=identity.identity_id
            )
        return as_json(written)

    return write_master_checkpoint


def read_master_checkpoint(context: ToolContext) -> Any:
    """Read the newest checkpoint, if there is one."""

    async def read_master_checkpoint() -> dict[str, Any]:
        """Read the most recent checkpoint written for this project.

        On recovery, read this first and then re-derive every claim in it from
        the project state before acting on it.
        """
        require_master(context, "read_master_checkpoint")
        with context.read() as session:
            checkpoint = CheckpointRepository(session, context.project_id).latest()
        return {
            "checkpoint": as_json(checkpoint) if checkpoint is not None else None,
            "found": checkpoint is not None,
        }

    return read_master_checkpoint


IMPLEMENTATIONS: dict[str, Any] = {
    "whoami": whoami,
    "read_project_state": read_project_state,
    "list_pending_approvals": list_pending_approvals,
    "write_master_checkpoint": write_master_checkpoint,
    "read_master_checkpoint": read_master_checkpoint,
}
