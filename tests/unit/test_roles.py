"""The five roles are fixed, and each carries a real behavioral contract."""

from __future__ import annotations

import pytest

from ravel.dsh.roles import ROLE_DEFINITIONS, AgentRole, definition_for


def test_exactly_five_roles_exist() -> None:
    """Adding a sixth role is a product decision, not an implementation detail."""
    assert len(AgentRole) == 5
    assert {r.value for r in AgentRole} == {
        "master",
        "research",
        "review",
        "compute-worker",
        "experimental-worker",
    }


def test_every_role_has_a_definition() -> None:
    assert set(ROLE_DEFINITIONS) == set(AgentRole)


def test_unknown_role_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown agent role"):
        definition_for("planner")


def test_no_agent_role_is_smuggled_in_as_a_string() -> None:
    """A deterministic responsibility must not arrive as a new agent type."""
    for forbidden in ("planner", "memory", "router", "safety", "scheduler"):
        with pytest.raises(ValueError):
            definition_for(forbidden)


@pytest.mark.parametrize("role", list(AgentRole))
def test_each_role_prompt_exists_and_is_substantial(role: AgentRole) -> None:
    definition = definition_for(role)
    assert definition.prompt_file.is_file(), f"{role.value} has no prompt file"
    persona = definition.persona
    assert len(persona) > 200, f"{role.value} prompt is too thin to be a contract"
    assert "RAVEL" in persona


@pytest.mark.parametrize("role", list(AgentRole))
def test_each_role_serves_the_ravel_tool_server(role: AgentRole) -> None:
    assert definition_for(role).mcp_module == "ravel.mcp.server"


def test_only_master_may_mutate_the_dag() -> None:
    assert AgentRole.MASTER.mutates_dag is True
    for role in AgentRole:
        if role is not AgentRole.MASTER:
            assert role.mutates_dag is False, f"{role.value} must not mutate the DAG"


def test_worker_roles_are_task_scoped() -> None:
    assert AgentRole.COMPUTE_WORKER.is_task_scoped
    assert AgentRole.EXPERIMENTAL_WORKER.is_task_scoped
    # Master and Review act on a whole project, so no task bounds them.
    assert not AgentRole.MASTER.is_task_scoped
    assert not AgentRole.RESEARCH.is_task_scoped
    assert not AgentRole.REVIEW.is_task_scoped


def test_role_values_are_filesystem_safe() -> None:
    """Role values become directory names under the runtime root."""
    for role in AgentRole:
        assert role.slug == role.slug.lower()
        assert "/" not in role.slug and ".." not in role.slug
