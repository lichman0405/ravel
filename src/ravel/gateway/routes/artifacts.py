"""Getting bytes in and out of a project, without PostgreSQL ever holding one.

`docs/08` lists "upload artifact" under the lab user and the artifact API under
transport, and `docs/04` states the rule that governs both: artifact *bytes*
live in object storage and never in the database. So the two halves of an
artifact are served by two different stores, and the split is visible in the
routes here — everything that returns metadata reads PostgreSQL and never
touches S3, and everything that returns bytes reaches S3 and never reads the
bytes out of a row.

**The Gateway is the only door, and it is not a pass-through.** `docs/08`
prefers "streaming or presigned flow through Gateway-controlled
authorization", and of those two this is the first: the caller authenticates to
RAVEL, RAVEL checks their membership, and only then does anything reach the
bucket. A presigned URL would move the authorization decision to the storage
layer, where the only question it can answer is whether the signature is valid
— so a link that leaked would keep working, and revoking a membership would not
stop it. Membership is checked here, per request, like everywhere else.

**A version is immutable, so an upload is an append.** There is no route that
replaces an artifact's bytes. A re-run with corrected inputs becomes version 2
and version 1 stays readable, which is what makes a result cited in a decision
still mean what it meant when the decision was made. The store enforces it as
well — `ArtifactImmutableError` on a key that already exists — so this is the
second of two locks rather than the only one.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from ravel.gateway.deps import GatewayState, GatewayStateDep, PrincipalDep
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import ArtifactStore, ArtifactStoreError, S3ArtifactStore

router = APIRouter(prefix="/projects", tags=["artifacts"])

#: What one upload may weigh. A cap rather than a limit on the design: the
#: store hashes the whole object before it uploads it, so an unbounded upload is
#: an unbounded allocation in the Gateway's own process, and a member with a
#: curl command could take the gateway down for everybody. Well above any file
#: V0 research produces.
MAX_UPLOAD_BYTES = 256 * 1024 * 1024

#: Read back in slices rather than whole when serving a download, so a large
#: artifact does not have to fit in memory twice to be handed over.
CHUNK_BYTES = 1 << 20


def artifact_store(state: GatewayState) -> ArtifactStore:
    """The object store this deployment writes artifacts to.

    Public, and used by the laboratory upload route as well as by the artifact
    routes here: both doors put bytes in the same bucket, and a second copy of
    this would be a second place for the deployment's storage configuration to
    be read — which is how one door comes to write to a bucket the other cannot
    find.

    Raises:
        HTTPException: 503, and the message says which half is missing. A
            deployment with no bucket configured can still serve every route
            that reads metadata — which is most of them — and that is worth
            keeping, so an absent store is one route failing rather than the
            application refusing to start.
    """
    try:
        return S3ArtifactStore(state.settings)
    except Exception as refused:
        # The client library raises its own exception types and this route has
        # no business enumerating them: what it knows is that no store could be
        # built, and that is what the caller is told.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"this deployment has no artifact store: {refused}",
        ) from refused


def _content_disposition(filename: str) -> str:
    """A download header that cannot be talked into doing something else.

    The filename was chosen by whoever uploaded the artifact, so it is not a
    string this application may paste into a header unexamined. Quotes and
    control characters are stripped rather than escaped: a filename that needed
    escaping is a filename that is not worth being clever about.
    """
    safe = "".join(character for character in filename if character.isprintable())
    safe = safe.replace("\\", "_").replace('"', "_").replace("/", "_").strip()
    return f'attachment; filename="{safe or "artifact"}"'


# ── Metadata ────────────────────────────────────────────────────────────────


@router.get("/{project_id}/artifacts", summary="What this project has produced")
def list_artifacts(
    standing: PrincipalDep, state: GatewayStateDep
) -> list[dict[str, Any]]:
    """Every artifact's metadata, newest first.

    Reads PostgreSQL and only PostgreSQL, so it answers while the object store
    is down — which is when a person is most likely to be asking what a project
    produced.
    """
    with state.database.read_only() as session:
        artifacts = ArtifactRepository(session, standing.project_id).all()
    return [artifact.model_dump(mode="json") for artifact in artifacts]


@router.get("/{project_id}/artifacts/{artifact_id}", summary="One artifact and its versions")
def read_artifact(
    standing: PrincipalDep, state: GatewayStateDep, artifact_id: str
) -> dict[str, Any]:
    """The artifact, with every version that was ever written.

    Versions are included rather than linked because the versions *are* the
    artifact: an artifact with one row and three versions is three different
    things a decision may have referred to, and a reader who sees only the
    latest cannot tell which one they are looking at.
    """
    with state.database.read_only() as session:
        repository = ArtifactRepository(session, standing.project_id)
        artifact = repository.get(artifact_id=artifact_id)
        versions = repository.versions(artifact_id)
    return {
        "artifact": artifact.model_dump(mode="json"),
        "versions": [version.model_dump(mode="json") for version in versions],
    }


# ── Bytes in ────────────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/artifacts",
    status_code=status.HTTP_201_CREATED,
    summary="Upload bytes as a new artifact or a new version of one",
)
async def upload(
    standing: PrincipalDep,
    state: GatewayStateDep,
    request: Request,
    # Deliberately without `min_length=1`. A default is validated like any other
    # value, so requiring one character here would reject the empty default —
    # which is exactly what adding a version sends, and the version-append path
    # would answer 422 to every caller who did not re-send a name they had
    # already given. The emptiness is checked below instead, where it can be
    # told apart from the case that is allowed to be empty.
    name: Annotated[str, Query(max_length=200)] = "",
    filename: Annotated[str, Query(max_length=255)] = "",
    media_type: Annotated[str, Query(max_length=200)] = "",
    kind: Annotated[str, Query(max_length=100)] = "",
    node_id: Annotated[str | None, Query()] = None,
    artifact_id: Annotated[
        str | None,
        Query(description="Add a version to this artifact instead of creating one."),
    ] = None,
) -> dict[str, Any]:
    """Store the body and register it.

    Any member may upload, which is the point of the lab user's screen: the
    person who ran the experiment is the person holding the file, and requiring
    the owner to relay it would mean the owner uploading files they have never
    seen. What an upload may *mean* is not decided here — a file is an artifact
    because it was registered, and whether it satisfies a node's contract is a
    Review verdict and a completeness check, neither of which this route makes.

    Raises:
        HTTPException: 413 if the body is larger than this deployment accepts,
            read from `Content-Length` when it is sent and counted while
            reading when it is not. 400 if a new artifact is uploaded with no
            name. 503 if there is no object store.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {MAX_UPLOAD_BYTES} bytes",
        )
    if artifact_id is None and not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a new artifact needs a name; adding a version does not",
        )

    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        # The header may be absent — a chunked upload has no length to check —
        # so the size is counted as well as declared. Checking only the header
        # would make the cap advisory.
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {MAX_UPLOAD_BYTES} bytes",
        )

    store = artifact_store(state)
    try:
        with state.database.transaction() as session:
            repository = ArtifactRepository(session, standing.project_id, store)
            if artifact_id is None:
                artifact, version = repository.register(
                    name=name,
                    chunks=[body],
                    created_by=standing.user_id,
                    filename=filename,
                    media_type=media_type,
                    kind=kind,
                    node_id=node_id,
                )
            else:
                artifact = repository.get(artifact_id=artifact_id)
                version = repository.add_version(
                    artifact_id,
                    chunks=[body],
                    created_by=standing.user_id,
                    filename=filename,
                    media_type=media_type,
                    node_id=node_id,
                )
    except ArtifactStoreError as refused:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"the artifact store refused the upload: {refused}",
        ) from refused

    return {
        "artifact": artifact.model_dump(mode="json"),
        "version": version.model_dump(mode="json"),
    }


# ── Bytes out ───────────────────────────────────────────────────────────────


@router.get(
    "/{project_id}/artifacts/{artifact_id}/versions/{version}/content",
    summary="Download one version's bytes",
    response_class=StreamingResponse,
)
def download(
    standing: PrincipalDep,
    state: GatewayStateDep,
    artifact_id: str,
    version: int,
    request: Request,
) -> Response:
    """The bytes of one version, as stored.

    Addressed by version and not by artifact because "the artifact" is
    ambiguous the moment there is a second version, and a caller who cannot say
    which one they want cannot be given a reproducible answer.

    The content hash goes out as the `ETag`, and a client that sends it back as
    `If-None-Match` is told the bytes have not changed without transferring
    them. The hash is the only thing about an artifact that a decision record
    cites, so "has this changed since I read it" is the question a reader
    actually has.

    Raises:
        HTTPException: 404 if this project has no such version. 503 if there is
            no object store, which is the one thing metadata reads survive.
    """
    with state.database.read_only() as session:
        repository = ArtifactRepository(session, standing.project_id)
        repository.get(artifact_id=artifact_id)
        versions = repository.versions(artifact_id)
    chosen = next((candidate for candidate in versions if candidate.version == version), None)
    if chosen is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"artifact {artifact_id!r} has no version {version}",
        )

    etag = f'"{chosen.content_hash}"'
    if request.headers.get("if-none-match") in {etag, chosen.content_hash}:
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})

    store = artifact_store(state)
    try:
        # Opened here rather than inside the generator so that a store which
        # refuses is a 502 the caller can act on, rather than a 200 whose body
        # stops partway. A failure *during* the read is still the second thing,
        # and cannot be anything else — by then the status line has gone.
        stream = store.open(chosen.storage_key)
    except ArtifactStoreError as refused:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"the artifact store could not read this version: {refused}",
        ) from refused

    def slices() -> Iterator[bytes]:
        try:
            while block := stream.read(CHUNK_BYTES):
                yield block
        finally:
            stream.close()

    return StreamingResponse(
        slices(),
        media_type=chosen.media_type or "application/octet-stream",
        headers={
            "ETag": etag,
            "Content-Disposition": _content_disposition(chosen.filename),
            "X-RAVEL-Content-Hash": chosen.content_hash,
        },
    )


__all__ = ["CHUNK_BYTES", "MAX_UPLOAD_BYTES", "artifact_store", "router"]
