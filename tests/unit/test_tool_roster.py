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


def test_the_roster_agrees_with_the_authorship_record() -> None:
    """Every writing tool is held by exactly the roles the table says wrote it.

    The write set is named explicitly so that a read tool which starts writing,
    or a writing tool handed to a second role, has to be noticed in two places
    rather than one — the roster and the table that says who the author is.
    Editing one without the other is what this catches.
    """
    assert DAG_MUTATION_TOOLS <= WRITE_TOOLS
    assert set(WRITE_TOOLS) == set().union(*WRITE_AUTHORSHIP.values())
    for name in WRITE_TOOLS:
        authors = frozenset(
            role for role, authored in WRITE_AUTHORSHIP.items() if name in authored
        )
        assert name in TOOL_ROLES, f"{name} writes but is not part of the RAVEL roster"
        assert TOOL_ROLES[name] == authors, (
            f"{name} is held by {sorted(r.value for r in TOOL_ROLES[name])} and the "
            f"authorship record says {sorted(r.value for r in authors)}"
        )


def test_the_four_execution_and_review_powers_do_not_overlap() -> None:
    """Master, Review, Research and the Workers write four disjoint records.

    Stated separately from the loop above because this is the separation of
    powers itself: Master cannot record a verdict on its own work or evidence
    for it, Review cannot change the plan it judges, and Research cannot decide
    what its evidence means for the plan.
    """
    master = WRITE_AUTHORSHIP[AgentRole.MASTER]
    others = frozenset().union(
        *(
            authored
            for role, authored in WRITE_AUTHORSHIP.items()
            if role is not AgentRole.MASTER
        )
    )
    assert master.isdisjoint(others)
    assert DAG_MUTATION_TOOLS.issubset(master)
    assert "submit_review" not in master
    assert "register_source" not in master


@pytest.mark.parametrize("role", list(AgentRole))
def test_a_role_writes_only_what_the_table_gives_it(role: AgentRole) -> None:
    """A role's writing tools are exactly its authored set, or none at all.

    Stated as an equality in both directions rather than a disjointness: a
    role that holds a writing tool the table does not give it is a role that
    acquired one quietly, and a role whose authored set is empty holds nothing
    that writes.
    """
    assert set(tools_for(role)) & WRITE_TOOLS == set(WRITE_AUTHORSHIP.get(role, frozenset()))
