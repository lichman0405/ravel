"""Evidence, artifacts, and the boundary between them.

`ArtifactRepository.register` is the one place where bytes enter RAVEL, and the
ordering there is deliberate: the object is written to storage *first*, and only
then is the metadata row staged. If the transaction rolls back, the object
remains in storage as an orphan — harmless, and reclaimable — whereas the
opposite order would leave a row pointing at bytes that do not exist, which is
a broken record that no later process can repair.

Evidence is separate from artifacts on purpose. The same PDF can support one
claim and contradict another, so a claim is its own record that *references*
sources and artifacts and carries the retrieval metadata that makes it
checkable.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ravel.domain.artifacts import (
    Artifact,
    ArtifactVersion,
    artifact_key,
    is_simulated,
)
from ravel.domain.enums import AccessStatus, ClaimClass
from ravel.domain.events import ActorType, ProjectEventType
from ravel.domain.evidence import (
    Evidence,
    EvidenceConflict,
    EvidenceSource,
    ResearchRecord,
)
from ravel.state.mapping import from_row
from ravel.state.outbox import emit
from ravel.state.repositories.base import ProjectScopedRepository
from ravel.state.store import ArtifactStore, ArtifactStoreError
from ravel.state.tables import (
    ArtifactRow,
    ArtifactVersionRow,
    EvidenceConflictRow,
    EvidenceRow,
    EvidenceSourceRow,
    ResearchRecordRow,
)


class SimulatedEvidenceError(ValueError):
    """Raised when a claim would rest on bytes a mock produced.

    A distinct type rather than a bare `ValueError`, because this refusal is a
    rule about what evidence *is* rather than a complaint about a malformed
    field, and a caller that catches it is catching something specific.
    """


class EvidenceSourceRepository(ProjectScopedRepository[EvidenceSource]):
    """Every place a claim came from, as it was actually reached."""

    row_type = EvidenceSourceRow
    record_type = EvidenceSource

    def _order_by(self) -> Any:
        return EvidenceSourceRow.url

    def readable(self) -> list[EvidenceSource]:
        """Sources RAVEL actually obtained the content of."""
        return self.all(access_status=AccessStatus.OK.value)

    def record(self, source: EvidenceSource) -> EvidenceSource:
        """Store one source row, unless it rests on simulated bytes.

        No event: a source is a component of evidence, not a project event. The
        `EVIDENCE_REGISTERED` event is emitted once, when the claim resting on
        it is written.

        Raises:
            SimulatedEvidenceError: The source points at an artifact a mock
                backend produced.
        """
        self._refuse_simulated(source)
        return self.add(source)

    def _refuse_simulated(self, source: EvidenceSource) -> None:
        """Refuse a source whose bytes came from a mock.

        This is the choke point, and it is here rather than in `Evidence`
        because the question is about another row: whether a source rests on
        simulated bytes can only be answered by someone holding the session.

        A guard rather than a convention, because a convention is enforced by
        whoever remembers it. `docs/06_EXECUTION_AND_REVIEW.md` §6 makes this
        absolute — mock output must never enter the Evidence Ledger as real
        scientific evidence — and an absolute rule that is only written down is
        a rule that holds until someone is in a hurry.

        The lookup is scoped to this project. A reference to an artifact in
        another project resolves to nothing here, which refuses the source for
        the ordinary reason: the bytes are not reachable from this scope.
        """
        if not source.artifact_ref:
            return
        kind = self.session.execute(
            select(ArtifactRow.kind).where(
                ArtifactRow.project_id == self.project_id,
                ArtifactRow.artifact_id == source.artifact_ref,
            )
        ).scalar_one_or_none()
        if kind is not None and is_simulated(kind):
            raise SimulatedEvidenceError(
                f"source {source.url} rests on artifact {source.artifact_ref}, which "
                f"is marked {kind!r}; simulated output is not evidence, and admitting "
                "it here would put it beyond every later reader's ability to tell"
            )


class EvidenceRepository(ProjectScopedRepository[Evidence]):
    """The Evidence Ledger."""

    row_type = EvidenceRow
    record_type = Evidence

    def register(self, evidence: Evidence, *, actor_id: str) -> Evidence:
        """Add a claim to the ledger and announce it.

        Raises:
            ProjectScopeError: The claim belongs to another project.
        """
        self.add(evidence)
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.EVIDENCE_REGISTERED,
            actor_type=ActorType.AGENT,
            actor_id=actor_id,
            payload={
                "evidence_id": evidence.evidence_id,
                "display_id": evidence.display_id,
                "claim_class": evidence.claim_class.value,
                "source_tier": evidence.source_tier.value,
            },
        )
        return evidence

    def facts(self) -> list[Evidence]:
        """Every claim recorded as a fact."""
        return self.all(claim_class=ClaimClass.FACT.value)

    def for_task(self, research_task_ref: str) -> list[Evidence]:
        """Every claim a research task produced."""
        return self.all(research_task_ref=research_task_ref)

    def supported_by(self, source_id: str) -> list[Evidence]:
        """Every claim resting on one source."""
        return [
            evidence
            for evidence in self.all()
            if source_id in evidence.source_refs
        ]


class EvidenceConflictRepository(ProjectScopedRepository[EvidenceConflict]):
    """Disagreements between evidence rows, recorded rather than averaged away."""

    row_type = EvidenceConflictRow
    record_type = EvidenceConflict

    def unresolved(self) -> list[EvidenceConflict]:
        """Every conflict no decision has resolved yet."""
        return [conflict for conflict in self.all() if conflict.resolution is None]


class ResearchRecordRepository(ProjectScopedRepository[ResearchRecord]):
    """A Research Agent's structured output for one task."""

    row_type = ResearchRecordRow
    record_type = ResearchRecord

    def for_task(self, task_id: str) -> list[ResearchRecord]:
        """Every record produced for one task."""
        return self.all(task_id=task_id)

    def for_node(self, node_id: str) -> list[ResearchRecord]:
        """Every record produced for one DAG node."""
        return self.all(node_id=node_id)


@dataclass(frozen=True)
class TaskLedger:
    """Everything one research task produced, read as one thing.

    The four parts are only meaningful together: a claim without its sources
    cannot be checked, a source no claim rests on is a lead rather than
    evidence, a conflict is about claims rather than owned by one, and a record
    is the answer assembled from all three. Reading them separately is how a
    reader ends up with the record and none of what it rests on.

    The Research seat reads this to continue its own task. Review reads it to
    judge one — the same rows, so the seat that produced the answer and the
    seat that judges it are looking at the same delivery. That is the whole
    reason it is here rather than in either tool module.
    """

    claims: tuple[Evidence, ...]
    sources: tuple[EvidenceSource, ...]
    conflicts: tuple[EvidenceConflict, ...]
    records: tuple[ResearchRecord, ...]


def task_ledger(session: Session, project_id: str, node_id: str) -> TaskLedger:
    """The ledger for one research node: its claims, sources, conflicts, records.

    Keyed by the node rather than by the task id, because that is the identity
    both readers hold: a research task *is* a DAG node, and Review is given a
    node to judge rather than a task to continue.
    """
    claims = EvidenceRepository(session, project_id).for_task(node_id)
    sources = EvidenceSourceRepository(session, project_id)
    named = [sources.get(source_id=ref) for ref in source_refs(claims)]
    return TaskLedger(
        claims=tuple(claims),
        sources=tuple(named),
        conflicts=tuple(conflicts_for(session, project_id, claims)),
        records=tuple(ResearchRecordRepository(session, project_id).for_node(node_id)),
    )


def source_refs(claims: Iterable[Evidence]) -> tuple[str, ...]:
    """Every source any of these claims rests on, each named once."""
    return tuple(dict.fromkeys(ref for claim in claims for ref in claim.source_refs))


def conflicts_for(
    session: Session, project_id: str, claims: Iterable[Evidence]
) -> list[EvidenceConflict]:
    """The conflict records that are about these claims.

    Filtered rather than queried by task, because a conflict has no task of its
    own: it is about evidence rows, and which task those belong to is the
    question being asked.
    """
    named = {claim.evidence_id for claim in claims}
    return [
        conflict
        for conflict in EvidenceConflictRepository(session, project_id).all()
        if named & set(conflict.evidence_refs)
    ]


class ArtifactRepository(ProjectScopedRepository[Artifact]):
    """Artifact metadata, and the store the bytes go to.

    The store is optional because reading metadata is not reading bytes. A
    caller that only reports what a node produced — a reviewer looking at what
    it has to judge, a view listing a project's artifacts — needs PostgreSQL
    and nothing else, and requiring object-storage credentials of it would make
    the metadata unreadable exactly when the store is the thing that is broken.
    Every method that touches bytes asks for the store and says so when it is
    absent.
    """

    row_type = ArtifactRow
    record_type = Artifact

    def __init__(
        self, session: Session, project_id: str, store: ArtifactStore | None = None
    ) -> None:
        super().__init__(session, project_id)
        self.store = store

    def _store(self) -> ArtifactStore:
        """The store, refusing a byte-level operation that has none.

        Raises:
            ArtifactStoreError: This repository was built without a store.
        """
        if self.store is None:
            raise ArtifactStoreError(
                "this artifact repository has no object store, so it can read "
                "artifact metadata but not artifact bytes"
            )
        return self.store

    def register(
        self,
        *,
        name: str,
        chunks: Iterable[bytes],
        created_by: str,
        filename: str = "",
        media_type: str = "",
        kind: str = "",
        provenance: str = "",
        node_id: str | None = None,
        execution_ref: str | None = None,
        note: str = "",
    ) -> tuple[Artifact, ArtifactVersion]:
        """Store bytes and register them as the next version of a new artifact.

        Returns the artifact and the version that was written.

        Raises:
            ArtifactStoreError: The object store refused the write.
        """
        artifact = Artifact(
            project_id=self.project_id,
            name=name,
            kind=kind,
            media_type=media_type,
            provenance=provenance,
            node_id=node_id,
            created_by=created_by,
        )
        key = artifact_key(
            self.project_id, artifact.artifact_id, 1, filename or _safe_name(name)
        )
        stored = self._store().put(key, chunks, media_type=media_type)
        version = ArtifactVersion(
            artifact_id=artifact.artifact_id,
            project_id=self.project_id,
            version=1,
            storage_key=stored.key,
            content_hash=stored.content_hash,
            size_bytes=stored.size_bytes,
            media_type=media_type,
            filename=filename or _safe_name(name),
            created_by=created_by,
            node_id=node_id,
            execution_ref=execution_ref,
            note=note,
        )
        self._stage(artifact, version)
        return artifact, version

    def add_version(
        self,
        artifact_id: str,
        *,
        chunks: Iterable[bytes],
        created_by: str,
        filename: str = "",
        media_type: str = "",
        node_id: str | None = None,
        execution_ref: str | None = None,
        note: str = "",
    ) -> ArtifactVersion:
        """Store bytes as the next version of an existing artifact.

        A re-run with corrected inputs produces version 2 of the same artifact;
        the node's reference does not have to change to point at the newer bytes,
        and version 1 remains readable.

        Raises:
            NotFound: This project has no such artifact.
            ArtifactStoreError: The object store refused the write.
        """
        artifact = self.get(artifact_id=artifact_id)
        number = self.latest_version_number(artifact_id) + 1
        name = filename or _safe_name(artifact.name)
        key = artifact_key(self.project_id, artifact_id, number, name)
        stored = self._store().put(key, chunks, media_type=media_type)
        version = ArtifactVersion(
            artifact_id=artifact_id,
            project_id=self.project_id,
            version=number,
            storage_key=stored.key,
            content_hash=stored.content_hash,
            size_bytes=stored.size_bytes,
            media_type=media_type or artifact.media_type,
            filename=name,
            created_by=created_by,
            node_id=node_id,
            execution_ref=execution_ref,
            note=note,
        )
        self.session.add(
            ArtifactVersionRow(**version.model_dump(mode="python"))
        )
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.ARTIFACT_REGISTERED,
            actor_type=ActorType.AGENT,
            actor_id=created_by,
            payload={
                "artifact_id": artifact_id,
                "version": number,
                "content_hash": stored.content_hash,
            },
        )
        return version

    def _stage(self, artifact: Artifact, version: ArtifactVersion) -> None:
        """Stage the artifact, its first version, and the event together."""
        self.add(artifact)
        self.session.add(ArtifactVersionRow(**version.model_dump(mode="python")))
        emit(
            self.session,
            project_id=self.project_id,
            event_type=ProjectEventType.ARTIFACT_REGISTERED,
            actor_type=ActorType.AGENT,
            actor_id=artifact.created_by,
            payload={
                "artifact_id": artifact.artifact_id,
                "display_id": artifact.display_id,
                "version": version.version,
                "content_hash": version.content_hash,
            },
        )

    def versions(self, artifact_id: str) -> list[ArtifactVersion]:
        """Every version of an artifact, oldest first."""
        rows = (
            self.session.query(ArtifactVersionRow)
            .filter(
                ArtifactVersionRow.project_id == self.project_id,
                ArtifactVersionRow.artifact_id == artifact_id,
            )
            .order_by(ArtifactVersionRow.version)
            .all()
        )
        return [from_row(ArtifactVersion, row) for row in rows]

    def latest_version_number(self, artifact_id: str) -> int:
        """The highest version number, or zero if the artifact has none."""
        highest = self.session.execute(
            select(func.max(ArtifactVersionRow.version)).where(
                ArtifactVersionRow.project_id == self.project_id,
                ArtifactVersionRow.artifact_id == artifact_id,
            )
        ).scalar_one_or_none()
        return int(highest or 0)

    def verify(self, version: ArtifactVersion) -> bool:
        """Whether the stored bytes still hash to what the record claims."""
        return self._store().verify(version.storage_key, version.content_hash)

    def read(self, version: ArtifactVersion) -> bytes:
        """The bytes of one version.

        Raises:
            ArtifactStoreError: The object is missing or unreadable. A record
                whose bytes are gone is a real fault, reported rather than
                returned as an empty file.
        """
        if version.project_id != self.project_id:
            raise ArtifactStoreError(
                f"version {version.version_id} belongs to project "
                f"{version.project_id}, not {self.project_id}"
            )
        return self._store().get(version.storage_key)


def _safe_name(name: str) -> str:
    """A filename derived from an artifact name, safe to use in a storage key."""
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in name
    )
    cleaned = cleaned.strip("._") or "artifact"
    return cleaned[:120]
