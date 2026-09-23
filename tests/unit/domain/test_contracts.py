"""Contracts are the authority model, so their invariants are load-bearing."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    ApprovalRequirement,
    AuthorityEnvelope,
    ExecutionContract,
    ProjectSuccessContract,
    ResearchContract,
)
from ravel.domain.enums import CriterionProvenance, UserRole


def _criterion(**overrides: object) -> AcceptanceCriterion:
    defaults: dict[str, object] = {
        "statement": "Ki below 10 nM",
        "metric": "Ki",
        "threshold": "< 10 nM",
        "provenance": CriterionProvenance.USER_REQUIREMENT,
    }
    defaults.update(overrides)
    return AcceptanceCriterion(**defaults)  # type: ignore[arg-type]


# ── Provenance ──────────────────────────────────────────────────────────────


def test_a_criterion_must_declare_where_it_came_from() -> None:
    """There is no default, so an unrecorded origin cannot happen by omission."""
    with pytest.raises(ValidationError, match="provenance"):
        AcceptanceCriterion(statement="Ki below 10 nM")  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "provenance",
    [
        CriterionProvenance.LITERATURE_DERIVED,
        CriterionProvenance.STANDARD,
        CriterionProvenance.AUTHORITATIVE_DATABASE,
        CriterionProvenance.PRIOR_PROJECT_RESULT,
    ],
)
def test_a_cited_criterion_must_name_its_source(provenance: CriterionProvenance) -> None:
    with pytest.raises(ValidationError, match="requires provenance_ref"):
        _criterion(provenance=provenance)


def test_a_provisional_criterion_needs_no_reference() -> None:
    """Demanding one would push an author toward claiming a source they lack."""
    criterion = _criterion(provenance=CriterionProvenance.PROVISIONAL)
    assert criterion.provenance_ref is None


def test_a_user_requirement_needs_no_external_reference() -> None:
    assert _criterion(provenance=CriterionProvenance.USER_REQUIREMENT).provenance_ref is None


# ── Acceptance freeze ───────────────────────────────────────────────────────


def _acceptance() -> AcceptanceContract:
    return AcceptanceContract(
        project_id="proj-a", node_id="n1", criteria=(_criterion(),)
    )


def test_an_acceptance_contract_starts_unfrozen() -> None:
    assert not _acceptance().is_frozen


def test_freezing_stamps_the_moment_and_returns_a_new_contract() -> None:
    contract = _acceptance()
    frozen = contract.freeze()
    assert frozen.is_frozen
    assert frozen.frozen_at is not None
    assert not contract.is_frozen, "freezing must not mutate the original"


def test_freezing_twice_is_idempotent() -> None:
    """A retried activity must not fault on an already-frozen contract."""
    frozen = _acceptance().freeze()
    assert frozen.freeze() is frozen


def test_an_acceptance_contract_needs_at_least_one_criterion() -> None:
    with pytest.raises(ValidationError):
        AcceptanceContract(project_id="proj-a", node_id="n1", criteria=())


def test_criteria_are_addressable_by_id() -> None:
    criterion = _criterion()
    contract = AcceptanceContract(
        project_id="proj-a", node_id="n1", criteria=(criterion,)
    )
    assert contract.criteria_by_id() == {criterion.criterion_id: criterion}


# ── Execution contract ──────────────────────────────────────────────────────


def _execution() -> ExecutionContract:
    return ExecutionContract(
        project_id="proj-a",
        node_id="n1",
        objective="Run the docking simulation.",
        allowed_actions=("submit", "collect_outputs"),
        allowed_retries=1,
    )


def test_a_worker_may_only_do_what_the_contract_names() -> None:
    contract = _execution()
    assert contract.permits("submit")
    assert not contract.permits("change_parameters")
    assert not contract.permits("run_shell_command")


def test_an_empty_allow_list_permits_nothing() -> None:
    """Which is how a node that only waits is authored."""
    contract = ExecutionContract(project_id="proj-a", node_id="n1", objective="Wait.")
    assert not contract.permits("submit")
    assert not contract.retries_permitted()


def test_retries_are_off_unless_the_contract_allows_them() -> None:
    assert _execution().retries_permitted()
    assert not ExecutionContract(
        project_id="proj-a", node_id="n1", objective="x", allowed_retries=0
    ).retries_permitted()


def test_a_negative_retry_budget_is_refused() -> None:
    with pytest.raises(ValidationError):
        ExecutionContract(project_id="proj-a", node_id="n1", objective="x", allowed_retries=-1)


def test_freezing_an_execution_contract_is_idempotent() -> None:
    frozen = _execution().freeze()
    assert frozen.is_frozen and frozen.freeze() is frozen


# ── The environment a contract requires ─────────────────────────────────────


def test_a_contract_that_names_no_environment_requires_no_preparation() -> None:
    """The compatibility rule, as a property rather than as a promise.

    Every contract written before the field existed names no environment, and
    a node whose contract names none is run the way it always was: nothing is
    materialized, and the Worker executes under the terms it has. If this
    ever stopped being true, every existing node in every project would be
    waiting on a workspace.
    """
    assert _execution().execution_requirements == {}
    assert not _execution().requires_preparation
    assert _execution().requires_preparation is False


@pytest.mark.parametrize(
    ("kind", "name"),
    [("software", "raspa"), ("lab", "bench-chemistry")],
)
def test_a_contract_may_require_either_kind_of_environment(kind: str, name: str) -> None:
    contract = ExecutionContract(
        project_id="proj-a",
        node_id="n1",
        objective="Run the simulation.",
        execution_requirements={kind: name},
    )
    assert contract.requires_preparation
    assert contract.execution_requirements == {kind: name}


def test_a_requirement_of_an_unknown_kind_is_refused_where_it_is_written() -> None:
    """A typo must not become a run started in a workspace nothing prepared.

    The kind is a closed list, so `softwre` is refused at the moment the plan
    is written rather than discovered by a Worker that is handed a node whose
    environment nobody built. This is the same rule `allowed_ranges` and
    `allowed_substitutions` are checked by, and for the same reason.
    """
    with pytest.raises(ValidationError) as refused:
        ExecutionContract(
            project_id="proj-a",
            node_id="n1",
            objective="Run the simulation.",
            execution_requirements={"softwre": "raspa"},
        )
    assert "softwre" in str(refused.value)
    assert "software" in str(refused.value), (
        "the refusal does not name the kinds that would have been accepted, so "
        "the model reading it cannot correct itself"
    )


def test_a_requirement_that_names_nothing_is_refused() -> None:
    """`{"software": ""}` reads as a requirement and names nothing to prepare."""
    with pytest.raises(ValidationError) as refused:
        ExecutionContract(
            project_id="proj-a",
            node_id="n1",
            objective="Run the simulation.",
            execution_requirements={"software": "   "},
        )
    assert "names nothing" in str(refused.value)


def test_a_requirement_may_name_an_environment_ravel_has_no_materializer_for() -> None:
    """Which package RAVEL can build for is a deployment fact, not a domain one.

    `vasp` is a real request a Master may make. Whether this deployment can
    serve it is answered by the preparation layer — with a refusal Master
    decides on — rather than by the contract's schema, which has no way to
    know what is installed.
    """
    contract = ExecutionContract(
        project_id="proj-a",
        node_id="n1",
        objective="Relax the cell.",
        execution_requirements={"software": "vasp"},
    )
    assert contract.requires_preparation


# ── Authority envelope ──────────────────────────────────────────────────────


def test_an_envelope_decides_which_actions_need_a_human() -> None:
    envelope = AuthorityEnvelope(
        project_id="proj-a",
        requires_approval=(
            ApprovalRequirement(
                action="EXCEED_BUDGET", required_role=UserRole.PROJECT_OWNER
            ),
        ),
    )
    assert envelope.requires_human_approval("EXCEED_BUDGET")
    assert not envelope.requires_human_approval("CREATE_NODE")
    assert envelope.requirement_for("EXCEED_BUDGET") is not None
    assert envelope.requirement_for("CREATE_NODE") is None


def test_an_empty_envelope_requires_nothing() -> None:
    envelope = AuthorityEnvelope(project_id="proj-a")
    assert not envelope.requires_human_approval("TERMINATE_PROJECT")


# ── Research and success contracts ──────────────────────────────────────────


def test_a_research_contract_requires_a_goal_and_a_problem() -> None:
    with pytest.raises(ValidationError):
        ResearchContract(project_id="proj-a", original_user_goal="", scientific_problem="x")
    with pytest.raises(ValidationError):
        ResearchContract(project_id="proj-a", original_user_goal="x", scientific_problem="")


def test_a_research_contract_records_prohibitions() -> None:
    contract = ResearchContract(
        project_id="proj-a",
        original_user_goal="Find a better catalyst.",
        scientific_problem="Which dopant raises activity?",
        prohibited_actions=("no animal testing",),
    )
    assert contract.prohibited_actions == ("no animal testing",)


def test_a_success_contract_needs_success_criteria() -> None:
    with pytest.raises(ValidationError):
        ProjectSuccessContract(project_id="proj-a", success_criteria=())


def test_a_success_contract_version_two_names_what_it_replaces() -> None:
    """A change to success is versioned and requires a decision, never an edit."""
    revised = ProjectSuccessContract(
        project_id="proj-a",
        version=2,
        success_criteria=("Ki below 5 nM",),
        supersedes="ctr-1",
        decision_ref="dec-1",
    )
    assert revised.version == 2 and revised.supersedes == "ctr-1"
