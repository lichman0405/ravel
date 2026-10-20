"""T10-AC1: the route refuses what it cannot scientifically prepare.

Every case here is a contract the first supported route must not turn into a
package: a missing scientific term, a method that is not GCMC, a system whose
bytes the package does not hold, an unsupported unit, or a name that would
select a file outside the workspace. The refusal class matters as much as the
refusal: Master routes a missing parameter differently from an unsupported
environment.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tests.premium.T10.support import (
    INPUT_NAMES,
    TERMS,
    a_context,
    an_acceptance_terms,
    an_execution_contract,
    build_package,
    expected_output,
    official_inputs,
)

from ravel.compute.raspa.contract import REQUIRED_TERMS
from ravel.domain.preparation import PreparationRefusal
from ravel.preparation.workspace import AcceptanceTerms, MaterializationRefused

REFUSED_TERMS = (
    "method",
    "temperature",
    "temperature_unit",
    "pressure",
    "pressure_unit",
    "cycles",
    "initialization_cycles",
    "print_every",
    "unit_cells",
    "translation_probability",
    "reinsertion_probability",
    "swap_probability",
    "seed",
    "framework",
    "molecule",
    "molecule_definition",
    "force_field",
)


def refusal(root: Path, **options: object) -> MaterializationRefused:
    with pytest.raises(MaterializationRefused) as caught:
        build_package(root, **options)  # type: ignore[arg-type]
    return caught.value


def terms_without(term: str) -> dict[str, str]:
    terms = dict(TERMS)
    terms.pop(term)
    return terms


@pytest.mark.parametrize("term", REFUSED_TERMS)
def test_every_required_term_is_really_required(tmp_path: Path, term: str) -> None:
    """No term is defaulted from model memory or from RASPA's own defaults."""
    assert term in REQUIRED_TERMS
    refused = refusal(tmp_path / "package", terms=terms_without(term))
    assert refused.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert term in refused.reason


@pytest.mark.parametrize("term", ("temperature", "pressure"))
def test_a_blank_required_term_is_missing_not_zero(tmp_path: Path, term: str) -> None:
    terms = dict(TERMS)
    terms[term] = "   "
    refused = refusal(tmp_path / "package", terms=terms)
    assert refused.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert term in refused.reason


@pytest.mark.parametrize(
    "method", ("DFT", "conductivity", "MD", "NPT", "Gibbs", "CFCMC", "pore size distribution")
)
def test_other_calculation_types_are_not_this_route(tmp_path: Path, method: str) -> None:
    terms = dict(TERMS)
    terms["method"] = method
    refused = refusal(tmp_path / "package", terms=terms)
    assert refused.refusal is PreparationRefusal.UNSUPPORTED_ENVIRONMENT
    assert "GCMC" in refused.reason


def test_a_non_raspa_software_requirement_is_refused(tmp_path: Path) -> None:
    contract = an_execution_contract(requirements={"software": "lammps"})
    refused = refusal(tmp_path / "package", contract=contract)
    assert refused.refusal is PreparationRefusal.UNSUPPORTED_ENVIRONMENT
    assert "lammps" in refused.reason


@pytest.mark.parametrize(
    "term,value",
    (
        ("framework", "../MFI_SI.cif"),
        ("framework", "../../etc/passwd.cif"),
        ("framework", "structures/MFI_SI.cif"),
        ("molecule", "../methane"),
        ("molecule", "methane\nFramework 1"),
        ("force_field", "Example Zeolites"),
        ("molecule_definition", "/tmp/definitions"),
    ),
)
def test_a_supplied_name_can_never_be_a_path(tmp_path: Path, term: str, value: str) -> None:
    """A model-supplied name selects a definition, never a file location."""
    terms = dict(TERMS)
    terms[term] = value
    refused = refusal(tmp_path / "package", terms=terms)
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert term in refused.reason


@pytest.mark.parametrize(
    "term,value",
    (
        ("temperature_unit", "C"),
        ("temperature_unit", "fahrenheit"),
        ("pressure_unit", "atm"),
        ("pressure_unit", "torr"),
    ),
)
def test_an_unsupported_unit_is_not_silently_converted(
    tmp_path: Path, term: str, value: str
) -> None:
    terms = dict(TERMS)
    terms[term] = value
    refused = refusal(tmp_path / "package", terms=terms)
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert term in refused.reason


def test_bar_is_converted_to_the_canonical_pascal_state_point(tmp_path: Path) -> None:
    terms = dict(TERMS)
    terms["pressure"] = "1"
    terms["pressure_unit"] = "bar"
    package = build_package(tmp_path / "package", terms=terms)
    pressure = package.manifest["scientific_contract"]["pressure"]
    assert pressure["value"] == pytest.approx(100000.0)
    assert pressure["source_value"] == pytest.approx(1.0)
    assert pressure["source_unit"] == "bar"


@pytest.mark.parametrize(
    "term,value",
    (
        ("swap_probability", "0"),
        ("translation_probability", "1.5"),
        ("unit_cells", "2 2"),
        ("unit_cells", "2 0 2"),
        ("cycles", "1.5"),
        ("cycles", "0"),
        ("seed", "0"),
        ("seed", "18446744073709551616"),
        ("print_every", "30000"),
    ),
)
def test_an_unusable_sampling_value_is_refused(tmp_path: Path, term: str, value: str) -> None:
    terms = dict(TERMS)
    terms[term] = value
    refused = refusal(tmp_path / "package", terms=terms)
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT


@pytest.mark.parametrize(
    "term,value",
    (
        ("framework", "MFI.cif"),
        ("molecule", "CO2"),
        ("molecule_definition", "OtherDefinitions"),
        ("force_field", "OtherForceField"),
    ),
)
def test_a_system_without_pinned_provenance_is_refused(
    tmp_path: Path, term: str, value: str
) -> None:
    """Substituting bytes under a false manifest is the failure this prevents."""
    terms = dict(TERMS)
    terms[term] = value
    refused = refusal(tmp_path / "package", terms=terms)
    assert refused.refusal is PreparationRefusal.UNSUPPORTED_ENVIRONMENT
    assert value in refused.reason


def test_a_tampered_input_is_refused_by_content(tmp_path: Path) -> None:
    supplied = list(official_inputs())
    supplied[0] = replace(supplied[0], data=supplied[0].data + b"\n# edited\n")
    refused = refusal(tmp_path / "package", inputs=tuple(supplied))
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "pinned official" in refused.reason


def test_an_input_the_contract_never_named_is_refused(tmp_path: Path) -> None:
    contract = an_execution_contract(inputs=INPUT_NAMES[1:])
    refused = refusal(tmp_path / "package", contract=contract)
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "MFI_SI.cif" in refused.reason


def test_a_named_input_that_was_not_resolved_is_refused(tmp_path: Path) -> None:
    supplied = tuple(entry for entry in official_inputs() if entry.name != "methane.def")
    refused = refusal(tmp_path / "package", inputs=supplied)
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "methane.def" in refused.reason


def test_a_package_without_frozen_review_criteria_is_refused(tmp_path: Path) -> None:
    acceptance = AcceptanceTerms(contract_id="acceptance-empty", version=1, criteria=())
    refused = refusal(tmp_path / "package", acceptance=acceptance)
    assert refused.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert "acceptance" in refused.reason


def test_required_outputs_must_be_the_exact_raspa_output(tmp_path: Path) -> None:
    contract = an_execution_contract(required_outputs=("loading.csv",))
    refused = refusal(tmp_path / "package", contract=contract)
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "loading.csv" in refused.reason


def test_an_extra_required_output_is_refused(tmp_path: Path) -> None:
    output = expected_output()
    contract = an_execution_contract(required_outputs=(output, "notes.json"))
    refused = refusal(tmp_path / "package", contract=contract)
    assert refused.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "notes.json" in refused.reason


def test_a_non_empty_workspace_is_never_overwritten(tmp_path: Path) -> None:
    workspace = tmp_path / "package"
    workspace.mkdir()
    (workspace / "previous-run.txt").write_text("something", encoding="utf-8")
    refused = refusal(workspace)
    assert refused.refusal is PreparationRefusal.ENVIRONMENT_UNAVAILABLE
    assert "not empty" in refused.reason


def test_a_valid_contract_does_build(tmp_path: Path) -> None:
    """The control: every refusal above is not the route refusing everything."""
    package = build_package(tmp_path / "package")
    assert package.required_outputs == (expected_output(),)
    assert len(package.manifest["inputs"]) == 4


def test_the_package_is_built_with_the_caller_s_acceptance_contract_not_a_default(
    tmp_path: Path,
) -> None:
    acceptance = an_acceptance_terms(contract_id="acceptance-custom", version=3)
    package = build_package(tmp_path / "package", acceptance=acceptance)
    recorded = package.manifest["acceptance"]
    assert recorded["contract_ref"] == "acceptance-custom"
    assert recorded["contract_version"] == 3


def test_context_requires_the_workspace_to_be_the_one_it_writes(tmp_path: Path) -> None:
    """A materializer writes only the root it was given, never a sibling."""
    root = tmp_path / "chosen"
    context = a_context(root)
    assert context.workspace_root == root
    package = build_package(root)
    assert package.root == root
