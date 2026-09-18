"""Which tools each role may reach.

This table is the authorization record. A tool that is not listed for a role is
never registered in that role's server process, so the agent cannot call it —
the model's tool list and RAVEL's grant are the same object, not two things
kept in sync.

Phase 0 establishes the mechanism and the two tools that prove it. Domain tools
are added per phase by extending this table; none of them may widen a role's
roster beyond what its behavioral contract already permits.
"""

from __future__ import annotations

from ravel.domain.roles import AgentRole

MASTER = AgentRole.MASTER
RESEARCH = AgentRole.RESEARCH
REVIEW = AgentRole.REVIEW
COMPUTE_WORKER = AgentRole.COMPUTE_WORKER
EXPERIMENTAL_WORKER = AgentRole.EXPERIMENTAL_WORKER

#: Tool name -> the roles whose server registers it.
TOOL_ROLES: dict[str, frozenset[AgentRole]] = {
    # Every role may confirm its own scope. An agent that cannot tell which
    # project and role it is serving cannot report a scoping fault.
    "whoami": frozenset({MASTER, RESEARCH, REVIEW, COMPUTE_WORKER, EXPERIMENTAL_WORKER}),
    # Reading authoritative project state is Master's alone. Workers receive a
    # frozen contract; Review receives the artifact under review; neither needs
    # the whole project, and giving it to them would let a worker re-scope its
    # own task.
    "read_project_state": frozenset({MASTER}),
}

#: Model-facing descriptions. Written from the agent's point of view: what the
#: tool returns and when to reach for it, with no transport or RAVEL vocabulary.
TOOL_DESCRIPTIONS: dict[str, str] = {
    "whoami": (
        "Report the project and role this session is serving, and the tools available to it. "
        "Use this to confirm your scope before acting."
    ),
    "read_project_state": (
        "Read the current authoritative state of this project: its objective, phase, and the "
        "agent sessions bound to it. This is the record of truth; do not rely on recollection "
        "of earlier conversation."
    ),
}


def tools_for(role: AgentRole) -> tuple[str, ...]:
    """The tool names a role's server registers, in a stable order."""
    return tuple(name for name in sorted(TOOL_ROLES) if role in TOOL_ROLES[name])


def roles_for(tool: str) -> frozenset[AgentRole]:
    """The roles permitted to call a tool.

    Raises:
        KeyError: The tool is not part of the RAVEL roster.
    """
    return TOOL_ROLES[tool]
