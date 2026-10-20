"""P2: a simulated lab handover is not reportable as HUMAN_LAB evidence.

The handover's execution mode and the backend name recorded on an Execution
Record have to agree. A simulated lab run is a software proof of the handover,
not a person's bench result, and T12 derives the evidence type from the
backend name. These tests read that derivation rather than trusting the label
in a response: both a mock-prefixed simulated backend and a real `human-lab`
backend are written as ledger rows, and a report is assembled over each.
"""

from __future__ import annotations

from tests.support.lab import Handed

from ravel.backends.lab import (
    REAL_BACKEND_NAME,
    SIMULATED_BACKEND_NAME,
    HumanLabBackend,
)
from ravel.config import Settings
from ravel.domain.enums import (
    ClaimClass,
    CompletenessVerdict,
    EvidenceExecutionType,
    ReportDataMode,
    TerminationStatus,
)
from ravel.domain.execution import CompletenessCheck, ExecutionRecord
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.reports.models import ClaimRequest, EvidenceRequest, ReportRequest
from ravel.state.database import Database
from ravel.state.repositories.records import ExecutionRepository
from ravel.state.repositories.reports import ScientificReportRepository
from ravel.state.store import S3ArtifactStore

from .support import hand_over_live, terms_with_sop


def record_execution(database: Database, run: Handed) -> ExecutionRecord:
    """A completed execution whose backend is exactly the handover's backend."""
    execution = ExecutionRecord(
        project_id=run.prepared.project_id,
        node_id=run.node_id,
        execution_contract_ref=run.prepared.contract.contract_id,
        execution_contract_version=run.prepared.contract.version,
        backend=run.handover.backend,
        backend_job_ref=run.ref,
        completeness=CompletenessCheck(verdict=CompletenessVerdict.COMPLETE),
        termination_status=TerminationStatus.COMPLETED,
        executed_by=run.handover.backend,
    )
    with database.transaction() as session:
        return ExecutionRepository(
            session, run.prepared.project_id
        ).record(execution, actor_id=run.handover.backend)


def assemble(database: Database, project_id: str, execution: ExecutionRecord):
    """A report that cites one execution, through the production repository."""
    request = ReportRequest(
        title=f"Typing {execution.backend}",
        evidence=(EvidenceRequest(execution.execution_id),),
        claims=(
            ClaimRequest(
                statement="This execution supports the reported result.",
                claim_type=ClaimClass.HYPOTHESIS,
                evidence_ids=(execution.execution_id,),
                execution_refs=(execution.execution_id,),
            ),
        ),
    )
    with database.transaction() as session:
        return ScientificReportRepository(session, project_id).create(
            request, role=AgentRole.MASTER, actor_id=AgentRole.MASTER.value
        )


def test_the_backend_name_is_derived_from_the_mode(database: Database) -> None:
    assert HumanLabBackend(database=database).name == SIMULATED_BACKEND_NAME
    assert HumanLabBackend(database=database, live_lab=True).name == REAL_BACKEND_NAME
    assert SIMULATED_BACKEND_NAME.startswith("mock"), (
        "the report classifier derives SIMULATED from the mock prefix"
    )


async def test_a_simulated_handover_cannot_be_reported_as_human_lab_evidence(
    database: Database,
    project: Project,
    handed,
) -> None:
    """A SIMULATED handover is SIMULATED evidence, not bench evidence.

    The project has no other executions, so the report's data mode is derived
    from this one ledger row alone. Both the mode and the typed evidence
    reference are read back from the assembled report, not from the handover.
    """
    run = await handed()
    assert run.handover.execution_mode.value == "SIMULATED"
    assert run.handover.backend == SIMULATED_BACKEND_NAME

    execution = record_execution(database, run)
    report = assemble(database, project.project_id, execution)

    assert report.report.data_mode is ReportDataMode.SIMULATED
    assert [
        reference.execution_type for reference in report.evidence_refs
    ] == [EvidenceExecutionType.SIMULATED], (
        "a simulated lab handover was certified as human-lab evidence"
    )
    assert all(
        reference.execution_type is not EvidenceExecutionType.HUMAN_LAB
        for reference in report.evidence_refs
    )


async def test_a_real_handover_is_reported_as_human_lab_evidence(
    database: Database,
    project: Project,
    prepare,
    gateway_settings: Settings,
    artifact_store: S3ArtifactStore,
) -> None:
    """The REAL mode keeps its `human-lab` name and classification.

    This is software proof that the mode-derived name still reaches T12's
    classifier as HUMAN_LAB; it is not instrument evidence.
    """
    prepared = prepare(**terms_with_sop())
    run = await hand_over_live(
        database=database,
        settings=gateway_settings,
        store=artifact_store,
        prepared=prepared,
    )
    assert run.handover.execution_mode.value == "REAL"
    assert run.handover.backend == REAL_BACKEND_NAME

    execution = record_execution(database, run)
    report = assemble(database, project.project_id, execution)

    assert report.report.data_mode is ReportDataMode.REAL
    assert [
        reference.execution_type for reference in report.evidence_refs
    ] == [EvidenceExecutionType.HUMAN_LAB], (
        "a REAL handover stopped being human-lab evidence"
    )
