"""T10-AC2: the input package is deterministic and its standards are fixed early.

The two halves of the acceptance condition are structural, not procedural:

- the same frozen contract, built twice in two directories, produces the same
  package digest and the same manifest bytes (nothing host-dependent is inside);
- a version change, or one changed scientific term, produces a different one.

The manifest also has to carry the things a result will later be judged by —
the exact required output, the parser schema by digest, the convergence policy,
the Review criteria and their digest — and none of them may be absent, added by
the parser later, or stamped with a time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from tests.premium.T10.support import (
    TERMS,
    an_execution_contract,
    assert_valid,
    build_package,
    expected_output,
    schema_document,
    schema_sha256,
)

from ravel.compute.raspa import package as package_module
from ravel.compute.raspa.package import (
    CONVERGENCE_POLICY,
    MATERIALIZER,
    MATERIALIZER_VERSION,
    PACKAGE_SCHEMA,
    PARSER_SCHEMA_ID,
    PARSER_SCHEMA_SHA256,
    canonical_json,
    verify_input_package,
)
from ravel.compute.raspa.provenance import read_bundled, source_record
from ravel.compute.raspa.route import (
    REAL_RUN_STATUS,
    REAL_RUN_TASK,
    ROUTE_ID,
    SUPPORT_MATRIX,
    SUPPORTED_METHOD,
    SUPPORTED_VERSION_SERIES,
)
from ravel.domain.preparation import PreparationRefusal
from ravel.preparation.workspace import MaterializationRefused


def test_the_same_contract_produces_the_same_package_digest(tmp_path: Path) -> None:
    first = build_package(tmp_path / "first")
    second = build_package(tmp_path / "second")
    assert first.package_sha256 == second.package_sha256
    assert first.manifest == second.manifest
    assert (
        (first.root / "calculation_manifest.json").read_bytes()
        == (second.root / "calculation_manifest.json").read_bytes()
    )
    assert (first.root / "simulation.input").read_bytes() == (
        second.root / "simulation.input"
    ).read_bytes()


def test_a_contract_version_change_produces_a_new_package(tmp_path: Path) -> None:
    one = build_package(tmp_path / "v1")
    version_two = an_execution_contract(version=2)
    two = build_package(tmp_path / "v2", contract=version_two)
    assert one.package_sha256 != two.package_sha256
    assert one.manifest["execution_contract"]["contract_version"] == 1
    assert two.manifest["execution_contract"]["contract_version"] == 2


def test_one_changed_scientific_value_produces_a_new_package(tmp_path: Path) -> None:
    """There is no 'close enough': the state point is part of the identity."""
    baseline = build_package(tmp_path / "baseline")
    terms = dict(TERMS)
    terms["temperature"] = "301"
    warmer = build_package(tmp_path / "warmer", terms=terms)
    assert baseline.package_sha256 != warmer.package_sha256


def test_the_manifest_has_no_time_or_host_path(tmp_path: Path) -> None:
    """A timestamp in the manifest would make two builds of one contract differ."""
    package = build_package(tmp_path / "package")
    document = canonical_json(package.manifest)
    assert "generated_at" not in package.manifest
    assert str(tmp_path) not in document
    assert "/tmp/" not in document


def test_the_package_digest_is_recomputable_from_the_manifest(tmp_path: Path) -> None:
    package = build_package(tmp_path / "package")
    without = {k: v for k, v in package.manifest.items() if k != "package_sha256"}
    recomputed = hashlib.sha256(canonical_json(without).encode("utf-8")).hexdigest()
    assert package.package_sha256 == recomputed


def test_the_manifest_pins_the_standards_before_a_run(tmp_path: Path) -> None:
    package = build_package(tmp_path / "package")
    manifest = package.manifest
    assert manifest["schema"] == PACKAGE_SCHEMA
    assert manifest["materializer"] == MATERIALIZER
    assert manifest["materializer_version"] == MATERIALIZER_VERSION
    assert manifest["method"] == SUPPORTED_METHOD
    assert manifest["software"]["version_series"] == SUPPORTED_VERSION_SERIES
    assert manifest["required_outputs"] == [expected_output()]
    assert manifest["output_filename"] == expected_output()
    assert manifest["parser"] == {
        "schema_id": PARSER_SCHEMA_ID,
        "schema_version": "1",
        "schema_sha256": PARSER_SCHEMA_SHA256,
        "result_statuses": ["NORMAL", "NON_CONVERGED", "SOFTWARE_FAILURE"],
        "evidence_classes": ["COMPUTATION", "SIMULATED"],
    }
    assert manifest["convergence"] == CONVERGENCE_POLICY
    assert manifest["convergence"]["fixed_before_run"] is True
    assert manifest["acceptance"]["criteria"]
    assert len(manifest["acceptance"]["criteria_sha256"]) == 64


def test_the_parser_schema_digest_in_the_package_matches_the_schema_file() -> None:
    """A pinned digest that silently followed a checkout would not be pinned."""
    assert schema_sha256("raspa_result.schema.json") == PARSER_SCHEMA_SHA256


def test_the_manifest_validates_against_the_package_schema(tmp_path: Path) -> None:
    package = build_package(tmp_path / "package")
    assert_valid(package.manifest, "raspa_input_package.schema.json")


def test_the_scientific_contract_validates_against_its_schema(tmp_path: Path) -> None:
    package = build_package(tmp_path / "package")
    assert_valid(
        package.manifest["scientific_contract"], "raspa_scientific_contract.schema.json"
    )


def test_the_manifest_records_the_route_and_the_external_real_run(tmp_path: Path) -> None:
    manifest = build_package(tmp_path / "package").manifest
    assert manifest["route"]["route_id"] == ROUTE_ID
    assert manifest["route"]["method"] == SUPPORTED_METHOD
    assert manifest["route"]["version_series"] == SUPPORTED_VERSION_SERIES
    assert manifest["route"]["real_run_task"] == REAL_RUN_TASK
    assert manifest["route"]["real_run_status"] == REAL_RUN_STATUS
    assert manifest["real_run"]["task"] == "T25"
    assert manifest["real_run"]["status"] == "BLOCKED_EXTERNAL"
    assert manifest["real_run"]["blocker"] == SUPPORT_MATRIX.real_run_blocker
    assert manifest["evidence_scope"] == "IMPLEMENTATION_ONLY"


def test_the_manifest_records_every_input_by_pinned_official_digest(tmp_path: Path) -> None:
    package = build_package(tmp_path / "package")
    names = [entry["name"] for entry in package.manifest["inputs"]]
    assert names == [
        "MFI_SI.cif",
        "force_field_mixing_rules.def",
        "pseudo_atoms.def",
        "methane.def",
    ]
    for entry in package.manifest["inputs"]:
        assert entry["license"] == "MIT"
    assert package.manifest["inputs"][0]["sha256"] == source_record(
        "framework-mfi-si"
    ).sha256
    assert (package.root / "MFI_SI.cif").read_bytes() == read_bundled("framework-mfi-si")


def test_the_manifest_names_the_licenses_of_every_source(tmp_path: Path) -> None:
    manifest = build_package(tmp_path / "package").manifest
    sources = {entry["key"]: entry for entry in manifest["sources"]}
    assert set(sources) == {
        "raspa2-license",
        "official-simulation-input",
        "framework-mfi-si",
        "force-field-example-zeolites",
        "pseudo-atoms-example-zeolites",
        "molecule-methane",
    }
    assert all(entry["license"] == "MIT" for entry in sources.values())
    assert all(entry["commit"] for entry in sources.values())
    assert sources["framework-mfi-si"]["sha256"] == source_record("framework-mfi-si").sha256


def test_the_generated_files_are_every_file_the_package_wrote(tmp_path: Path) -> None:
    package = build_package(tmp_path / "package")
    listed = {entry["name"] for entry in package.manifest["generated_files"]}
    on_disk = {path.name for path in package.root.iterdir()} - {"calculation_manifest.json"}
    assert listed == on_disk
    assert listed == {
        "MFI_SI.cif",
        "force_field_mixing_rules.def",
        "pseudo_atoms.def",
        "methane.def",
        "simulation.input",
    }


def test_verification_accepts_an_untouched_package(tmp_path: Path) -> None:
    built = build_package(tmp_path / "package")
    verified = verify_input_package(built.root, strict=True)
    assert verified.package_sha256 == built.package_sha256
    assert verified.files == built.files


def test_verification_refuses_edited_input_bytes(tmp_path: Path) -> None:
    built = build_package(tmp_path / "package")
    input_path = built.root / "MFI_SI.cif"
    input_path.write_bytes(input_path.read_bytes() + b"\n# edited\n")
    with pytest.raises(MaterializationRefused) as caught:
        verify_input_package(built.root)
    assert caught.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT


def test_verification_refuses_an_edited_manifest(tmp_path: Path) -> None:
    built = build_package(tmp_path / "package")
    manifest_path = built.root / "calculation_manifest.json"
    document = json.loads(manifest_path.read_text(encoding="utf-8"))
    document["objective"] = "A different objective added after the build"
    manifest_path.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(MaterializationRefused) as caught:
        verify_input_package(built.root)
    assert caught.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "edited" in caught.value.reason


def test_strict_verification_refuses_an_unlisted_extra_file(tmp_path: Path) -> None:
    built = build_package(tmp_path / "package")
    (built.root / "extra.txt").write_text("not part of the package", encoding="utf-8")
    with pytest.raises(MaterializationRefused) as caught:
        verify_input_package(built.root, strict=True)
    assert caught.value.refusal is PreparationRefusal.INCONSISTENT_CONTRACT
    assert "extra.txt" in caught.value.reason


def test_the_acceptance_criteria_digest_is_recomputable(tmp_path: Path) -> None:
    manifest = build_package(tmp_path / "package").manifest
    recomputed = hashlib.sha256(
        canonical_json(manifest["acceptance"]["criteria"]).encode("utf-8")
    ).hexdigest()
    assert manifest["acceptance"]["criteria_sha256"] == recomputed


def test_a_criteria_change_changes_the_package(tmp_path: Path) -> None:
    from tests.premium.T10.support import an_acceptance_terms

    first = build_package(tmp_path / "first")
    changed = an_acceptance_terms()
    changed = replace(
        changed,
        criteria=tuple(
            criterion.model_copy(update={"threshold": "<= 0.10"})
            if criterion.criterion_id == "crit-convergence"
            else criterion
            for criterion in changed.criteria
        ),
    )
    second = build_package(tmp_path / "second", acceptance=changed)
    assert first.package_sha256 != second.package_sha256


def test_execution_metadata_does_not_claim_a_run(tmp_path: Path) -> None:
    package = build_package(tmp_path / "package")
    execution = package.execution()
    assert execution.materializer == MATERIALIZER
    assert execution.execution_metadata["software"] == "raspa"
    assert execution.execution_metadata["output"] == expected_output()
    assert execution.execution_metadata["package_sha256"] == package.package_sha256
    assert "status" not in execution.execution_metadata


def test_the_manifest_schema_file_is_the_one_the_parser_schema_pin_names() -> None:
    """The package digest pins a result schema; the schema carries its own id."""
    result_schema = schema_document("raspa_result.schema.json")
    assert result_schema["$id"] == PARSER_SCHEMA_ID
    assert result_schema["properties"]["schema"]["const"] == package_module.PACKAGE_SCHEMA.replace(
        "input_package", "result"
    )
