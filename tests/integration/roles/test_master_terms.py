"""P11-04: the terms a node runs under, written by Master's own tools.

An Execution Contract is what a Worker is checked against, and Phase 11 gave it
terms a node that computes or experiments cannot be specified without: the
values the run uses, the windows they may move inside, what it must deliver,
what stops it, and — the one this item is about — whether RAVEL must build an
environment before it runs at all.

A term the schema carries and the planning path cannot write is a term no node
will ever run under, so the assertion here is made at the model's side of the
boundary: a tool call, through a server launched the way the harness launches
one, read back out of PostgreSQL field by field. Two directions, because the
failure this closes went both ways: every term the contract declares is
*writable*, and a term that is not a value the contract can hold is *refused*
where the model can read why — with the plan left exactly as it was.
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.dsh.mcp_probe import ToolCall, probe
from tests.integration.dag.conftest import STAGES, register_roadmap
from tests.integration.roles.conftest import RoleEnvironment

from ravel.domain.contracts import ExecutionContract
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository

pytestmark = pytest.mark.integration

#: One node's terms as a model states them, with every field the contract
#: declares given a value. Written out rather than built from the model, so a
#: field added to `ExecutionContract` without a way to write it fails here.
TERMS: dict[str, Any] = {
    "criteria": [
        {
            "statement": "The simulated uptake matches the measured isotherm.",
            "provenance": "literature_derived",
            "provenance_ref": "10.1000/example",
            "metric": "uptake",
            "threshold": "within 10%",
        }
    ],
    "inputs": ["niobium_structure.cif", "force_field.def"],
    "allowed_actions": ["submit", "collect_outputs"],
    "required_outputs": ["isotherm.csv"],
    "parameter_targets": {"temperature_k": "298", "pressure_bar": "1.0"},
    "allowed_ranges": {"temperature_k": "270..330", "pressure_bar": "0.5..2"},
    "allowed_substitutions": ["ethanol -> methanol"],
    "allowed_retries": 2,
    "stop_conditions": ["the run exceeds its wall-clock limit"],
    "escalation_conditions": ["the backend reports a value outside the permitted range"],
    "resource_limits": {"nodes": "1", "wall_clock_hours": "2"},
    "execution_requirements": {"software": "raspa"},
    "procedure": "Simulate the isotherm at the stated temperature and pressure.",
}

COMPUTATION: dict[str, Any] = {
    "node_type": "COMPUTATION",
    "objective": "Simulate the adsorption isotherm for the doped framework.",
    "rationale": "The measurement cannot be planned without a predicted isotherm.",
}


def payload(call: ToolCall) -> dict[str, Any]:
    """One call's structured result, with the ways it can be absent ruled out."""
    assert not call.failed, f"{call.tool} failed: {call.error}"
    assert call.payload is not None, f"{call.tool} returned no structured payload"
    return call.payload


def stored_contract(database: Database, project: Project, node_id: str) -> ExecutionContract:
    """The contract PostgreSQL holds for a node, read without going through a tool."""
    with database.read_only() as session:
        node = DagRepository(session, project.project_id).node(node_id)
        assert node.execution_contract_ref, "the node was committed with no contract"
        return ExecutionContractRepository(session, project.project_id).get(
            contract_id=node.execution_contract_ref
        )


def nothing_was_planned(database: Database, project: Project) -> None:
    """The plan and its decisions are as they were, after a refused call."""
    with database.read_only() as session:
        assert DagRepository(session, project.project_id).nodes() == []


async def test_master_writes_every_term_the_contract_can_carry(
    role_environment: RoleEnvironment, project: Project, database: Database
) -> None:
    """The write path against the schema, field by field.

    Read from PostgreSQL rather than from the call's own answer: a tool that
    echoed what it was given would pass a test of its return value while
    writing nothing, and what a Worker checks an action against is the row.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(("add_dag_node", {**COMPUTATION, **TERMS}),),
    )
    written = payload(result.calls[0])
    contract = stored_contract(database, project, str(written["node_id"]))

    assert contract.objective == COMPUTATION["objective"]
    assert contract.procedure == TERMS["procedure"]
    assert contract.inputs == tuple(TERMS["inputs"])
    assert contract.allowed_actions == tuple(TERMS["allowed_actions"])
    assert contract.required_outputs == tuple(TERMS["required_outputs"])
    assert contract.parameter_targets == TERMS["parameter_targets"]
    assert contract.allowed_ranges == TERMS["allowed_ranges"]
    assert contract.allowed_substitutions == tuple(TERMS["allowed_substitutions"])
    assert contract.allowed_retries == TERMS["allowed_retries"]
    assert contract.stop_conditions == tuple(TERMS["stop_conditions"])
    assert contract.escalation_conditions == tuple(TERMS["escalation_conditions"])
    assert contract.resource_limits == TERMS["resource_limits"]
    assert contract.execution_requirements == TERMS["execution_requirements"]
    assert contract.acceptance_contract_ref, (
        "the criteria the model wrote were not bound to the contract that is "
        "measured against them"
    )
    # Frozen with the node, because a run's terms are the ones written before
    # it started rather than the ones in the table when somebody looks.
    assert contract.is_frozen

    # And the contract's own reading of what it now requires, which is what the
    # execution loop asks before it prepares anything.
    assert contract.requires_preparation
    assert contract.permits_value("temperature_k", "310")
    assert not contract.permits_value("temperature_k", "400")
    assert contract.permits_substitution("ethanol", "methanol")
    assert not contract.permits_substitution("methanol", "ethanol")


async def test_a_node_that_names_no_environment_requires_no_preparation(
    role_environment: RoleEnvironment, project: Project, database: Database
) -> None:
    """The compatibility rule, at the boundary a live run goes through.

    Most nodes name no environment, and none of them may start waiting on a
    workspace that was never meant to exist for them. This is the same
    assertion `tests/unit` makes on the contract, made again on what a tool
    call actually writes.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "add_dag_node",
                {
                    "node_type": "RESEARCH",
                    "objective": "Survey the literature on the doped framework.",
                    "rationale": "The stage cannot be planned without it.",
                },
            ),
        ),
    )
    written = payload(result.calls[0])
    contract = stored_contract(database, project, str(written["node_id"]))

    assert contract.execution_requirements == {}
    assert not contract.requires_preparation


async def test_a_stage_writes_the_terms_of_every_node_in_it(
    role_environment: RoleEnvironment, project: Project, database: Database
) -> None:
    """The other planning path, which writes terms per node from a list.

    `expand_dag_phase` is how a stage is committed, so a term that only
    `add_dag_node` could write would be a term most nodes never carry.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "expand_dag_phase",
                {
                    "phase": STAGES[0],
                    "rationale": "The stage has a measurement and a simulation.",
                    "nodes": [
                        {**COMPUTATION, "ref": "sim", **TERMS},
                        {
                            "node_type": "EXPERIMENT",
                            "objective": "Measure the isotherm at the bench.",
                            "dependencies": ["sim"],
                            # A node with dependencies states how it joins
                            # them; the model refuses one that does not, and
                            # this test is about the terms rather than about
                            # that rule.
                            "join_policy": "ALL",
                            "execution_requirements": {"lab": "bench-chemistry"},
                            "parameter_targets": {"temperature_k": "298"},
                            "allowed_actions": ["run_experiment"],
                            "required_outputs": ["measured_isotherm.csv"],
                            "criteria": [
                                {
                                    "statement": "The measured uptake matches the prediction.",
                                    "provenance": "user_requirement",
                                }
                            ],
                        },
                    ],
                },
            ),
        ),
    )
    expansion = payload(result.calls[0])
    by_objective = {node["objective"]: node["node_id"] for node in expansion["nodes"]}

    simulated = stored_contract(database, project, str(by_objective[COMPUTATION["objective"]]))
    measured = stored_contract(
        database, project, str(by_objective["Measure the isotherm at the bench."])
    )

    assert simulated.execution_requirements == {"software": "raspa"}
    assert simulated.parameter_targets == TERMS["parameter_targets"]
    assert simulated.allowed_ranges == TERMS["allowed_ranges"]
    assert simulated.required_outputs == ("isotherm.csv",)
    assert measured.execution_requirements == {"lab": "bench-chemistry"}
    assert measured.parameter_targets == {"temperature_k": "298"}
    assert measured.allowed_actions == ("run_experiment",)
    # A node the model wrote without them carries the empty ones rather than
    # whatever the node before it in the list had.
    assert measured.stop_conditions == ()
    assert measured.allowed_retries == 0


async def test_a_range_that_is_not_a_range_is_refused_and_plans_nothing(
    role_environment: RoleEnvironment, project: Project, database: Database
) -> None:
    """A range that cannot be read mechanically is refused where it is written.

    The Worker's rule is that it checks rather than judges, so a window written
    as a sentence is a window nothing can check against — and the refusal says
    how to write one, because the model reading it has to be able to correct
    itself in the next call.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "add_dag_node",
                {**COMPUTATION, **TERMS, "allowed_ranges": {"temperature_k": "warm"}},
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed, f"a range that is not one was accepted: {call.payload}"
    error = call.error or ""
    assert "temperature_k" in error
    assert "8..12" in error, f"the refusal does not say how to write a range: {error}"
    nothing_was_planned(database, project)


async def test_a_requirement_of_an_unknown_kind_is_refused_and_plans_nothing(
    role_environment: RoleEnvironment, project: Project, database: Database
) -> None:
    """The typo this vocabulary exists to catch.

    `softwre` must not become a node that runs with nothing prepared for it:
    the whole point of a closed list of requirement kinds is that a misspelling
    is refused by the schema rather than discovered by a Worker that starts a
    run in a directory that was never built.
    """
    register_roadmap(database, project, *STAGES)
    result = await probe(
        role_environment.for_project(project, AgentRole.MASTER),
        calls=(
            (
                "add_dag_node",
                {**COMPUTATION, **TERMS, "execution_requirements": {"softwre": "raspa"}},
            ),
        ),
    )

    call = result.calls[0]
    assert call.failed, f"an unknown requirement kind was accepted: {call.payload}"
    error = call.error or ""
    assert "softwre" in error
    assert "software" in error, f"the refusal does not name the kinds it accepts: {error}"
    nothing_was_planned(database, project)
