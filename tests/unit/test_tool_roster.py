"""The tool roster is the authorization record, and it must stay coherent.

If a tool is granted but not implemented, a role boots with a broken server.
If a tool is granted to a role whose contract forbids the action, the model can
be talked into doing it. Both are caught here, statically.
"""

from __future__ import annotations

import pytest

from ravel.dsh.roles import AgentRole
from ravel.mcp.registry import (
    DAG_MUTATION_TOOLS,
    TOOL_DESCRIPTIONS,
    TOOL_ROLES,
    WRITE_AUTHORSHIP,
    WRITE_TOOLS,
    mutating_tools_for,
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


# ── The DAG-mutation boundary ───────────────────────────────────────────────


def test_master_holds_every_dag_mutation_tool() -> None:
    assert set(mutating_tools_for(AgentRole.MASTER)) == set(DAG_MUTATION_TOOLS)


@pytest.mark.parametrize(
    "role",
    [AgentRole.RESEARCH, AgentRole.REVIEW, AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER],
)
def test_no_other_role_holds_a_dag_mutation_tool(role: AgentRole) -> None:
    """The separation of powers, as a property of the table.

    `tests/integration/roles` asserts the same thing against the running
    servers. This one exists so that a mis-edit fails in a second, in the
    fastest suite, rather than only where the database is available.
    """
    assert mutating_tools_for(role) == ()
    assert set(tools_for(role)).isdisjoint(DAG_MUTATION_TOOLS)


def test_every_dag_mutation_tool_is_granted_to_master() -> None:
    """A mutation tool granted to nobody is dead code wearing a scary name."""
    for name in DAG_MUTATION_TOOLS:
        assert TOOL_ROLES[name] == frozenset({AgentRole.MASTER})


def test_every_writing_tool_belongs_to_exactly_one_role() -> None:
    """A writing tool is held by its author and by nobody else.

    The write set is named explicitly so that a read tool which starts writing,
    or a writing tool handed to a second role, has to be noticed in two places
    rather than one — the roster and the table that says who the author is.
    """
    assert DAG_MUTATION_TOOLS <= WRITE_TOOLS
    assert set(WRITE_TOOLS) == set().union(*WRITE_AUTHORSHIP.values())
    for role, authored in WRITE_AUTHORSHIP.items():
        for name in authored:
            assert name in TOOL_ROLES, f"{name} writes but is not part of the RAVEL roster"
            assert TOOL_ROLES[name] == frozenset({role}), (
                f"{name} is {role.value}'s to write, so no other role may hold it"
            )


def test_master_writes_the_plan_and_review_writes_verdicts() -> None:
    """The two authorship sets are disjoint, and neither contains the other's.

    Stated separately from the role loop above because this is the separation
    of powers itself: Master cannot record a verdict on its own work, and
    Review cannot change the plan it judges.
    """
    assert WRITE_AUTHORSHIP[AgentRole.MASTER].isdisjoint(WRITE_AUTHORSHIP[AgentRole.REVIEW])
    assert DAG_MUTATION_TOOLS.issubset(WRITE_AUTHORSHIP[AgentRole.MASTER])
    assert "submit_review" not in WRITE_AUTHORSHIP[AgentRole.MASTER]


@pytest.mark.parametrize(
    "role",
    [AgentRole.RESEARCH, AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER],
)
def test_a_role_with_no_authorship_holds_no_writing_tool_at_all(role: AgentRole) -> None:
    """A worker's contract is "execute this and report"; it does not write state.

    Stated as a disjointness rather than a fixed roster: later phases give the
    workers tools of their own, and this rule has to keep holding as they do —
    a worker that needs to write a record is a role whose authorship has to be
    added to the table deliberately, not a role that quietly acquired a tool.
    """
    assert role not in WRITE_AUTHORSHIP
    assert set(tools_for(role)).isdisjoint(WRITE_TOOLS)
