"""How each role is composed into a harness runtime.

The role *set* is a domain fact and lives in `ravel.domain.roles`. What is
harness-specific is the rest: which behavioral prompt a role boots with and
which tool server it mounts.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ravel.domain.roles import AgentRole as AgentRole  # re-exported for the harness seam

REPO_ROOT = Path(__file__).resolve().parents[3]
PROMPTS_DIR = REPO_ROOT / "prompts"

__all__ = ["ROLE_DEFINITIONS", "AgentRole", "RoleDefinition", "definition_for"]


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
