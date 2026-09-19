"""Phase 3 gate: the role permission matrix, against the running servers.

The registry says which role may reach which tool; `tests/unit` checks that the
registry is coherent. This module asks the thing itself. Every assertion here
is made against a tool server launched the way the harness launches one, over
real stdio, against a real PostgreSQL — because the property being claimed is
not "the table is right" but "a Research session has no way to change the
plan", and only the running process can answer that.

Two directions are checked for every role:

- **what is registered** — the roster the model would see, compared against the
  composition dump, so the dump is a claim with a falsifier rather than a
  restatement of the table;
- **what happens when it asks anyway** — a non-Master server is called with a
  DAG mutation, and the answer must be a refusal, with nothing left in the
  database afterwards.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.dsh.mcp_probe import probe
from tests.integration.dag.conftest import STAGES, register_roadmap
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.contracts import CriterionProvenance
from ravel.domain.enums import NodeStatus, ReviewOutcome
from ravel.domain.roles import AgentRole
from ravel.dsh.composition import composition_dump
from ravel.mcp.registry import DAG_MUTATION_TOOLS
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.identity import CheckpointRepository
from ravel.state.repositories.records import DecisionRepository

WORKERS = tuple(role for role in AgentRole if role is not AgentRole.MASTER)

#: Terms a node runs under, as a model states them. A COMPUTATION node is
#: measured against criteria frozen before it runs, so a plan that committed one
#: without any would be a node no Worker may ever start.
TERMS: dict[str, Any] = {
    "criteria": [
        {
            "statement": "Conductivity rises by at least 15% across the series.",
            "provenance": "user_requirement",
            "threshold": ">= 15%",
        }
    ],
    "allowed_actions": ["run_simulation"],
    "required_outputs": ["conductivity.csv"],
}

#: A node creation as the model would phrase it. Every argument is one a model
#: could supply; none of them names a project, because the tool has no such
#: parameter.
A_NODE_CREATION: dict[str, Any] = {
    "node_type": "RESEARCH",
    "objective": "Survey the literature on the dopant series.",
    "rationale": "The stage cannot be planned without knowing what is already published.",
}


@pytest.fixture(scope="module")
def dump() -> dict[str, dict[str, Any]]:
    """The composition dump, keyed by role name."""
    entries = composition_dump(
        project_id="proj-dump",
        mcp_command="/usr/bin/python3",
        brief_dir=Path("/tmp/ravel-composition-dump"),
    )
    return {str(entry["role"]): entry for entry in entries}


# ── The roster the model sees ───────────────────────────────────────────────


def test_the_dump_covers_every_role(dump: dict[str, dict[str, Any]]) -> None:
    assert set(dump) == {role.value for role in AgentRole}


@pytest.mark.parametrize("role", list(AgentRole))
async def test_a_roles_server_registers_exactly_what_the_dump_promises(
    role: AgentRole,
    dump: dict[str, dict[str, Any]],
    role_environment: RoleEnvironment,
    project: Any,
) -> None:
    """The dump is a description of a server; this is the server."""
    result = await probe(role_environment.for_project(project, role))

    assert set(result.tools) == set(dump[role.value]["tools"])
    assert result.whoami is not None
    assert result.whoami["role"] == role.value
    assert result.whoami["project_id"] == project.project_id
    # The tool's own report and the registration agree, so neither can drift.
    assert set(result.whoami["tools"]) == set(result.tools)


def test_only_master_holds_a_tool_that_changes_the_dag(dump: dict[str, dict[str, Any]]) -> None:
    """The one invariant that holds across every phase of the build."""
    master = dump[AgentRole.MASTER.value]
    assert set(master["dag_mutation_tools"]) == set(DAG_MUTATION_TOOLS)

    for role_value, entry in dump.items():
        if role_value == AgentRole.MASTER.value:
            continue
        assert entry["dag_mutation_tools"] == [], f"{role_value} claims DAG mutation authority"
        assert set(entry["tools"]).isdisjoint(DAG_MUTATION_TOOLS), (
            f"{role_value} is offered {sorted(set(entry['tools']) & DAG_MUTATION_TOOLS)}"
        )


# ── What happens when a non-Master asks anyway ──────────────────────────────


@pytest.mark.parametrize("role", WORKERS)
async def test_a_non_master_server_does_not_register_a_dag_mutation_tool(
    role: AgentRole, role_environment: RoleEnvironment, project: Any
) -> None:
    result = await probe(role_environment.for_project(project, role))

    assert set(result.tools).isdisjoint(DAG_MUTATION_TOOLS)
    assert result.whoami is not None
    assert result.whoami["may_mutate_dag"] is False


@pytest.mark.parametrize("role", WORKERS)
async def test_a_non_master_asking_to_change_the_dag_is_refused(
    role: AgentRole, role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """Refused at the transport, not merely discouraged in a prompt.

    The call is made deliberately rather than assumed impossible: a server that
    registered the tool and refused it inside the handler would also pass a
    test that only read the roster, and that server would be one mis-edit away
    from changing the plan.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, role), calls=(("add_dag_node", A_NODE_CREATION),)
    )

    call = result.calls[0]
    assert call.failed, f"{role.value} was allowed to add a node: {call.payload}"
    assert "Unknown tool" in (call.error or "")

    with database.read_only() as session:
        assert DagRepository(session, project.project_id).nodes() == []
        assert DecisionRepository(session, project.project_id).all() == []


# ── Master, end to end: the tool writes, PostgreSQL holds it ────────────────


async def test_master_commits_a_node_through_its_own_server(
    role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """The whole point of Phase 3, in one test: a tool call becomes project state."""
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(("add_dag_node", A_NODE_CREATION),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["objective"] == A_NODE_CREATION["objective"]
    assert call.payload["created_by"] == AgentRole.MASTER.value

    with database.read_only() as session:
        stored = DagRepository(session, project.project_id).node(str(call.payload["node_id"]))
        decisions = DecisionRepository(session, project.project_id).all()

    assert stored.objective == A_NODE_CREATION["objective"]
    # A node is never written without the decision that ordered it, and the
    # decision names the node it created — in both directions.
    assert stored.decision_ref
    assert [record.decision_id for record in decisions] == [stored.decision_ref]
    assert call.payload["node_id"] in decisions[0].affected_nodes.created


async def test_master_expands_a_stage_and_the_siblings_depend_on_each_other(
    role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """A stage's analysis committed in the same call as the measurement it reads.

    The dependency is written as a `ref` — a name local to the call — because a
    caller cannot know the node ids RAVEL has not assigned yet. The edges that
    land in the database must be between real node ids.
    """
    register_roadmap(database, project, *STAGES)
    stage = STAGES[1]
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "expand_dag_phase",
                {
                    "phase": stage,
                    "rationale": "The stage is planned as a measurement and the fit that reads it.",
                    "nodes": [
                        {
                            "ref": "measure",
                            "node_type": "COMPUTATION",
                            "objective": "Measure.",
                            **TERMS,
                        },
                        {
                            "node_type": "COMPUTATION",
                            "objective": "Fit the model.",
                            "dependencies": ["measure"],
                            "join_policy": "ALL",
                            **TERMS,
                        },
                    ],
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    written = {node["objective"]: node for node in call.payload["nodes"]}
    assert set(written) == {"Measure.", "Fit the model."}
    assert all(node["roadmap_phase"] == stage for node in call.payload["nodes"])

    with database.read_only() as session:
        dag = DagRepository(session, project.project_id)
        edges = dag.edges()
        fit = dag.node(written["Fit the model."]["node_id"])
        waiting = dag.join_state(fit)

    assert fit.dependencies == (written["Measure."]["node_id"],)
    assert [(edge.from_node, edge.to_node) for edge in edges] == [
        (written["Measure."]["node_id"], written["Fit the model."]["node_id"])
    ]
    # A node with a fan-in waits for it rather than starting immediately.
    assert waiting == "waiting"


async def test_a_stage_beyond_the_horizon_is_refused_with_a_readable_reason(
    role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """The rolling horizon, as the agent experiences it.

    An anticipated refusal has to reach the model as a message, and the
    transport only carries one if the handler reports it rather than letting it
    escape as a crash. A crash arrives as `Error executing tool
    expand_dag_phase` and stops there; a reported refusal arrives as that same
    prefix followed by the reason. So the assertion is about what comes after
    the name, and this test is what keeps the explanation on the wire.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "expand_dag_phase",
                {
                    "phase": STAGES[3],
                    "rationale": "Planning the last stage now.",
                    "nodes": [{"node_type": "REVIEW", "objective": "Review everything."}],
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert call.error is not None
    prefix = "Error executing tool expand_dag_phase"
    assert call.error.startswith(prefix)
    reason = call.error[len(prefix) :].lstrip(": ")
    assert reason, "the refusal was collapsed into a bare 'Error executing tool'"
    # The reason names what is reachable, so the agent can choose again.
    assert STAGES[3] in reason
    assert STAGES[0] in reason

    with database.read_only() as session:
        assert DagRepository(session, project.project_id).nodes() == []
        assert DecisionRepository(session, project.project_id).all() == []


async def test_a_node_planned_through_the_tool_can_actually_run(
    role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """The terms travel with the node, and the DAG agrees the node may start.

    This is the difference between a plan and a graph. `can_enter_running` is
    what a Worker's start is checked against — frozen acceptance criteria for
    the node types that have them, and an Execution Contract for every node a
    Worker executes — and a plan committed without either would leave work in
    the project that no Worker may start, with nothing saying why.

    Asked of the domain rather than of the tables: whether the criteria are
    frozen *and* bound is exactly the question `can_enter_running` asks. The
    gate is asked from READY — a freshly planned node is PLANNED, and PLANNED
    cannot reach RUNNING at all, which would refuse the node before the terms
    were ever consulted and make this test pass for the wrong reason.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "add_dag_node",
                {
                    "node_type": "COMPUTATION",
                    "objective": "Measure conductivity across the dopant series.",
                    "rationale": "The stage's first question is what the samples conduct.",
                    **TERMS,
                },
            ),
        ),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    node_id = str(call.payload["node_id"])

    with database.read_only() as session:
        dag = DagRepository(session, project.project_id)
        node = dag.node(node_id)
        criteria = AcceptanceContractRepository(
            session, project.project_id
        ).frozen_for_node(node_id)
        terms = ExecutionContractRepository(session, project.project_id).for_node(node_id)
        frozen = dag.has_frozen_acceptance(node_id)
        contracted = dag.has_execution_contract(node_id)
        ready = node.model_copy(update={"status": NodeStatus.READY})
        unreviewed = ready.can_enter_running(
            has_frozen_acceptance=frozen,
            has_execution_contract=contracted,
            pre_run_outcome=dag.latest_pre_run_outcome(node_id),
        )
        reviewed = ready.can_enter_running(
            has_frozen_acceptance=frozen,
            has_execution_contract=contracted,
            pre_run_outcome=ReviewOutcome.PASS,
        )

    assert frozen, "the DAG does not see a frozen acceptance contract for this node"
    assert contracted, "the DAG does not see an Execution Contract for this node"
    assert criteria is not None, "the node's criteria were never frozen"
    assert [criterion.statement for criterion in criteria.criteria] == [
        TERMS["criteria"][0]["statement"]
    ]
    assert criteria.criteria[0].provenance is CriterionProvenance.USER_REQUIREMENT
    assert criteria.is_frozen
    assert node.acceptance_contract_ref == criteria.contract_id

    assert terms.is_frozen
    assert terms.allowed_actions == tuple(TERMS["allowed_actions"])
    assert terms.required_outputs == tuple(TERMS["required_outputs"])
    assert terms.acceptance_contract_ref == criteria.contract_id, (
        "the terms say what the run will be measured against, or a reader of the "
        "contract has to find that out from somewhere else"
    )

    # A COMPUTATION node also owes a pre-flight review, which is a different
    # checkpoint produced by a different role — so the plan is checked for
    # being the *only* thing standing in the way. Unreviewed, the node is
    # refused, and the refusal names the review rather than the terms.
    assert not unreviewed.allowed
    assert "reviewed" in unreviewed.reason
    assert reviewed.allowed, reviewed.reason


async def test_a_node_that_could_never_run_is_refused_with_a_readable_reason(
    role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """A COMPUTATION node with no criteria is work no Worker may start.

    Refused at the moment it is planned rather than left in the DAG for the
    scheduler to find: the alternative is a node that sits in PLANNED forever
    with a project that never ends and nothing in the record saying why.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "add_dag_node",
                {
                    "node_type": "COMPUTATION",
                    "objective": "Measure something nobody defined.",
                    "rationale": "The stage needs a measurement.",
                },
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed
    assert call.error is not None
    prefix = "Error executing tool add_dag_node"
    assert call.error.startswith(prefix)
    reason = call.error[len(prefix) :].lstrip(": ")
    assert "acceptance criteria" in reason, (
        f"the refusal has to say what was missing; it said {reason!r}"
    )

    with database.read_only() as session:
        assert DagRepository(session, project.project_id).nodes() == [], (
            "a refused plan leaves nothing behind, terms or nodes"
        )
        assert (
            AcceptanceContractRepository(session, project.project_id).all() == []
        ), "the criteria are written in the node's transaction, so a refusal takes them too"


async def test_master_reads_the_project_it_is_actually_serving(
    role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """State comes from PostgreSQL, not from the brief the process launched with."""
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(("read_project_state", {}),),
    )

    call = result.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["project_id"] == project.project_id
    assert call.payload["title"] == project.title
    assert call.payload["objective"] == project.objective
    assert [phase["name"] for phase in call.payload["roadmap"]] == list(STAGES)
    assert call.payload["current_phase"] == STAGES[0]
    assert call.payload["dag"]["nodes"] == 0


async def test_master_writes_a_checkpoint_and_reads_it_back(
    role_environment: RoleEnvironment, project: Any, database: Any
) -> None:
    """Recovery is a feature of the tools, so it is exercised through them."""
    environment = role_environment.for_project(project, AgentRole.MASTER)
    written = await probe(
        environment,
        calls=(
            (
                "write_master_checkpoint",
                {
                    "current_focus": "Choosing the first measurement.",
                    "waiting_on": ["the owner's approval of the budget"],
                },
            ),
        ),
    )
    assert not written.calls[0].failed, written.calls[0].error

    read_back = await probe(environment, calls=(("read_master_checkpoint", {}),))
    call = read_back.calls[0]
    assert not call.failed, call.error
    assert call.payload is not None
    assert call.payload["found"] is True
    checkpoint = call.payload["checkpoint"]
    assert checkpoint["current_focus"] == "Choosing the first measurement."
    assert checkpoint["waiting_on"] == ["the owner's approval of the budget"]

    with database.read_only() as session:
        latest = CheckpointRepository(session, project.project_id).latest()
    assert latest is not None
    assert latest.checkpoint_id == checkpoint["checkpoint_id"]
