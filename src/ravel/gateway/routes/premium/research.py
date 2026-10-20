"""Sources, evidence and computations: the project's research record, paged.

Three different things are read here and kept apart, because they answer
different questions. A *source* is a place RAVEL actually reached; *evidence* is
a claim resting on sources; a *computation* is the execution record of work a
backend did. A reader that collapsed them into one list would be unable to tell
what was read from what was concluded from what was run.

The source detail supports a bounded byte range. That is not pagination of a
list: it is the only way to read a stored snapshot without loading an unbounded
document into the Gateway's memory, and a source's content can be large. When a
source has no stored artifact, the metadata is still served and the content
section says it is unavailable, rather than pretending an empty document is the
source.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel

from ravel.domain.evidence import Evidence, EvidenceSource
from ravel.domain.execution import ExecutionRecord
from ravel.gateway.deps import GatewayState, GatewayStateDep, PrincipalDep
from ravel.gateway.routes.artifacts import artifact_store
from ravel.gateway.routes.premium.support import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    paginate,
)
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.records import ExecutionRepository
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
)
from ravel.state.store import ArtifactStoreError

router = APIRouter(tags=["v1-research"])

#: The most bytes one source-content request will read. A source snapshot is
#: text or a small document; a caller that wants more reads the next range.
MAX_SOURCE_BYTES = 100_000

#: How far into a stored source a range may begin. Bounded so that one request
#: cannot ask the Gateway to stream past a hundred megabytes merely to discard
#: them; a reader that far into a source should download the artifact itself.
MAX_SOURCE_OFFSET = 10_000_000

#: Read size while skipping to the requested offset.
SOURCE_CHUNK_BYTES = 64 * 1024


class EvidenceView(BaseModel):
    """One claim together with every source it cites."""

    evidence: Evidence
    sources: list[EvidenceSource]


@router.get(
    "/projects/{project_id}/sources",
    response_model=Page[EvidenceSource],
    summary="Every source this project reached",
)
def list_sources(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
) -> Page[EvidenceSource]:
    """Sources ordered by URL and identifier.

    Sources carry no creation timestamp — a source can be re-read without
    becoming a new row — so the ordering key is the URL and the opaque source
    identifier. Both are stable, which is what a keyset cursor needs.
    """
    with state.database.read_only() as session:
        found = EvidenceSourceRepository(session, standing.project_id).all()
    return paginate(
        found,
        key=lambda source: (source.url, source.source_id),
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/projects/{project_id}/sources/{source_id}",
    response_model=dict[str, Any],
    summary="One source, with a bounded range of its stored content",
)
def read_source(
    standing: PrincipalDep,
    state: GatewayStateDep,
    source_id: str,
    offset: Annotated[int, Query(ge=0, le=MAX_SOURCE_OFFSET)] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_SOURCE_BYTES)] = MAX_SOURCE_BYTES,
) -> dict[str, Any]:
    """The source record, and bytes ``[offset, offset+limit)`` of its snapshot.

    `next_offset` is present only when more bytes remain; a caller pages through
    the content with it. The bytes are decoded as UTF-8 with replacement so a
    text source reads as text; a binary source is still returned this way and
    the `media_type` in the record tells the reader what it was.
    """
    with state.database.read_only() as session:
        source = EvidenceSourceRepository(session, standing.project_id).get(
            source_id=source_id
        )
    return {
        "source": source.model_dump(mode="json"),
        "content": _source_content(state, standing.project_id, source, offset, limit),
    }


@router.get(
    "/projects/{project_id}/evidence",
    response_model=Page[EvidenceView],
    summary="The Evidence Ledger, each claim with its sources",
)
def list_evidence(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
    node_id: Annotated[
        str | None,
        Query(description="Only claims recorded against this research task."),
    ] = None,
) -> Page[EvidenceView]:
    """Claims in the order they were registered, with their citations joined in."""
    with state.database.read_only() as session:
        found = EvidenceRepository(session, standing.project_id).all()
        if node_id is not None:
            found = [item for item in found if item.research_task_ref == node_id]
        sources = {
            source.source_id: source
            for source in EvidenceSourceRepository(session, standing.project_id).all()
        }
    page = paginate(
        found,
        key=lambda item: (item.created_at.isoformat(), item.evidence_id),
        limit=limit,
        cursor=cursor,
    )
    return Page[EvidenceView](
        items=[_view(item, sources) for item in page.items],
        next_cursor=page.next_cursor,
        has_more=page.has_more,
    )


@router.get(
    "/projects/{project_id}/evidence/{evidence_id}",
    response_model=EvidenceView,
    summary="One claim and the sources it rests on",
)
def read_evidence(
    standing: PrincipalDep, state: GatewayStateDep, evidence_id: str
) -> EvidenceView:
    """A single claim. A reference to another project answers as absent."""
    with state.database.read_only() as session:
        evidence = EvidenceRepository(session, standing.project_id).get(
            evidence_id=evidence_id
        )
        sources = {
            source.source_id: source
            for source in EvidenceSourceRepository(session, standing.project_id).all()
        }
    return _view(evidence, sources)


@router.get(
    "/projects/{project_id}/computations",
    response_model=Page[ExecutionRecord],
    summary="Compute and experiment executions",
)
def list_computations(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
) -> Page[ExecutionRecord]:
    """Execution records, oldest first."""
    with state.database.read_only() as session:
        found = ExecutionRepository(session, standing.project_id).all()
    return paginate(
        found,
        key=lambda execution: (execution.created_at.isoformat(), execution.execution_id),
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/projects/{project_id}/computations/{execution_id}",
    response_model=ExecutionRecord,
    summary="One execution record",
)
def read_computation(
    standing: PrincipalDep, state: GatewayStateDep, execution_id: str
) -> ExecutionRecord:
    """One execution by identifier, scoped to this project."""
    with state.database.read_only() as session:
        return ExecutionRepository(session, standing.project_id).get(
            execution_id=execution_id
        )


def _view(evidence: Evidence, sources: dict[str, EvidenceSource]) -> EvidenceView:
    """A claim with the sources it cites, in its own order."""
    return EvidenceView(
        evidence=evidence,
        sources=[
            sources[reference]
            for reference in evidence.source_refs
            if reference in sources
        ],
    )


def _source_content(
    state: GatewayState,
    project_id: str,
    source: EvidenceSource,
    offset: int,
    limit: int,
) -> dict[str, Any]:
    """A bounded range of the source's stored snapshot, or why there is none.

    The stored object is named by `snapshot_ref`, falling back to
    `artifact_ref`: the two references exist because a source may be stored as
    a dedicated snapshot or as an artifact the research seat already registered.
    A reference that does not resolve inside this project is reported as
    unavailable rather than as a 404 — the *source* exists and its metadata is
    the answer; only its bytes are missing.
    """
    reference = source.snapshot_ref or source.artifact_ref
    if not reference:
        return {"available": False, "reason": "this source has no stored content"}

    with state.database.read_only() as session:
        repository = ArtifactRepository(session, project_id)
        try:
            repository.get(artifact_id=reference)
            versions = repository.versions(reference)
        except NotFound:
            versions = []
    if not versions:
        return {
            "available": False,
            "reason": "the artifact this source names is not in this project",
        }
    chosen = versions[-1]

    try:
        store = artifact_store(state)
        stream = store.open(chosen.storage_key)
    except HTTPException as refused:
        if refused.status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
            return {"available": False, "reason": "this deployment has no artifact store"}
        raise
    except ArtifactStoreError:
        return {"available": False, "reason": "the artifact store could not read this source"}

    try:
        skipped = 0
        while skipped < offset:
            block = stream.read(min(SOURCE_CHUNK_BYTES, offset - skipped))
            if not block:
                break
            skipped += len(block)
        raw = stream.read(limit + 1)
    finally:
        stream.close()

    truncated = len(raw) > limit
    body = raw[:limit]
    more = truncated
    return {
        "available": True,
        "artifact_id": chosen.artifact_id,
        "version": chosen.version,
        "content_hash": chosen.content_hash,
        "media_type": chosen.media_type or source.media_type or "application/octet-stream",
        "offset": offset,
        "next_offset": offset + len(body) if more else None,
        "truncated": truncated,
        "text": body.decode("utf-8", errors="replace"),
    }


__all__ = ["EvidenceView", "router"]
