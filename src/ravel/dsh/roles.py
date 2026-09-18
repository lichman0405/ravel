"""The five RAVEL agent roles.

Exactly five roles exist, and each is a fixed contract rather than a label: the
role decides the behavioral prompt the agent runs under and the tools it can
reach. Nothing else in RAVEL may add a role — deterministic responsibilities
belong to software components, not to another agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "prompts"


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

        Workers and Research act on a single explicit task. Master and Review
        act on the project as a whole, so a task identifier never bounds them.
        """
        return self in {AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER}

    @property
    def mutates_dag(self) -> bool:
        """Whether this role may change the Scientific DAG.

        Only Master may. Every other role requests a change through Master;
        none of them holds the capability.
        """
        return self is AgentRole.MASTER


@dataclass(frozen=True, slots=True)
class RoleDefinition:
    """Everything that distinguishes one role from another at runtime."""

    role: AgentRole
    prompt_file: Path
    mcp_module: str

    @property
    def persona(self) -> str:
        """The role's behavioral contract, read from the shipped prompt file.

        Read eagerly so a missing contract fails at composition rather than
        producing an agent with no instructions.
        """
        return self.prompt_file.read_text(encoding="utf-8")


ROLE_DEFINITIONS: dict[AgentRole, RoleDefinition] = {
    AgentRole.MASTER: RoleDefinition(
        role=AgentRole.MASTER,
        prompt_file=PROMPTS_DIR / "master_role.md",
        mcp_module="ravel.mcp.server",
    ),
    AgentRole.RESEARCH: RoleDefinition(
        role=AgentRole.RESEARCH,
        prompt_file=PROMPTS_DIR / "research_role.md",
        mcp_module="ravel.mcp.server",
    ),
    AgentRole.REVIEW: RoleDefinition(
        role=AgentRole.REVIEW,
        prompt_file=PROMPTS_DIR / "review_role.md",
        mcp_module="ravel.mcp.server",
    ),
    AgentRole.COMPUTE_WORKER: RoleDefinition(
        role=AgentRole.COMPUTE_WORKER,
        prompt_file=PROMPTS_DIR / "compute_worker_role.md",
        mcp_module="ravel.mcp.server",
    ),
    AgentRole.EXPERIMENTAL_WORKER: RoleDefinition(
        role=AgentRole.EXPERIMENTAL_WORKER,
        prompt_file=PROMPTS_DIR / "experimental_worker_role.md",
        mcp_module="ravel.mcp.server",
    ),
}


def definition_for(role: AgentRole | str) -> RoleDefinition:
    """Look up a role's definition, rejecting anything outside the five."""
    try:
        return ROLE_DEFINITIONS[AgentRole(role)]
    except ValueError as exc:
        known = ", ".join(r.value for r in AgentRole)
        raise ValueError(f"unknown agent role {role!r}; known roles: {known}") from exc
