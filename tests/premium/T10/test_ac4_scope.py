"""T10-AC4: what this task claims, and what it explicitly does not.

The real RASPA run belongs to T25 and is external. T10 proves deterministic
implementation behaviour: a package can be built without executing anything, the
official output parses only as a fixture, and the route record says
`BLOCKED_EXTERNAL` rather than implying an execution that did not happen.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from tests.premium.T10.support import (
    a_scientific_contract,
    build_package,
    reference_result,
    synthetic_output,
)

from ravel.compute.raspa.package import MATERIALIZER
from ravel.compute.raspa.parser import EvidenceClass, ResultStatus
from ravel.compute.raspa.provenance import SourceKind
from ravel.compute.raspa.route import (
    REAL_RUN_BLOCKER,
    REAL_RUN_STATUS,
    REAL_RUN_TASK,
    REAL_RUN_UNLOCK,
    ROUTE_ID,
    SUPPORT_MATRIX,
    SUPPORTED_METHOD,
    SUPPORTED_VERSION_SERIES,
    UNSUPPORTED_METHODS,
)


def test_the_support_matrix_is_one_route_and_names_its_boundary() -> None:
    assert SUPPORT_MATRIX.route_id == ROUTE_ID
    assert ROUTE_ID == "raspa/gcmc-gas-adsorption"
    assert SUPPORT_MATRIX.software == "RASPA"
    assert SUPPORT_MATRIX.method == SUPPORTED_METHOD == "GCMC"
    assert SUPPORT_MATRIX.version_series == SUPPORTED_VERSION_SERIES == "2.0"
    assert SUPPORT_MATRIX.real_run_task == REAL_RUN_TASK == "T25"
    assert SUPPORT_MATRIX.real_run_status == REAL_RUN_STATUS == "BLOCKED_EXTERNAL"
    assert SUPPORT_MATRIX.real_run_blocker == REAL_RUN_BLOCKER
    assert SUPPORT_MATRIX.real_run_unlock == REAL_RUN_UNLOCK
    assert "no real RASPA execution" in SUPPORT_MATRIX.evidence_scope


def test_the_unsupported_methods_are_named_not_implied() -> None:
    assert "DFT" in UNSUPPORTED_METHODS
    assert "CONDUCTIVITY" in UNSUPPORTED_METHODS
    assert "MD" in UNSUPPORTED_METHODS
    assert "GIBBS" in UNSUPPORTED_METHODS


def test_building_a_package_executes_no_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The package is files and hashes. Starting software is T25's job."""

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("T10 must not execute a RASPA process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    package = build_package(tmp_path / "package")
    assert package.manifest["real_run"]["status"] == "BLOCKED_EXTERNAL"
    assert package.execution().execution_metadata["software"] == "raspa"
    assert package.execution().materializer == MATERIALIZER


def test_the_manifest_states_the_external_blocker_in_every_package(tmp_path: Path) -> None:
    manifest = build_package(tmp_path / "package").manifest
    assert manifest["evidence_scope"] == "IMPLEMENTATION_ONLY"
    assert manifest["real_run"] == {
        "task": "T25",
        "status": "BLOCKED_EXTERNAL",
        "blocker": REAL_RUN_BLOCKER,
        "unlock": REAL_RUN_UNLOCK,
    }
    assert manifest["route"]["real_run_status"] == "BLOCKED_EXTERNAL"


def test_the_reference_output_is_evidence_only_that_the_parser_works(
    tmp_path: Path,
) -> None:
    package = build_package(tmp_path / "package")
    result = reference_result(
        package.package_sha256,
        contract=a_scientific_contract(),
    )
    assert result.status is ResultStatus.NORMAL
    assert result.source_kind is SourceKind.REFERENCE_FIXTURE
    assert result.evidence_class is EvidenceClass.SIMULATED
    assert "not a RAVEL execution" in result.to_document()["source"]["evidence_reason"]


def test_a_synthetic_output_is_a_test_fixture_not_a_result() -> None:
    result = reference_result(
        "0" * 64,
        contract=a_scientific_contract(),
        provenance=SourceKind.TEST_FIXTURE,
        output=synthetic_output(),
    )
    assert result.evidence_class is EvidenceClass.SIMULATED
    assert result.source_kind is SourceKind.TEST_FIXTURE


def test_the_source_kind_vocabulary_keeps_fixtures_apart_from_runs() -> None:
    assert {member.value for member in SourceKind} == {
        "REAL_RUN",
        "REFERENCE_FIXTURE",
        "TEST_FIXTURE",
    }
