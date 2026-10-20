"""T12-AC3: a report is an immutable snapshot; commentary is a separate row."""

from __future__ import annotations

import pytest
from tests.premium.T12.support import (
    MemoryStore,
    a_prepared_node,
    a_real_execution,
    a_real_source,
    a_review,
    an_artifact,
    an_evidence,
    assert_report_schema,
    explain,
    read_report,
    report,
    utc,
)

from ravel.domain.enums import ClaimClass, ReviewCheckpoint, ReviewOutcome
from ravel.domain.roles import AgentRole
from ravel.reports.models import (
    ClaimRequest,
    EvidenceRequest,
    ReportRequest,
    ReportSnapshot,
)
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.reports import ScientificReportRepository


def _request(evidence_id: str, artifact_id: str, execution_id: str) -> ReportRequest:
    return ReportRequest(
        title="Frozen conductivity report",
        evidence=(
            EvidenceRequest(evidence_id, locator="pp. 4-6"),
            EvidenceRequest(artifact_id),
            EvidenceRequest(execution_id),
        ),
        claims=(
            ClaimRequest(
                statement="The source and the run agree.",
                claim_type=ClaimClass.HYPOTHESIS,
                evidence_ids=(evidence_id, artifact_id),
                execution_refs=(execution_id,),
            ),
        ),
        limitations=("Only one dopant concentration was measured.",),
    )


def test_report_is_frozen_against_later_ledger_changes(database, project) -> None:
    prepared = a_prepared_node(database, project.project_id)
    store = MemoryStore()
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    artifact, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="run-1.csv",
        provenance="slurm:3003",
        created_by="slurm",
        node_id=prepared.node_id,
    )
    first = a_real_execution(database, prepared, backend="slurm")
    request = _request(evidence.evidence_id, artifact.artifact_id, first.execution_id)

    original = report(database, project.project_id, request)
    original_json = original.certified_json()
    original_digest = original.report.rendered["ledger_digest"]
    assert original.report.rendered["executions"]["total"] == 1

    second = a_real_execution(database, prepared, backend="slurm")
    assert second.execution_id != first.execution_id

    frozen = read_report(database, project.project_id, original.report.report_id)
    assert frozen.report.rendered["executions"]["total"] == 1
    assert frozen.report.rendered["executions"]["real"] == 1
    assert frozen.report.rendered["ledger_digest"] == original_digest
    assert frozen.report_json() == original_json
    assert frozen.certified_json() == original_json
    assert frozen.report.status is original.report.status
    assert [claim.claim_id for claim in frozen.claims] == [
        claim.claim_id for claim in original.claims
    ]

    regenerated = report(database, project.project_id, request)
    assert regenerated.report.report_id != original.report.report_id
    assert regenerated.report.rendered["executions"]["total"] == 2
    assert regenerated.report.rendered["executions"]["real"] == 2
    assert regenerated.report.rendered["ledger_digest"] != original_digest
    assert _claim_ids(regenerated) != _claim_ids(original)

    still_frozen = read_report(database, project.project_id, original.report.report_id)
    assert still_frozen.report.rendered["executions"]["total"] == 1
    assert still_frozen.report_json() == original_json


def test_master_commentary_cannot_change_rendered_facts(database, project) -> None:
    prepared = a_prepared_node(database, project.project_id)
    store = MemoryStore()
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    artifact, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="run-2.csv",
        provenance="slurm:4004",
        created_by="slurm",
        node_id=prepared.node_id,
    )
    execution = a_real_execution(database, prepared, backend="slurm")
    snapshot = report(
        database,
        project.project_id,
        _request(evidence.evidence_id, artifact.artifact_id, execution.execution_id),
    )
    before = snapshot.certified_json()
    before_rendered = snapshot.report.rendered
    before_claims = [
        (claim.claim_id, claim.claim_type, claim.statement) for claim in snapshot.claims
    ]

    explain(
        database,
        project.project_id,
        snapshot.report.report_id,
        "Master explains why the single concentration is enough for this stage.",
    )

    after = read_report(database, project.project_id, snapshot.report.report_id)
    assert after.commentary is not None
    assert after.commentary.role is AgentRole.MASTER
    assert after.commentary.author == AgentRole.MASTER.value
    assert "single concentration" in after.commentary.body

    assert after.report_json() == before
    assert after.certified_json() == before
    assert after.report.rendered == before_rendered
    assert [
        (claim.claim_id, claim.claim_type, claim.statement) for claim in after.claims
    ] == before_claims
    assert "commentary" not in after.report_json()

    with pytest.raises(ValueError, match="already has Master's explanation"):
        explain(
            database,
            project.project_id,
            snapshot.report.report_id,
            "A second explanation cannot be attached to the same snapshot.",
        )
    assert (
        read_report(database, project.project_id, snapshot.report.report_id).report_json() == before
    )


@pytest.mark.parametrize(
    "role",
    [
        pytest.param(AgentRole.RESEARCH, id="research"),
        pytest.param(AgentRole.REVIEW, id="review"),
        pytest.param(AgentRole.COMPUTE_WORKER, id="compute-worker"),
    ],
)
def test_only_master_may_explain_a_report(database, project, role: AgentRole) -> None:
    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Unexplained",
            claims=(
                ClaimRequest(
                    statement="There is nothing to explain yet.",
                    claim_type=ClaimClass.HYPOTHESIS,
                ),
            ),
        ),
    )

    with pytest.raises(PermissionError):
        explain(
            database,
            project.project_id,
            snapshot.report.report_id,
            "I am not Master.",
            role=role,
        )

    after = read_report(database, project.project_id, snapshot.report.report_id)
    assert after.commentary is None


def test_review_reads_frozen_criteria_and_raw_artifact_metadata(database, project) -> None:
    prepared = a_prepared_node(database, project.project_id)
    assert prepared.acceptance is not None
    store = MemoryStore()
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    artifact, version = an_artifact(
        database,
        project.project_id,
        store,
        name="bench.csv",
        provenance="lab-upload:handover-9",
        created_by="lab-user",
        node_id=prepared.node_id,
    )
    execution = a_real_execution(database, prepared, backend="human-lab")
    request = ReportRequest(
        title="Review's report",
        evidence=(
            EvidenceRequest(evidence.evidence_id, locator="pp. 9-10"),
            EvidenceRequest(artifact.artifact_id),
            EvidenceRequest(execution.execution_id),
        ),
        claims=(
            ClaimRequest(
                statement="The bench run agrees with the source.",
                claim_type=ClaimClass.HYPOTHESIS,
                evidence_ids=(evidence.evidence_id, artifact.artifact_id),
                execution_refs=(execution.execution_id,),
                execution_node_id=prepared.node_id,
                execution_count=1,
            ),
        ),
    )

    snapshot = report(database, project.project_id, request, role=AgentRole.REVIEW)
    assert snapshot.certified is True

    reviewed = read_report(database, project.project_id, snapshot.report.report_id)
    document = reviewed.certified_json()
    assert_report_schema(document)

    reviews = reviewed.report.rendered["reviews"]
    assert reviews["total"] == 1
    entry = reviews["index"][0]
    assert entry["node_id"] == prepared.node_id
    assert entry["checkpoint"] == ReviewCheckpoint.PRE_RUN.value
    assert entry["outcome"] == ReviewOutcome.PASS.value
    assert entry["frozen_criteria_ref"] == prepared.acceptance.contract_id
    assert entry["frozen_criteria_version"] == prepared.acceptance.version

    artifacts = reviewed.report.rendered["artifacts"]
    assert artifacts["total"] == 1
    raw = artifacts["index"][0]
    assert raw["artifact_id"] == artifact.artifact_id
    assert raw["latest_version"] == version.version
    assert raw["content_hash"] == version.content_hash
    assert raw["provenance"] == "lab-upload:handover-9"
    assert store.verify(version.storage_key, version.content_hash)

    evidence_index = {
        item["evidence_id"]: item for item in reviewed.report.rendered["evidence_index"]
    }
    assert evidence_index[evidence.evidence_id]["locator"] == "pp. 9-10"


def test_two_reports_over_one_ledger_render_the_same_facts(database, project) -> None:
    prepared = a_prepared_node(database, project.project_id)
    store = MemoryStore()
    source = a_real_source(database, project.project_id)
    evidence = an_evidence(database, project.project_id, source_ids=(source.source_id,))
    artifact, _ = an_artifact(
        database,
        project.project_id,
        store,
        name="run-3.csv",
        provenance="slurm:5005",
        created_by="slurm",
        node_id=prepared.node_id,
    )
    execution = a_real_execution(database, prepared, backend="slurm")
    tied_at = utc()
    tied_reviews = (
        a_review(database, prepared, created_at=tied_at),
        a_review(database, prepared, created_at=tied_at),
    )
    tied_ids = {review.review_id for review in tied_reviews}
    request = _request(evidence.evidence_id, artifact.artifact_id, execution.execution_id)

    first = report(database, project.project_id, request)
    second = report(database, project.project_id, request)

    assert first.report.report_id != second.report.report_id
    assert first.report.rendered == second.report.rendered
    reviews = first.report.rendered["reviews"]
    assert reviews["total"] == 3
    tied_order = [
        entry["review_id"] for entry in reviews["index"] if entry["review_id"] in tied_ids
    ]
    assert tied_order == sorted(tied_ids)
    assert first.report.data_mode is second.report.data_mode
    assert [(ref.evidence_id, ref.execution_type) for ref in first.evidence_refs] == [
        (ref.evidence_id, ref.execution_type) for ref in second.evidence_refs
    ]
    assert [
        (claim.claim_type, claim.statement, claim.evidence_ids, claim.execution_refs)
        for claim in first.claims
    ] == [
        (claim.claim_type, claim.statement, claim.evidence_ids, claim.execution_refs)
        for claim in second.claims
    ]


def test_reports_and_commentary_are_project_scoped(database, project, other_project) -> None:
    snapshot = report(
        database,
        project.project_id,
        ReportRequest(
            title="Only this project's",
            claims=(
                ClaimRequest(
                    statement="Scoped to its project.",
                    claim_type=ClaimClass.HYPOTHESIS,
                ),
            ),
        ),
    )

    with database.read_only() as session:
        foreign = ScientificReportRepository(session, other_project.project_id)
        with pytest.raises(NotFound):
            foreign.read(snapshot.report.report_id)
        with pytest.raises(NotFound):
            foreign.certified_json(snapshot.report.report_id)
        with pytest.raises(NotFound):
            foreign.commentary_for(snapshot.report.report_id)

    with database.transaction() as session:
        foreign = ScientificReportRepository(session, other_project.project_id)
        with pytest.raises(NotFound):
            foreign.comment(
                snapshot.report.report_id,
                "A comment from the wrong project.",
                role=AgentRole.MASTER,
                author=AgentRole.MASTER.value,
            )

    own = read_report(database, project.project_id, snapshot.report.report_id)
    assert own.report.project_id == project.project_id
    assert own.commentary is None


def _claim_ids(snapshot: ReportSnapshot) -> tuple[str, ...]:
    return tuple(claim.claim_id for claim in snapshot.claims)
