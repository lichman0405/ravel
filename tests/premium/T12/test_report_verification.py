"""T12-AC2: a certified report indexes real rows; disagreement is a conflict."""

from __future__ import annotations

import pytest
from tests.premium.T12.support import (
    SOURCE_HASH,
    MemoryStore,
    a_backend_job,
    a_prepared_node,
    a_real_execution,
    a_real_source,
    an_artifact,
    an_evidence,
    assert_report_schema,
    conflict_codes,
    evidence_types,
    report,
)

from ravel.domain.enums import ClaimClass, JobState, ReportDataMode, ReportStatus
from ravel.domain.execution import DeviationRecord
from ravel.reports.models import (
    ClaimRequest,
    EvidenceRequest,
    ReportNotCertified,
    ReportRequest,
)
from ravel.state.repositories.records import DeviationRepository


def test_certified_report_indexes_ledger_rows_and_renders_counts(database, project) -> None:
    prepared = a_prepared_node(database, project.project_id)
    store = MemoryStore()
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    artifact, version = an_artifact(
        database,
        project.project_id,
        store,
        name="conductivity.csv",
        provenance="slurm:1001",
        created_by="slurm",
        node_id=prepared.node_id,
    )
    first = a_real_execution(
        database, prepared, backend="slurm", output_refs=(artifact.artifact_id,)
    )
    second = a_real_execution(
        database, prepared, backend="slurm", output_refs=(artifact.artifact_id,)
    )
    a_backend_job(database, prepared, backend="slurm", state=JobState.COMPLETED)
    with database.transaction() as session:
        DeviationRepository(session, project.project_id).raise_(
            DeviationRecord(
                project_id=project.project_id,
                node_id=prepared.node_id,
                execution_contract_ref=prepared.contract.contract_id,
                requested_action="read_extra_file",
                description="The contract did not name the extra input.",
                raised_by="compute-worker",
            )
        )

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Conductivity after 500 hours",
            evidence=(
                EvidenceRequest(evidence.evidence_id, locator="pp. 12-14"),
                EvidenceRequest(artifact.artifact_id),
                EvidenceRequest(first.execution_id),
                EvidenceRequest(second.execution_id),
            ),
            claims=(
                ClaimRequest(
                    statement="Niobium doping raises stability above the threshold.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(evidence.evidence_id,),
                ),
                ClaimRequest(
                    statement="Two real Slurm runs produced the reported conductivity.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(artifact.artifact_id,),
                    execution_refs=(first.execution_id, second.execution_id),
                    execution_node_id=prepared.node_id,
                    execution_count=2,
                ),
            ),
            limitations=("Bench confirmation is not part of this report.",),
        ),
    )

    assert snapshot.report.status is ReportStatus.CERTIFIED
    assert snapshot.report.data_mode is ReportDataMode.REAL
    assert snapshot.conflicts == ()
    assert evidence_types(snapshot) == {
        evidence.evidence_id: "RESEARCH_SOURCE",
        artifact.artifact_id: "COMPUTATION",
        first.execution_id: "COMPUTATION",
        second.execution_id: "COMPUTATION",
    }
    assert [claim.claim_type for claim in snapshot.claims] == [
        ClaimClass.FACT,
        ClaimClass.FACT,
    ]

    rendered = snapshot.report.rendered
    assert rendered["executions"] == {
        "total": 2,
        "real": 2,
        "simulated": 0,
        "by_status": {"COMPLETED": 2},
        "by_backend": {"slurm": 2},
    }
    assert rendered["backend_jobs"] == {
        "total": 1,
        "real": 1,
        "simulated": 0,
        "by_state": {"COMPLETED": 1},
        "by_backend": {"slurm": 1},
    }
    assert rendered["artifacts"]["total"] == 1
    assert rendered["artifacts"]["index"][0]["content_hash"] == version.content_hash
    assert rendered["reviews"]["total"] == 1
    assert rendered["reviews"]["by_outcome"] == {"PASS": 1}
    assert rendered["reviews"]["by_checkpoint"] == {"PRE_RUN": 1}
    assert rendered["deviations"] == {"total": 1, "open": 1}
    index = rendered["evidence_index"]
    assert [entry["execution_type"] for entry in index] == [
        "RESEARCH_SOURCE",
        "COMPUTATION",
        "COMPUTATION",
        "COMPUTATION",
    ]
    assert index[0]["locator"] == "pp. 12-14"
    assert index[0]["content_hash"] == SOURCE_HASH
    assert rendered["ledger_digest"].startswith("sha256:")

    document = snapshot.certified_json()
    assert_report_schema(document)
    assert len(document["claims"]) == 2
    assert len(document["evidence_refs"]) == 4


def test_fabricated_source_is_a_conflict_not_an_invented_row(database, project) -> None:
    fabricated = "0" * 32
    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Fabricated citation",
            evidence=(EvidenceRequest(fabricated, locator="p. 1"),),
            claims=(
                ClaimRequest(
                    statement="A paper that does not exist supports this.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(fabricated,),
                ),
            ),
        ),
    )

    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert snapshot.report.data_mode is ReportDataMode.REAL
    assert snapshot.evidence_refs == ()
    assert snapshot.claims == ()
    assert conflict_codes(snapshot) == {"FABRICATED_SOURCE"}
    conflict = snapshot.conflicts[0]
    assert conflict["original_record"] is None
    assert conflict["asserted"] == {"evidence_id": fabricated}
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()


def test_hash_mismatch_keeps_the_ledger_hash_and_refuses_export(database, project) -> None:
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    asserted = f"sha256:{'cd' * 32}"

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Asserted hash",
            evidence=(
                EvidenceRequest(
                    evidence.evidence_id,
                    locator="pp. 2-3",
                    content_hash=asserted,
                ),
            ),
            claims=(
                ClaimRequest(
                    statement="The source says the catalyst survives.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(evidence.evidence_id,),
                ),
            ),
        ),
    )

    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert "HASH_MISMATCH" in conflict_codes(snapshot)
    evidence_conflict = next(
        item
        for item in snapshot.conflicts
        if item["code"] == "HASH_MISMATCH" and item["claim_id"] is None
    )
    assert evidence_conflict["asserted"] == {"content_hash": asserted}
    assert evidence_conflict["actual"] == {"content_hash": SOURCE_HASH}
    assert evidence_conflict["original_record"]["content_hash"] == SOURCE_HASH
    assert snapshot.evidence_refs[0].content_hash == SOURCE_HASH
    assert snapshot.claims == ()
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()


def test_source_claim_without_a_locator_is_refused(database, project) -> None:
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="No page range",
            evidence=(EvidenceRequest(evidence.evidence_id),),
            claims=(
                ClaimRequest(
                    statement="The source supports the claim.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(evidence.evidence_id,),
                ),
            ),
        ),
    )

    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert conflict_codes(snapshot) == {"MISSING_LOCATOR"}
    assert evidence_types(snapshot) == {evidence.evidence_id: "RESEARCH_SOURCE"}
    assert snapshot.evidence_refs[0].locator == ""
    assert snapshot.claims == ()
    assert len(snapshot.conflicts) >= 2
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()


def test_wrong_execution_count_keeps_the_ledger_count(database, project) -> None:
    prepared = a_prepared_node(database, project.project_id)
    first = a_real_execution(database, prepared, backend="slurm")
    second = a_real_execution(database, prepared, backend="slurm")

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Wrong count",
            evidence=(EvidenceRequest(first.execution_id),),
            claims=(
                ClaimRequest(
                    statement="Three runs support the result.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(first.execution_id,),
                    execution_refs=(first.execution_id, second.execution_id),
                    execution_node_id=prepared.node_id,
                    execution_count=3,
                ),
            ),
        ),
    )

    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert conflict_codes(snapshot) == {"WRONG_EXECUTION_COUNT"}
    conflict = snapshot.conflicts[0]
    assert conflict["asserted"]["execution_count"] == 3
    assert conflict["original_record"]["execution_count"] == 2
    assert set(conflict["original_record"]["execution_ids"]) == {
        first.execution_id,
        second.execution_id,
    }
    assert snapshot.claims == ()
    assert snapshot.report.rendered["executions"]["total"] == 2
    assert snapshot.report.rendered["executions"]["real"] == 2
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()


def test_computation_claim_must_name_its_executions(database, project) -> None:
    prepared = a_prepared_node(database, project.project_id)
    store = MemoryStore()
    artifact, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="result.csv",
        provenance="slurm:2002",
        created_by="slurm",
        node_id=prepared.node_id,
    )

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Artifact without a run",
            evidence=(EvidenceRequest(artifact.artifact_id),),
            claims=(
                ClaimRequest(
                    statement="The artifact is the computation result.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(artifact.artifact_id,),
                ),
            ),
        ),
    )

    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert conflict_codes(snapshot) == {"MISSING_EXECUTION_REF"}
    assert evidence_types(snapshot) == {artifact.artifact_id: "COMPUTATION"}
    assert snapshot.claims == ()
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()


def test_fabricated_execution_reference_is_a_conflict(database, project) -> None:
    fabricated = "f" * 32
    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Fabricated run",
            claims=(
                ClaimRequest(
                    statement="The fabricated run completed.",
                    claim_type=ClaimClass.HYPOTHESIS,
                    execution_refs=(fabricated,),
                ),
            ),
        ),
    )

    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert conflict_codes(snapshot) == {"FABRICATED_EXECUTION"}
    conflict = snapshot.conflicts[0]
    assert conflict["asserted"] == {"execution_id": fabricated}
    assert conflict["original_record"] is None
    assert snapshot.claims == ()
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()
