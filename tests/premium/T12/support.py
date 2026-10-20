"""Real ledger rows for T12, and a fake only where storage is external.

Every helper below writes through the repository the production path uses.
The one doubled boundary is `MemoryStore`: report generation reads artifact
metadata and hashes from PostgreSQL, and no test needs MinIO's network to
prove that. Artifact rows, evidence rows, executions, jobs, reviews, reports
and their guards are real PostgreSQL rows.

Fixture-collected sources are marked `fixture://` and fixture artifacts carry
`fixture:` provenance, so a test can never accidentally present them as real
Research evidence — the report classifier treats both as `SIMULATED`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from io import BytesIO
from typing import Any, BinaryIO

from tests.integration.conftest import READ_AT, Prepared, a_source, build_prepared

from ravel.domain.artifacts import Artifact, ArtifactVersion
from ravel.domain.enums import (
    AccessStatus,
    ClaimClass,
    CompletenessVerdict,
    Confidence,
    EvidenceSourceTier,
    JobState,
    NodeType,
    TerminationStatus,
)
from ravel.domain.evidence import Evidence, EvidenceSource
from ravel.domain.execution import BackendJob, CompletenessCheck, ExecutionRecord
from ravel.domain.roles import AgentRole
from ravel.reports.models import ReportRequest, ReportSnapshot
from ravel.state.database import Database
from ravel.state.repositories.records import BackendJobRepository, ExecutionRepository
from ravel.state.repositories.reports import ScientificReportRepository
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceRepository,
)
from ravel.state.store import ArtifactStoreError, StoredObject, hash_chunks

#: The hash `tests.integration.conftest.a_source` writes for a readable source.
SOURCE_HASH = f"sha256:{'ab' * 32}"


class MemoryStore:
    """An `ArtifactStore` held in memory. The only doubled boundary in T12."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}

    def put(
        self, key: str, chunks: Iterable[bytes], *, media_type: str = ""
    ) -> StoredObject:
        if key in self.objects:
            raise ArtifactStoreError(f"{key} already exists; artifacts are immutable")
        data = b"".join(chunks)
        digest, size = hash_chunks([data])
        self.objects[key] = (data, digest)
        return StoredObject(key=key, content_hash=digest, size_bytes=size)

    def get(self, key: str) -> bytes:
        if key not in self.objects:
            raise ArtifactStoreError(f"{key} is absent")
        return self.objects[key][0]

    def open(self, key: str) -> BinaryIO:
        return BytesIO(self.get(key))

    def exists(self, key: str) -> bool:
        return key in self.objects

    def verify(self, key: str, expected_hash: str) -> bool:
        return key in self.objects and self.objects[key][1] == expected_hash

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)


def a_prepared_node(
    database: Database,
    project_id: str,
    *,
    node_type: NodeType = NodeType.COMPUTATION,
) -> Prepared:
    """A READY node with frozen contracts, built through production services."""
    with database.transaction() as session:
        return build_prepared(
            session,
            project_id=project_id,
            node_type=node_type,
            with_acceptance=node_type is not NodeType.RESEARCH,
        )


def a_real_source(
    database: Database,
    project_id: str,
    *,
    url: str = "https://example.org/niobium-stability",
) -> EvidenceSource:
    return a_source(
        database, project_id, url=url, tier=EvidenceSourceTier.A
    )


def a_fixture_source(database: Database, project_id: str) -> EvidenceSource:
    """A source collected from a parser fixture, explicitly non-real."""
    return a_source(
        database,
        project_id,
        url="fixture://papers/niobium-stability.html",
        tier=EvidenceSourceTier.A,
    )


def an_evidence(
    database: Database,
    project_id: str,
    *,
    source_ids: tuple[str, ...],
    claim_class: ClaimClass = ClaimClass.FACT,
    statement: str = "Niobium doping raises conductivity stability.",
) -> Evidence:
    evidence = Evidence(
        project_id=project_id,
        statement=statement,
        claim_class=claim_class,
        source_tier=EvidenceSourceTier.A,
        source_refs=source_ids,
        retrieved_at=READ_AT if source_ids else None,
        content_hash=SOURCE_HASH if source_ids else None,
        access_status=AccessStatus.OK,
        confidence=Confidence.HIGH,
    )
    with database.transaction() as session:
        return EvidenceRepository(session, project_id).register(
            evidence, actor_id="research-agent"
        )


def a_real_execution(
    database: Database,
    prepared: Prepared,
    *,
    backend: str = "slurm",
    status: TerminationStatus = TerminationStatus.COMPLETED,
    output_refs: tuple[str, ...] = (),
    backend_job_ref: str | None = None,
) -> ExecutionRecord:
    execution = ExecutionRecord(
        project_id=prepared.project_id,
        node_id=prepared.node_id,
        execution_contract_ref=prepared.contract.contract_id,
        execution_contract_version=prepared.contract.version,
        backend=backend,
        backend_job_ref=backend_job_ref,
        output_refs=output_refs,
        completeness=CompletenessCheck(verdict=CompletenessVerdict.COMPLETE),
        termination_status=status,
        executed_by=backend,
        started_at=READ_AT,
    )
    with database.transaction() as session:
        return ExecutionRepository(session, prepared.project_id).record(
            execution, actor_id=backend
        )


def a_backend_job(
    database: Database,
    prepared: Prepared,
    *,
    backend: str = "mock-compute",
    state: JobState = JobState.RUNNING,
    attempt: int = 1,
) -> BackendJob:
    job = BackendJob(
        project_id=prepared.project_id,
        node_id=prepared.node_id,
        attempt=attempt,
        execution_contract_ref=prepared.contract.contract_id,
        execution_contract_version=prepared.contract.version,
        backend=backend,
    )
    with database.transaction() as session:
        repository = BackendJobRepository(session, prepared.project_id)
        stored = repository.start(job)
        if state is JobState.SUBMITTED:
            return stored
        if state is not JobState.RUNNING:
            stored = repository.record_state(stored.job_id, JobState.RUNNING)
        return repository.record_state(stored.job_id, state)


def an_artifact(
    database: Database,
    project_id: str,
    store: MemoryStore,
    *,
    name: str,
    kind: str = "",
    provenance: str = "",
    created_by: str = "slurm",
    node_id: str | None = None,
    body: bytes = b"measurement\n",
    filename: str = "",
    media_type: str = "text/csv",
) -> tuple[Artifact, ArtifactVersion]:
    with database.transaction() as session:
        return ArtifactRepository(session, project_id, store).register(
            name=name,
            chunks=[body],
            created_by=created_by,
            filename=filename,
            media_type=media_type,
            kind=kind,
            provenance=provenance,
            node_id=node_id,
        )


def report(
    database: Database,
    project_id: str,
    request: ReportRequest,
    *,
    role: AgentRole = AgentRole.MASTER,
) -> ReportSnapshot:
    with database.transaction() as session:
        return ScientificReportRepository(session, project_id).create(
            request, role=role, actor_id=role.value
        )


def read_report(
    database: Database, project_id: str, report_id: str
) -> ReportSnapshot:
    with database.read_only() as session:
        return ScientificReportRepository(session, project_id).read(report_id)


def explain(
    database: Database,
    project_id: str,
    report_id: str,
    body: str,
    *,
    role: AgentRole = AgentRole.MASTER,
) -> Any:
    with database.transaction() as session:
        return ScientificReportRepository(session, project_id).comment(
            report_id, body, role=role, author=role.value
        )


def assert_report_schema(document: dict[str, Any]) -> None:
    """The frozen `report.schema.json` shape, checked without a new dependency.

    `additionalProperties: false` is enforced by exact key sets at every
    object. Types, lengths, enum vocabularies and the UUID/date-time formats
    are checked explicitly so a report cannot pass by merely being JSON.
    """
    assert set(document) == {
        "report_id",
        "project_id",
        "title",
        "generated_at",
        "data_mode",
        "evidence_refs",
        "claims",
        "limitations",
    }
    uuid.UUID(document["report_id"])
    assert 1 <= len(document["project_id"]) <= 64
    assert 1 <= len(document["title"]) <= 500
    generated_at = datetime.fromisoformat(document["generated_at"])
    assert generated_at.tzinfo is not None
    assert document["data_mode"] in {"REAL", "SIMULATED", "MIXED"}
    assert isinstance(document["evidence_refs"], list)
    for ref in document["evidence_refs"]:
        assert set(ref) == {"evidence_id", "execution_type"}
        assert isinstance(ref["evidence_id"], str) and ref["evidence_id"]
        assert ref["execution_type"] in {
            "RESEARCH_SOURCE",
            "COMPUTATION",
            "HUMAN_LAB",
            "SIMULATED",
        }
    assert isinstance(document["claims"], list)
    for claim in document["claims"]:
        assert set(claim) == {
            "claim_id",
            "claim_type",
            "statement",
            "evidence_ids",
        }
        assert isinstance(claim["claim_id"], str) and claim["claim_id"]
        assert claim["claim_type"] in {"FACT", "INFERENCE", "HYPOTHESIS"}
        assert isinstance(claim["statement"], str) and claim["statement"]
        assert isinstance(claim["evidence_ids"], list)
        assert all(isinstance(item, str) and item for item in claim["evidence_ids"])
    assert isinstance(document["limitations"], list)
    assert all(isinstance(item, str) for item in document["limitations"])


def conflict_codes(snapshot: ReportSnapshot) -> set[str]:
    return {str(conflict["code"]) for conflict in snapshot.conflicts}


def evidence_types(snapshot: ReportSnapshot) -> dict[str, str]:
    return {
        ref.evidence_id: ref.execution_type.value for ref in snapshot.evidence_refs
    }


def utc(year: int = 2026, month: int = 10, day: int = 20) -> datetime:
    return datetime(year, month, day, 12, 0, tzinfo=UTC)
