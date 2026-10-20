"""T10-AC2: parsing distinguishes software failure, non-convergence, and a result.

The reference output is the official RASPA file committed by the software's
authors. It is a fixture: the tests label it `REFERENCE_FIXTURE` and the parser
marks it `SIMULATED`. The synthetic files are `TEST_FIXTURE`, used only for
branches the official output does not exercise. No test in this module claims a
RAVEL execution.
"""

from __future__ import annotations

import hashlib

import pytest
from tests.premium.T10.support import (
    a_scientific_contract,
    assert_valid,
    build_package,
    reference_output_bytes,
    reference_result,
    schema_document,
    synthetic_output,
)

from ravel.compute.raspa.contract import RaspaScientificContract
from ravel.compute.raspa.parser import (
    EvidenceClass,
    ResultStatus,
    parse_raspa_output,
)
from ravel.compute.raspa.provenance import SourceKind


@pytest.fixture(scope="module")
def package_bytes(tmp_path_factory: pytest.TempPathFactory) -> str:
    root = tmp_path_factory.mktemp("package")
    return build_package(root).package_sha256


@pytest.fixture(scope="module")
def contract() -> RaspaScientificContract:
    return a_scientific_contract()


def parse(output: bytes, contract: RaspaScientificContract, package: str):
    return parse_raspa_output(
        output,
        exit_code=0,
        contract=contract,
        package_sha256=package,
        provenance=SourceKind.TEST_FIXTURE,
    )


def test_the_official_reference_output_parses_to_a_normal_result(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = reference_result(package_bytes, contract=contract)
    assert result.status is ResultStatus.NORMAL
    assert result.source_kind is SourceKind.REFERENCE_FIXTURE
    assert result.evidence_class is EvidenceClass.SIMULATED
    assert result.software_version == "2.0.45"
    assert result.software_series == "2.0"
    assert result.temperature_k == 300.0
    assert result.pressure_pa == 100000.0
    assert result.seed == 1621335350
    assert result.cycles == 25000
    assert result.initialization_cycles == 5000
    assert result.warnings == 0
    assert result.molecules_per_unit_cell is not None
    assert result.molecules_per_unit_cell.mean == pytest.approx(2.8975450000)
    assert result.molecules_per_unit_cell.standard_error == pytest.approx(0.0326619316)
    assert result.mol_per_kg is not None
    assert result.mol_per_kg.mean == pytest.approx(0.5023401474)
    assert result.mg_per_g is not None
    assert result.mg_per_g.mean == pytest.approx(8.0587717205)
    assert result.conversion_mol_per_kg == pytest.approx(0.1733675050)
    assert result.conversion_mg_per_g == pytest.approx(2.7812412648)
    assert len(result.blocks) == 5
    assert result.blocks == pytest.approx((22.98140, 23.14100, 23.52020, 23.04180, 23.21740))
    assert result.relative_standard_error is not None
    assert result.relative_standard_error < 0.05


def test_the_reference_result_is_a_fixture_and_says_so(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = reference_result(package_bytes, contract=contract)
    document = result.to_document()
    assert document["source"]["kind"] == "REFERENCE_FIXTURE"
    assert document["source"]["evidence_class"] == "SIMULATED"
    assert "not a RAVEL execution" in document["source"]["evidence_reason"]
    assert document["status"] == "NORMAL"
    assert document["units"] == {
        "temperature": "K",
        "pressure": "Pa",
        "loading_primary": "mol/kg framework",
        "loading_secondary": "molecules/unit cell",
        "seed": "dimensionless",
    }


def test_the_result_document_validates_against_the_parser_schema(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = reference_result(package_bytes, contract=contract)
    assert_valid(result.to_document(), "raspa_result.schema.json")


def test_the_result_schema_pins_the_three_outcomes() -> None:
    schema = schema_document("raspa_result.schema.json")
    assert schema["properties"]["status"]["enum"] == [
        "NORMAL",
        "NON_CONVERGED",
        "SOFTWARE_FAILURE",
    ]
    assert schema["properties"]["source"]["properties"]["evidence_class"]["enum"] == [
        "COMPUTATION",
        "SIMULATED",
    ]


def test_a_synthetic_complete_run_is_normal(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output(), contract, package_bytes)
    assert result.status is ResultStatus.NORMAL
    assert result.evidence_class is EvidenceClass.SIMULATED
    assert result.blocks == pytest.approx((2.0, 3.0, 3.0, 4.0, 3.0))


def test_too_few_block_means_is_non_converged(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output(blocks=(2.0, 3.0, 4.0)), contract, package_bytes)
    assert result.status is ResultStatus.NON_CONVERGED
    assert "at least 5" in result.reason


def test_a_relative_standard_error_above_the_policy_is_non_converged(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(
        synthetic_output(mean=3.0, standard_error=0.5), contract, package_bytes
    )
    assert result.status is ResultStatus.NON_CONVERGED
    assert "relative standard error" in result.reason


def test_a_missing_completion_line_is_a_software_failure(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output(finished=False), contract, package_bytes)
    assert result.status is ResultStatus.SOFTWARE_FAILURE
    assert "Simulation finished" in result.reason


def test_reported_warnings_are_a_software_failure(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output(warnings=2), contract, package_bytes)
    assert result.status is ResultStatus.SOFTWARE_FAILURE
    assert "2 warnings" in result.reason


def test_a_state_point_that_differs_from_the_contract_is_a_software_failure(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output(temperature=310.0), contract, package_bytes)
    assert result.status is ResultStatus.SOFTWARE_FAILURE
    assert result.temperature_k == 310.0
    assert "state point" in result.reason


def test_a_seed_that_differs_from_the_contract_is_a_software_failure(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output(seed=7), contract, package_bytes)
    assert result.status is ResultStatus.SOFTWARE_FAILURE


def test_a_different_raspa_series_is_a_software_failure(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output(version="RASPA 2.1.0"), contract, package_bytes)
    assert result.status is ResultStatus.SOFTWARE_FAILURE
    assert result.software_series == "2.1"


def test_loading_that_disagrees_with_the_printed_conversions_is_a_failure(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(
        synthetic_output(printed_mol_kg=9.0), contract, package_bytes
    )
    assert result.status is ResultStatus.SOFTWARE_FAILURE
    assert "unit conversion" in result.reason


def test_a_non_zero_exit_is_a_software_failure_even_when_the_text_looks_normal(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse_raspa_output(
        synthetic_output(),
        exit_code=137,
        contract=contract,
        package_sha256=package_bytes,
        provenance=SourceKind.TEST_FIXTURE,
    )
    assert result.status is ResultStatus.SOFTWARE_FAILURE
    assert "exited 137" in result.reason


def test_non_utf8_bytes_are_a_software_failure(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = parse(synthetic_output() + b"\xff\xfe", contract, package_bytes)
    assert result.status is ResultStatus.SOFTWARE_FAILURE
    assert "UTF-8" in result.reason


def test_parsing_requires_an_explicit_source_kind(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    """A string that spells a source kind is not the claim the kind makes."""
    with pytest.raises(TypeError):
        parse_raspa_output(
            synthetic_output(),
            exit_code=0,
            contract=contract,
            package_sha256=package_bytes,
            provenance="REFERENCE_FIXTURE",  # type: ignore[arg-type]
        )


def test_the_result_digest_covers_the_canonical_document(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = reference_result(package_bytes, contract=contract)
    assert result.sha256() == hashlib.sha256(result.canonical_json().encode()).hexdigest()
    assert result.output_sha256 == hashlib.sha256(reference_output_bytes()).hexdigest()
    assert result.output_size_bytes == len(reference_output_bytes())


def test_the_parser_records_every_check_it_made(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    result = reference_result(package_bytes, contract=contract)
    names = {check.name for check in result.checks}
    assert names == {
        "output_is_text",
        "software_version_supported",
        "state_point_matches_contract",
        "run_completed_without_warnings",
        "loading_statistics_parsed",
        "block_means_numbered",
        "unit_conversions_consistent",
        "convergence_policy_met",
    }
    assert all(check.passed for check in result.checks)


def test_a_fixture_can_never_be_parsed_as_a_computation(
    package_bytes: str, contract: RaspaScientificContract
) -> None:
    """Both fixture kinds are SIMULATED; neither is a RAVEL run."""
    for kind in (SourceKind.REFERENCE_FIXTURE, SourceKind.TEST_FIXTURE):
        result = reference_result(package_bytes, contract=contract, provenance=kind)
        assert result.evidence_class is EvidenceClass.SIMULATED
    assert len(SourceKind) == 3
