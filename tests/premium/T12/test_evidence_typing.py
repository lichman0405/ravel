"""T12-AC1: evidence types come from provenance, not from a report author."""

from __future__ import annotations

from dataclasses import fields

import pytest
from tests.premium.T12.support import (
    MemoryStore,
    a_backend_job,
    a_fixture_source,
    a_prepared_node,
    a_real_execution,
    a_real_source,
    an_artifact,
    an_evidence,
    assert_report_schema,
    evidence_types,
    report,
)

from ravel.domain.artifacts import SIMULATED_KIND, simulated_provenance
from ravel.domain.enums import (
    ClaimClass,
    JobState,
    NodeType,
    ReportDataMode,
    ReportStatus,
)
from ravel.domain.roles import AgentRole
from ravel.reports.models import (
    ClaimRequest,
    EvidenceRequest,
    ReportNotCertified,
    ReportRequest,
)


def test_provenance_decides_every_execution_type(database, project) -> None:
    compute = a_prepared_node(database, project.project_id, node_type=NodeType.COMPUTATION)
    experiment = a_prepared_node(database, project.project_id, node_type=NodeType.EXPERIMENT)
    store = MemoryStore()

    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    simulated, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="mock-run.csv",
        kind=SIMULATED_KIND,
        provenance=simulated_provenance("mock-compute", "COMPUTE_OK"),
        created_by="mock-compute",
        node_id=compute.node_id,
    )
    relabelled, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="looks-real.csv",
        kind="measurement",
        provenance=simulated_provenance("mock-compute", "RELABELLED"),
        created_by="mock-compute",
        node_id=compute.node_id,
    )
    kind_only, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="kind-only.csv",
        kind=SIMULATED_KIND,
        provenance="",
        created_by="compute-worker",
        node_id=compute.node_id,
    )
    real_compute, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="slurm-output.csv",
        provenance="slurm:998877",
        created_by="slurm",
        node_id=compute.node_id,
    )
    lab_upload, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="bench.csv",
        provenance="lab-upload:handover-1",
        created_by="lab-user",
        node_id=experiment.node_id,
    )
    slurm_execution = a_real_execution(database, compute, backend="slurm")
    mock_execution = a_real_execution(database, compute, backend="mock-compute")
    lab_execution = a_real_execution(database, experiment, backend="human-lab")

    request = ReportRequest(
        title="Typed evidence",
        evidence=(
            EvidenceRequest(evidence.evidence_id, locator="pp. 12-14"),
            EvidenceRequest(simulated.artifact_id),
            EvidenceRequest(relabelled.artifact_id),
            EvidenceRequest(kind_only.artifact_id),
            EvidenceRequest(real_compute.artifact_id),
            EvidenceRequest(lab_upload.artifact_id),
            EvidenceRequest(slurm_execution.execution_id),
            EvidenceRequest(mock_execution.execution_id),
            EvidenceRequest(lab_execution.execution_id),
        ),
        claims=(
            ClaimRequest(
                statement="The computed and bench results differ from the mock run.",
                claim_type=ClaimClass.HYPOTHESIS,
                evidence_ids=(
                    evidence.evidence_id,
                    simulated.artifact_id,
                    relabelled.artifact_id,
                    kind_only.artifact_id,
                    real_compute.artifact_id,
                    lab_upload.artifact_id,
                    slurm_execution.execution_id,
                    mock_execution.execution_id,
                    lab_execution.execution_id,
                ),
                execution_refs=(
                    slurm_execution.execution_id,
                    mock_execution.execution_id,
                    lab_execution.execution_id,
                ),
            ),
        ),
    )
    snapshot = report(database, project.project_id, request)

    assert evidence_types(snapshot) == {
        evidence.evidence_id: "RESEARCH_SOURCE",
        simulated.artifact_id: "SIMULATED",
        relabelled.artifact_id: "SIMULATED",
        kind_only.artifact_id: "SIMULATED",
        real_compute.artifact_id: "COMPUTATION",
        lab_upload.artifact_id: "HUMAN_LAB",
        slurm_execution.execution_id: "COMPUTATION",
        mock_execution.execution_id: "SIMULATED",
        lab_execution.execution_id: "HUMAN_LAB",
    }
    assert snapshot.report.data_mode is ReportDataMode.MIXED
    assert snapshot.report.status is ReportStatus.CERTIFIED
    assert snapshot.claims[0].claim_type is ClaimClass.HYPOTHESIS
    document = snapshot.certified_json()
    assert_report_schema(document)
    assert {ref["evidence_id"] for ref in document["evidence_refs"]} == set(
        evidence_types(snapshot)
    )


def test_mock_backend_job_status_marks_the_report_simulated(database, project) -> None:
    compute = a_prepared_node(database, project.project_id)
    a_backend_job(database, compute, backend="mock-compute", state=JobState.COMPLETED)
    fixture = a_fixture_source(database, project.project_id)
    evidence = an_evidence(
        database,
        project.project_id,
        source_ids=(fixture.source_id,),
        statement="The fixture records a completed mock job.",
    )

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Mock backend status",
            evidence=(EvidenceRequest(evidence.evidence_id, locator="fixture p. 1"),),
            claims=(
                ClaimRequest(
                    statement="The mock job completed.",
                    claim_type=ClaimClass.HYPOTHESIS,
                    evidence_ids=(evidence.evidence_id,),
                ),
            ),
            limitations=("No computation took place; this is simulated data.",),
        ),
    )

    assert snapshot.report.data_mode is ReportDataMode.SIMULATED
    assert snapshot.report.status is ReportStatus.CERTIFIED
    assert snapshot.report.rendered["backend_jobs"] == {
        "total": 1,
        "real": 0,
        "simulated": 1,
        "by_state": {"COMPLETED": 1},
        "by_backend": {"mock-compute": 1},
    }
    assert snapshot.report.rendered["executions"]["total"] == 0


def test_fixture_source_is_not_real_research_evidence(database, project) -> None:
    store = MemoryStore()
    fixture = a_fixture_source(database, project.project_id)
    claim_source = an_evidence(
        database,
        project.project_id,
        source_ids=(fixture.source_id,),
        statement="A fixture says niobium is stable.",
    )
    fixture_artifact, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="fixture-parse.json",
        provenance="fixture:parser-case-1",
        created_by="fixture-parser",
    )

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Fixture collection",
            evidence=(
                EvidenceRequest(claim_source.evidence_id, locator="fixture page 1"),
                EvidenceRequest(fixture_artifact.artifact_id),
            ),
            claims=(
                ClaimRequest(
                    statement="The fixture claims niobium is stable.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(claim_source.evidence_id,),
                ),
                ClaimRequest(
                    statement="The parser fixture produced this JSON.",
                    claim_type=ClaimClass.HYPOTHESIS,
                    evidence_ids=(fixture_artifact.artifact_id,),
                ),
            ),
        ),
    )

    assert evidence_types(snapshot) == {
        claim_source.evidence_id: "SIMULATED",
        fixture_artifact.artifact_id: "SIMULATED",
    }
    assert snapshot.report.data_mode is ReportDataMode.SIMULATED
    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert {"SIMULATED_AS_FACT"} <= {conflict["code"] for conflict in snapshot.conflicts}
    assert snapshot.certified is False
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()


def test_a_relabelled_simulated_artifact_never_becomes_a_fact(database, project) -> None:
    compute = a_prepared_node(database, project.project_id)
    store = MemoryStore()
    relabelled, version = an_artifact(
        database,
        project.project_id,
        store,
        name="processed.csv",
        kind="measurement",
        provenance=simulated_provenance("mock-lab", "LAB_OK"),
        created_by="mock-lab",
        node_id=compute.node_id,
    )

    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Relabelling attempt",
            evidence=(EvidenceRequest(relabelled.artifact_id),),
            claims=(
                ClaimRequest(
                    statement="The measurement shows a 20 percent gain.",
                    claim_type=ClaimClass.FACT,
                    evidence_ids=(relabelled.artifact_id,),
                ),
            ),
        ),
    )

    assert evidence_types(snapshot) == {relabelled.artifact_id: "SIMULATED"}
    assert snapshot.report.data_mode is ReportDataMode.SIMULATED
    assert snapshot.report.status is ReportStatus.REPORT_CONFLICT
    assert snapshot.claims == ()
    conflict = next(item for item in snapshot.conflicts if item["code"] == "SIMULATED_AS_FACT")
    assert conflict["asserted"]["claim_type"] == "FACT"
    assert conflict["original_record"][relabelled.artifact_id]["artifact_kind"] == "measurement"
    assert conflict["original_record"][relabelled.artifact_id]["provenance"] == (
        simulated_provenance("mock-lab", "LAB_OK")
    )
    assert version.content_hash is not None
    with pytest.raises(ReportNotCertified):
        snapshot.certified_json()


def test_report_requests_cannot_carry_a_type_or_mode() -> None:
    assert "data_mode" not in {field.name for field in fields(ReportRequest)}
    assert "execution_type" not in {field.name for field in fields(EvidenceRequest)}
    with pytest.raises(TypeError):
        EvidenceRequest(evidence_id="a" * 32, execution_type="COMPUTATION")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ClaimRequest(
            statement="x",
            claim_type=ClaimClass.FACT,
            execution_type="SIMULATED",  # type: ignore[call-arg]
        )


def test_report_assembled_by_research_is_refused(database, project) -> None:
    with pytest.raises(PermissionError):
        report(
            database,
            project.project_id,
            ReportRequest(
                title="Not Research's to write",
                claims=(
                    ClaimRequest(
                        statement="Research may not certify a report.",
                        claim_type=ClaimClass.HYPOTHESIS,
                    ),
                ),
            ),
            role=AgentRole.RESEARCH,
        )
