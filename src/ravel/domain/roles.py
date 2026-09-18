"""The five RAVEL agent roles.

Exactly five roles exist, and each is a fixed contract rather than a label.
The role decides the behavioral prompt an agent runs under and the tools it can
reach. Nothing else in RAVEL may add a role: a deterministic responsibility
belongs to a software component, never to another agent.

This enum lives in the domain because "RAVEL has five roles" is a product fact.
How each role is *composed into a harness runtime* is a DSH concern and lives
in `ravel.dsh.roles`.
"""

from __future__ import annotations

from enum import StrEnum


class AgentRole(StrEnum):
    """The five agent roles. The value is the on-the-wire role name."""

    MASTER = "master"
    RESEARCH = "research"
    REVIEW = "review"
    COMPUTE_WORKER = "compute-worker"
    EXPERIMENTAL_WORKER = "experimental-worker"

    @property
    def slug(self) -> str:
        """A filesystem-safe form, used for runtime directories."""
        return self.value

    @property
    def display_name(self) -> str:
        """A human-readable name for logs, the TUI, and the API."""
        return {
            AgentRole.MASTER: "Master",
            AgentRole.RESEARCH: "Research Agent",
            AgentRole.REVIEW: "Review Agent",
            AgentRole.COMPUTE_WORKER: "Compute Worker",
            AgentRole.EXPERIMENTAL_WORKER: "Experimental Worker",
        }[self]

    @property
    def is_task_scoped(self) -> bool:
        """Whether one assignment fully describes this role's authority.

        Workers act on a single explicit task. Master, Research, and Review act
        on the project as a whole, so a task identifier never bounds them.
        """
        return self in {AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER}

    @property
    def mutates_dag(self) -> bool:
        """Whether this role may change the Scientific DAG.

        Only Master may. Every other role requests a change through Master;
        none of them holds the capability.
        """
        return self is AgentRole.MASTER

    @property
    def is_executor(self) -> bool:
        """Whether this role performs formal computation or experiment work."""
        return self in {AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER}

    @property
    def is_independent_of_master(self) -> bool:
        """Whether this role's judgement is recorded independently of Master.

        Review and Research report what they found; Master decides what it
        means. Collapsing the two would remove the check on Master's reasoning.
        """
        return self in {AgentRole.RESEARCH, AgentRole.REVIEW}
