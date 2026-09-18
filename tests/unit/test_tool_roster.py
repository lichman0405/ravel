"""The tool roster is the authorization record, and it must stay coherent.

If a tool is granted but not implemented, a role boots with a broken server.
If a tool is granted to a role whose contract forbids the action, the model can
be talked into doing it. Both are caught here, statically.
"""

from __future__ import annotations

import pytest

from ravel.dsh.roles import AgentRole
from ravel.mcp.registry import (
    TOOL_DESCRIPTIONS,
    TOOL_ROLES,
    roles_for,
    tools_for,
)
from ravel.mcp.server import IMPLEMENTATIONS


@pytest.mark.parametrize("role", list(AgentRole))
def test_every_role_has_at_least_one_tool(role: AgentRole) -> None:
    """A role with no tools cannot act, and cannot even report its scope."""
    assert tools_for(role), f"{role.value} has an empty roster"


@pytest.mark.parametrize("role", list(AgentRole))
def test_every_role_can_confirm_its_own_scope(role: AgentRole) -> None:
    assert "whoami" in tools_for(role)


def test_master_has_the_project_state_tool() -> None:
    assert "read_project_state" in tools_for(AgentRole.MASTER)


@pytest.mark.parametrize(
    "role",
    [AgentRole.RESEARCH, AgentRole.REVIEW, AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER],
)
def test_no_non_master_role_reads_project_state(role: AgentRole) -> None:
    """Workers get a frozen contract; Review gets the artifact under review."""
    assert "read_project_state" not in tools_for(role)


def test_every_granted_tool_is_described_and_implemented() -> None:
    for name, roles in TOOL_ROLES.items():
        assert roles, f"tool {name!r} is granted to nobody and should not exist"
        assert TOOL_DESCRIPTIONS.get(name), f"tool {name!r} has no model-facing description"
        assert name in IMPLEMENTATIONS, f"tool {name!r} is granted but not implemented"


def test_no_orphan_descriptions_or_implementations() -> None:
    """A described or implemented tool that is granted to nobody is dead weight."""
    assert set(TOOL_DESCRIPTIONS) == set(TOOL_ROLES)
    assert set(IMPLEMENTATIONS) == set(TOOL_ROLES)


def test_roles_for_returns_the_roster() -> None:
    assert roles_for("read_project_state") == frozenset({AgentRole.MASTER})


def test_roles_for_rejects_an_unknown_tool() -> None:
    with pytest.raises(KeyError):
        roles_for("delete_project")


def test_tool_order_is_stable() -> None:
    """Registration order reaches the model; a reshuffle would churn prompts."""
    assert tools_for(AgentRole.MASTER) == tuple(sorted(tools_for(AgentRole.MASTER)))
