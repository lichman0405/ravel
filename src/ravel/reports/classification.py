"""Derive what a report citation *is* from the row that holds it.

The report author supplies identifiers and nothing about evidence type. This
module answers, from PostgreSQL rows alone, one of four words:

- `RESEARCH_SOURCE` — a source that was read, not produced by a run.
- `COMPUTATION` — output of a real compute backend.
- `HUMAN_LAB` — a bench delivery filed against a real laboratory handover.
- `SIMULATED` — bytes an explicitly non-real producer wrote: a mock backend, a
  fixture collection, or an artifact whose provenance says so even if its
  `kind` was edited to look like a measurement.

Three rules make the derivation hard to relabel:

1. **Every marker is checked, not just the one a caller controls.** A mock
   artifact is `SIMULATED` if `kind == "simulated"`, if its provenance is
   `mock-*:scenario`, or if its creator names a mock. Editing one of the three
   does not make the other two disappear.
2. **Fixture collections are visibly non-real.** A source whose URL names a
   fixture (`fixture://...`) and an artifact whose provenance begins
   `fixture:` classify as `SIMULATED`. A parser test that collected bytes from
   a fixture can therefore never present them as real Research evidence.
3. **A claim inherits the strongest marker among its sources.** A claim resting
   on one simulated source and one real paper is `SIMULATED`, not an average.

There is no path here that reads a caller-supplied type. The only inputs are
the ledger index's rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ravel.domain.artifacts import is_simulated
from ravel.domain.enums import EvidenceExecutionType
from ravel.state.tables import (
    ArtifactRow,
    ArtifactVersionRow,
    BackendJobRow,
    DagNodeRow,
    DeviationRecordRow,
    EvidenceRow,
    EvidenceSourceRow,
    ExecutionRecordRow,
    ReviewRecordRow,
)

#: Provenance prefixes that are not real measurement even when `kind` is blank.
MOCK_BACKEND_PREFIX = "mock"
FIXTURE_PROVENANCE_PREFIX = "fixture:"
FIXTURE_URL_PREFIX = "fixture://"
LAB_UPLOAD_PREFIX = "lab-upload:"
SLURM_PROVENANCE_PREFIX = "slurm:"


def is_mock_name(name: str) -> bool:
    """Whether a backend or creator name is one of RAVEL's mocks."""
    return name.strip().lower().startswith(MOCK_BACKEND_PREFIX)


def is_fixture_url(url: str) -> bool:
    """Whether a source URL is an explicit non-real fixture reference."""
    lowered = url.strip().lower()
    return lowered.startswith(FIXTURE_URL_PREFIX) or lowered.startswith("fixture:")


def is_fixture_provenance(provenance: str) -> bool:
    """Whether artifact provenance names fixture bytes rather than a run."""
    return provenance.strip().lower().startswith(FIXTURE_PROVENANCE_PREFIX)


def is_simulated_artifact(artifact: ArtifactRow) -> bool:
    """Whether an artifact row is marked non-real by any of its provenance.

    The three checks are redundant on purpose. `kind` is the marker the rest of
    the system enforces, `provenance` is what a mock writes, and `created_by`
    is the backend's name. A report is the last place a relabelled artifact may
    pass as a measurement, so it believes all three rather than the easiest one
    to rewrite.
    """
    return (
        is_simulated(artifact.kind)
        or is_mock_name(artifact.created_by)
        or is_mock_name(artifact.provenance)
        or is_fixture_provenance(artifact.provenance)
    )


def is_simulated_backend(backend: str) -> bool:
    """Whether an execution's backend is one of the mocks."""
    return is_mock_name(backend)


def is_lab_backend(backend: str) -> bool:
    """Whether an execution's backend is the human laboratory backend."""
    return backend.strip().lower() == "human-lab"


@dataclass(frozen=True, slots=True)
class LedgerIndex:
    """The project's ledger rows a report is derived from, read once."""

    evidence: dict[str, EvidenceRow]
    sources: dict[str, EvidenceSourceRow]
    artifacts: dict[str, ArtifactRow]
    versions: dict[str, tuple[ArtifactVersionRow, ...]]
    executions: dict[str, ExecutionRecordRow]
    jobs: dict[str, BackendJobRow]
    reviews: tuple[ReviewRecordRow, ...]
    deviations: tuple[DeviationRecordRow, ...]
    node_types: dict[str, str]

    @classmethod
    def load(cls, session: Session, project_id: str) -> LedgerIndex:
        """Read every indexed row in one project.

        All eight reads are project-scoped at the query, not filtered later, so
        a report cannot name another project's row even by an identifier a
        caller happened to know.
        """

        def rows(table: Any, *order_by: Any) -> list[Any]:
            statement = select(table).where(table.project_id == project_id)
            if order_by:
                statement = statement.order_by(*order_by)
            return list(session.execute(statement).scalars())

        versions: dict[str, list[ArtifactVersionRow]] = {}
        for version in rows(ArtifactVersionRow):
            versions.setdefault(version.artifact_id, []).append(version)
        return cls(
            evidence={row.evidence_id: row for row in rows(EvidenceRow)},
            sources={row.source_id: row for row in rows(EvidenceSourceRow)},
            artifacts={row.artifact_id: row for row in rows(ArtifactRow)},
            versions={
                artifact_id: tuple(sorted(group, key=lambda version: version.version))
                for artifact_id, group in versions.items()
            },
            executions={row.execution_id: row for row in rows(ExecutionRecordRow)},
            jobs={row.job_id: row for row in rows(BackendJobRow)},
            reviews=tuple(
                rows(
                    ReviewRecordRow,
                    ReviewRecordRow.created_at,
                    ReviewRecordRow.review_id,
                )
            ),
            deviations=tuple(sorted(rows(DeviationRecordRow), key=lambda row: row.raised_at)),
            node_types={row.node_id: row.node_type for row in rows(DagNodeRow)},
        )

    def latest_version(self, artifact_id: str) -> ArtifactVersionRow | None:
        """The newest stored version of an artifact, if it has one."""
        stored = self.versions.get(artifact_id, ())
        return stored[-1] if stored else None

    def executions_for_node(self, node_id: str) -> tuple[ExecutionRecordRow, ...]:
        """Every execution record this project holds for one node."""
        return tuple(row for row in self.executions.values() if row.node_id == node_id)


@dataclass(frozen=True, slots=True)
class Classification:
    """A derived evidence type and the ledger facts it was derived from.

    ``missing_source_refs`` is not a classification: it is the evidence row
    naming source identifiers this project does not hold. The report builder
    turns it into a conflict, because an existing evidence row that cites a
    nonexistent source is the nested form of a fabricated source citation.
    """

    execution_type: EvidenceExecutionType
    original_record: dict[str, Any]
    content_hash: str | None
    missing_source_refs: tuple[str, ...] = ()


#: How strongly a marker constrains the claim resting on it. A claim inherits
#: the strongest, so mixing a real paper with a mock run is still SIMULATED.
_SEVERITY: dict[EvidenceExecutionType, int] = {
    EvidenceExecutionType.RESEARCH_SOURCE: 0,
    EvidenceExecutionType.COMPUTATION: 1,
    EvidenceExecutionType.HUMAN_LAB: 2,
    EvidenceExecutionType.SIMULATED: 3,
}


def classify(index: LedgerIndex, evidence_id: str) -> Classification | None:
    """Classify one cited identifier, or `None` if the ledger has no such row.

    Lookup order is fixed so the same identifier resolves the same way every
    time. The four tables share identifier *shapes*, not identifier spaces;
    trying evidence, source, execution and artifact in that order is a total
    rule rather than an accident of dictionary order.
    """
    if evidence_id in index.evidence:
        return _classify_evidence(index, index.evidence[evidence_id])
    if evidence_id in index.sources:
        return _classify_source(index, index.sources[evidence_id])
    if evidence_id in index.executions:
        return _classify_execution(index.executions[evidence_id])
    if evidence_id in index.artifacts:
        return _classify_artifact(index, index.artifacts[evidence_id])
    return None


def _classify_evidence(index: LedgerIndex, evidence: EvidenceRow) -> Classification:
    source_types: dict[str, str] = {}
    originals: list[dict[str, Any]] = []
    missing: list[str] = []
    hashes: list[str] = []
    strongest = EvidenceExecutionType.RESEARCH_SOURCE
    for source_id in evidence.source_refs:
        source = index.sources.get(source_id)
        if source is None:
            missing.append(source_id)
            continue
        classified = _classify_source(index, source)
        source_types[source_id] = classified.execution_type.value
        originals.append(classified.original_record)
        if classified.content_hash:
            hashes.append(classified.content_hash)
        if _SEVERITY[classified.execution_type] > _SEVERITY[strongest]:
            strongest = classified.execution_type
    return Classification(
        execution_type=strongest,
        original_record={
            "kind": "evidence",
            "evidence_id": evidence.evidence_id,
            "claim_class": evidence.claim_class,
            "source_refs": list(evidence.source_refs),
            "source_types": source_types,
            "missing_source_refs": missing,
            "content_hash": evidence.content_hash,
            "sources": originals,
        },
        content_hash=evidence.content_hash or (hashes[0] if hashes else None),
        missing_source_refs=tuple(missing),
    )


def _classify_source(index: LedgerIndex, source: EvidenceSourceRow) -> Classification:
    original: dict[str, Any] = {
        "kind": "source",
        "source_id": source.source_id,
        "url": source.url,
        "access_status": source.access_status,
        "tier": source.tier,
        "content_hash": source.content_hash,
        "artifact_ref": source.artifact_ref,
    }
    if is_fixture_url(source.url):
        return Classification(
            execution_type=EvidenceExecutionType.SIMULATED,
            original_record={**original, "non_real": "fixture-url"},
            content_hash=source.content_hash,
        )
    artifact = index.artifacts.get(source.artifact_ref or "")
    if artifact is None:
        return Classification(
            execution_type=EvidenceExecutionType.RESEARCH_SOURCE,
            original_record={
                **original,
                "artifact_ref_missing": bool(source.artifact_ref),
            },
            content_hash=source.content_hash,
        )
    classified = _artifact_kind(index, artifact)
    original.update(
        {
            "artifact_kind": artifact.kind,
            "artifact_provenance": artifact.provenance,
            "artifact_created_by": artifact.created_by,
        }
    )
    return Classification(
        execution_type=classified or EvidenceExecutionType.RESEARCH_SOURCE,
        original_record=original,
        content_hash=source.content_hash,
    )


def _classify_execution(execution: ExecutionRecordRow) -> Classification:
    if is_simulated_backend(execution.backend):
        execution_type = EvidenceExecutionType.SIMULATED
    elif is_lab_backend(execution.backend):
        execution_type = EvidenceExecutionType.HUMAN_LAB
    else:
        execution_type = EvidenceExecutionType.COMPUTATION
    return Classification(
        execution_type=execution_type,
        original_record={
            "kind": "execution",
            "execution_id": execution.execution_id,
            "node_id": execution.node_id,
            "backend": execution.backend,
            "backend_job_ref": execution.backend_job_ref,
            "termination_status": execution.termination_status,
            "output_refs": list(execution.output_refs),
        },
        content_hash=None,
    )


def _classify_artifact(index: LedgerIndex, artifact: ArtifactRow) -> Classification:
    latest = index.latest_version(artifact.artifact_id)
    return Classification(
        execution_type=_artifact_kind(index, artifact) or EvidenceExecutionType.RESEARCH_SOURCE,
        original_record={
            "kind": "artifact",
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "artifact_kind": artifact.kind,
            "provenance": artifact.provenance,
            "created_by": artifact.created_by,
            "node_id": artifact.node_id,
            "latest_version": latest.version if latest is not None else None,
            "content_hash": latest.content_hash if latest is not None else None,
        },
        content_hash=latest.content_hash if latest is not None else None,
    )


def _artifact_kind(index: LedgerIndex, artifact: ArtifactRow) -> EvidenceExecutionType | None:
    """The provenance-derived kind of an artifact, or `None` if unmarked.

    `None` is not "unknown evidence": it means nothing in the artifact's own
    provenance says it came from a run, so a source that points at it is a
    research source and a direct artifact reference is treated as one too.
    """
    if is_simulated_artifact(artifact):
        return EvidenceExecutionType.SIMULATED
    provenance = artifact.provenance.strip().lower()
    node_type = index.node_types.get(artifact.node_id or "", "")
    if provenance.startswith(LAB_UPLOAD_PREFIX) or node_type == "EXPERIMENT":
        return EvidenceExecutionType.HUMAN_LAB
    if (
        provenance.startswith(SLURM_PROVENANCE_PREFIX)
        or is_mock_name(artifact.created_by)
        or node_type == "COMPUTATION"
    ):
        return EvidenceExecutionType.COMPUTATION
    return None


def artifact_is_simulated(index: LedgerIndex, artifact: ArtifactRow) -> bool:
    """Whether this artifact counts as simulated in the deterministic facts."""
    return _artifact_kind(index, artifact) is EvidenceExecutionType.SIMULATED


__all__ = [
    "Classification",
    "LedgerIndex",
    "artifact_is_simulated",
    "classify",
    "is_fixture_provenance",
    "is_fixture_url",
    "is_lab_backend",
    "is_mock_name",
    "is_simulated_artifact",
    "is_simulated_backend",
]
