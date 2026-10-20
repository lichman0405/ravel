"""T10-AC3: a parsed result traces back to PostgreSQL, or is refused by item.

The trace is the check that the result document, the package, and the rows in
the Ledger are one account rather than three. The positive case builds the
package from a *real* frozen execution contract and a *real* frozen acceptance
contract bound to a node, then registers real artifact versions, evidence
sources, and an execution. The failures each remove or change one of those rows
and name the item that no longer supports the claim.

The artifact bytes live in a `MemoryStore` here; every row the trace reads is a
real PostgreSQL row written through the production repositories.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tests.integration.conftest import Prepared
from tests.premium.T10.support import (
    INPUT_NAMES,
    TERMS,
    a_traced_fixture,
    an_execution_contract,
    build_package,
    expected_output,
    reference_result,
    synthetic_output,
)

from ravel.compute.raspa.contract import RaspaScientificContract
from ravel.compute.raspa.ledger import (
    RaspaTraceRefused,
    TraceRefusal,
    trace_result,
)
from ravel.compute.raspa.package import RaspaInputPackage
from ravel.preparation.workspace import AcceptanceTerms
from ravel.state.repositories.records import ExecutionRepository
from ravel.state.repositories.research import ArtifactRepository


def prepared_for_raspa(prepare) -> Prepared:
    return prepare(
        parameter_targets=TERMS,
        inputs=INPUT_NAMES,
        required_outputs=(expected_output(),),
        execution_requirements={"software": "raspa"},
    )


def acceptance_of(prepared: Prepared) -> AcceptanceTerms:
    acceptance = prepared.acceptance
    assert acceptance is not None
    return AcceptanceTerms(
        contract_id=acceptance.contract_id,
        version=acceptance.version,
        criteria=acceptance.criteria,
    )


def package_for(
    tmp_path: Path,
    prepared: Prepared,
    *,
    acceptance: AcceptanceTerms | None = None,
) -> RaspaInputPackage:
    return build_package(
        tmp_path / "package",
        contract=prepared.contract,
        acceptance=acceptance_of(prepared) if acceptance is None else acceptance,
    )


def trace(database, prepared, package, fixture, result=None):
    with database.transaction() as session:
        return trace_result(
            session,
            project_id=prepared.project_id,
            node_id=prepared.node_id,
            manifest=package.manifest,
            result=fixture.result if result is None else result,
            artifact_id=fixture.artifact.artifact_id,
            artifact_version=fixture.version.version,
        )


def refusal(database, prepared, package, fixture, result=None) -> RaspaTraceRefused:
    with pytest.raises(RaspaTraceRefused) as caught:
        trace(database, prepared, package, fixture, result)
    return caught.value


def test_a_complete_ledger_traces_end_to_end(database, prepare, tmp_path: Path) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package)

    result = trace(database, prepared, package, fixture)
    kinds = [item.kind for item in result.items]
    assert kinds == [
        "EXECUTION_CONTRACT",
        "EXECUTION",
        "ACCEPTANCE_CONTRACT",
        "INPUT",
        "INPUT",
        "INPUT",
        "INPUT",
        "SOURCE",
        "SOURCE",
        "SOURCE",
        "SOURCE",
        "SOURCE",
        "SOURCE",
        "UNITS",
        "RESULT",
    ]
    assert result.package_sha256 == package.package_sha256
    assert result.result_sha256 == fixture.result.sha256()
    assert result.result_status == "NORMAL"
    assert result.to_document()["schema"] == "ravel.raspa.ledger_trace/1"


def test_the_package_is_built_from_the_frozen_rows_the_node_runs_under(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    manifest = package.manifest
    assert prepared.acceptance is not None
    assert manifest["execution_contract"] == {
        "contract_ref": prepared.contract.contract_id,
        "contract_version": prepared.contract.version,
    }
    assert manifest["acceptance"]["contract_ref"] == prepared.acceptance.contract_id
    assert manifest["acceptance"]["contract_version"] == prepared.acceptance.version
    assert manifest["acceptance"]["criteria_sha256"] == package.manifest["acceptance"][
        "criteria_sha256"
    ]


def test_tracing_writes_nothing(database, prepare, tmp_path: Path) -> None:
    """A trace is a reader; it cannot make its own claims true."""
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package)
    before = trace(database, prepared, package, fixture).to_document()

    with database.transaction() as session:
        executions = len(
            ExecutionRepository(session, prepared.project_id).for_node(prepared.node_id)
        )
        artifacts = len(ArtifactRepository(session, prepared.project_id).all())
    after = trace(database, prepared, package, fixture).to_document()
    assert before == after

    with database.transaction() as session:
        assert (
            len(ExecutionRepository(session, prepared.project_id).for_node(prepared.node_id))
            == executions
        )
        assert len(ArtifactRepository(session, prepared.project_id).all()) == artifacts


def test_a_fixture_registered_as_a_real_result_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package, result_kind="computation")
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.FIXTURE_PRESENTED_AS_REAL
    assert "fixture registered as a real result" in refused.reason
    assert "computation" in refused.reason


def test_an_input_without_an_artifact_row_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package, record_all_inputs=False)
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.INPUT_UNTRACED
    assert "MFI_SI.cif" in refused.reason


def test_a_source_without_a_readable_row_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package, record_all_sources=False)
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.SOURCE_UNTRACED
    assert "COPYING" in refused.reason


def test_a_result_without_an_execution_row_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package, record_execution_record=False)
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.EXECUTION_NOT_RECORDED
    assert prepared.contract.contract_id in refused.reason


def test_an_execution_that_does_not_name_the_result_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(
        database, prepared, package, output_refs=("some-other-artifact",)
    )
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.RESULT_UNTRACED
    assert "does not name artifact" in refused.reason


def test_claimed_result_bytes_that_differ_from_the_stored_bytes_are_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package)
    scientific = RaspaScientificContract.from_execution_contract(prepared.contract)
    different = reference_result(
        package.package_sha256,
        contract=scientific,
        output=synthetic_output(blocks=(9.0, 9.1, 8.9, 9.2, 9.0)),
    )
    refused = refusal(database, prepared, package, fixture, different)
    assert refused.code is TraceRefusal.RESULT_HASH_MISMATCH


def test_criteria_written_to_fit_the_result_are_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    original = acceptance_of(prepared)
    changed = replace(
        original,
        criteria=tuple(
            criterion.model_copy(update={"threshold": "<= 0.50"})
            if criterion.criterion_id == original.criteria[0].criterion_id
            else criterion
            for criterion in original.criteria
        ),
    )
    package = package_for(tmp_path, prepared, acceptance=changed)
    fixture = a_traced_fixture(database, prepared, package)
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.CRITERIA_RETROFITTED
    assert "criteria" in refused.reason


def test_unfrozen_review_criteria_cannot_define_a_result(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepare(
        parameter_targets=TERMS,
        inputs=INPUT_NAMES,
        required_outputs=(expected_output(),),
        execution_requirements={"software": "raspa"},
        freeze_acceptance=False,
        cleared=False,
    )
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package)
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.CRITERIA_NOT_FROZEN


def test_a_manifest_from_other_scientific_terms_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    terms = dict(TERMS)
    terms["temperature"] = "301"
    other_contract = an_execution_contract(
        terms,
        contract_id=prepared.contract.contract_id,
        version=prepared.contract.version,
        required_outputs=(expected_output(terms),),
    )
    package = build_package(
        tmp_path / "package",
        contract=other_contract,
        acceptance=acceptance_of(prepared),
    )
    fixture = a_traced_fixture(database, prepared, package)
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.CONTRACT_DOCUMENT_MISMATCH


def test_a_contract_not_in_the_project_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    missing = an_execution_contract(contract_id="contract-not-recorded")
    package = build_package(
        tmp_path / "package", contract=missing, acceptance=acceptance_of(prepared)
    )
    fixture = a_traced_fixture(database, prepared, package)
    refused = refusal(database, prepared, package, fixture)
    assert refused.code is TraceRefusal.CONTRACT_NOT_TRACED


def test_a_result_naming_another_package_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package)
    scientific = RaspaScientificContract.from_execution_contract(prepared.contract)
    other = reference_result("0" * 64, contract=scientific)
    refused = refusal(database, prepared, package, fixture, other)
    assert refused.code is TraceRefusal.PACKAGE_HASH_MISMATCH


def test_an_edited_package_manifest_is_refused(
    database, prepare, tmp_path: Path
) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package)
    edited = dict(package.manifest)
    edited["package_sha256"] = "0" * 64
    with database.transaction() as session, pytest.raises(RaspaTraceRefused) as caught:
        trace_result(
            session,
            project_id=prepared.project_id,
            node_id=prepared.node_id,
            manifest=edited,
            result=fixture.result,
            artifact_id=fixture.artifact.artifact_id,
            artifact_version=fixture.version.version,
        )
    assert caught.value.code is TraceRefusal.PACKAGE_HASH_MISMATCH


def test_a_trace_refusal_returns_to_master(database, prepare, tmp_path: Path) -> None:
    prepared = prepared_for_raspa(prepare)
    package = package_for(tmp_path, prepared)
    fixture = a_traced_fixture(database, prepared, package, record_all_inputs=False)
    refused = refusal(database, prepared, package, fixture)
    assert refused.document()["return_to"] == "MASTER"
    assert refused.document()["code"] == "INPUT_UNTRACED"
