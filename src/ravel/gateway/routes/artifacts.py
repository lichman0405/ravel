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

import hashlib
import logging
import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Annotated, Any, BinaryIO, cast

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response, StreamingResponse

from ravel.domain.artifacts import unusable_filename_reason
from ravel.gateway.deps import GatewayState, GatewayStateDep, PrincipalDep
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import (
    HASH_ALGORITHM,
    ArtifactImmutableError,
    ArtifactStore,
    ArtifactStoreError,
    S3ArtifactStore,
    StoredObject,
    _is_precondition_failure,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["artifacts"])

#: What one upload may weigh by default. The effective cap is
#: `RAVEL_GATEWAY_UPLOAD_LIMIT_BYTES`, which the composition root validates as a
#: positive number; this constant is the shipped default and the value the
#: laboratory upload route imports. Well above any file V0 research produces,
#: and low enough that a member with a curl command cannot fill the host's disk
#: with one request.
MAX_UPLOAD_BYTES = 64 * 1024 * 1024

#: How much of an upload the streaming S3 store holds in memory before it spills
#: to a temporary file. Every byte still passes through the hash, but no byte
#: beyond this is retained, so the Gateway's memory does not grow with the file.
UPLOAD_MEMORY_BYTES = 1 << 20

#: Read back in slices rather than whole when serving a download, so a large
#: artifact does not have to fit in memory twice to be handed over.
CHUNK_BYTES = 1 << 20


class UploadTooLarge(RuntimeError):
    """A body exceeded the configured upload cap while it was being read.

    A distinct type rather than a store error: the size decision belongs to the
    request boundary, and it has to be made before a store is asked to keep
    anything. The route translates it to 413.
    """

    def __init__(self, limit: int) -> None:
        super().__init__(f"the upload exceeds the {limit}-byte limit")
        self.limit = limit


class StreamingS3ArtifactStore(S3ArtifactStore):
    """An S3 store that uploads a spooled body instead of a copied one.

    `S3ArtifactStore.put` hashes the whole object into a `bytes` before it
    uploads, which makes an upload cost as much process memory as the file is
    large. The upload boundary is capped, so that is bounded rather than
    unbounded, but the cap is still a promise the process makes about its own
    footprint. This subclass keeps the same single-shot conditional write and
    the same content-addressed hash, and replaces only where the bytes wait:
    they are written to a `SpooledTemporaryFile` as they are hashed, and the
    file object itself is handed to `put_object`.

    The spool is memory up to `UPLOAD_MEMORY_BYTES` and a temporary file past
    that, so peak memory is a constant rather than a multiple of the artifact.
    It is closed in every case, including a refused upload, which is what keeps
    a failed request from leaving temporary data behind. The route that calls
    this has already bounded the body; the order — hash while writing, upload
    only after the hash is complete — is what makes the recorded hash describe
    exactly the bytes that were sent.
    """

    def __init__(
        self,
        settings: Any | None = None,
        *,
        client: Any | None = None,
        bucket: str | None = None,
        memory_bytes: int = UPLOAD_MEMORY_BYTES,
    ) -> None:
        super().__init__(settings, client=client, bucket=bucket)
        self._memory_bytes = memory_bytes

    def put(self, key: str, chunks: Iterable[bytes], *, media_type: str = "") -> StoredObject:
        """Hash the stream into a spool, then upload the spool.

        The error text names the failure type and not the client library's
        message: an S3 exception can carry an endpoint, a bucket, or a request
        that a caller has no business reading, and the route above turns a
        refusal into a fixed sentence in any case.

        Raises:
            ArtifactImmutableError: An object already exists at this key.
            ArtifactStoreError: The object store refused the upload.
        """
        digest = hashlib.new(HASH_ALGORITHM)
        size = 0
        with tempfile.SpooledTemporaryFile(
            max_size=self._memory_bytes, mode="w+b", prefix="ravel-upload-"
        ) as spool:
            for chunk in chunks:
                digest.update(chunk)
                size += len(chunk)
                spool.write(chunk)
            spool.seek(0)
            extra = {"ContentType": media_type} if media_type else {}
            try:
                self._client.put_object(
                    Bucket=self._bucket,
                    Key=key,
                    Body=spool,
                    **extra,
                    **{"IfNoneMatch": "*"},
                )
            except Exception as error:
                if _is_precondition_failure(error):
                    raise ArtifactImmutableError(
                        f"{key} already exists in {self._bucket}; artifact versions are "
                        "immutable, so a changed file needs a new version number"
                    ) from error
                raise ArtifactStoreError(
                    f"could not store {key}: {type(error).__name__}"
                ) from error
        return StoredObject(
            key=key,
            content_hash=f"{HASH_ALGORITHM}:{digest.hexdigest()}",
            size_bytes=size,
        )


def artifact_store(state: GatewayState) -> ArtifactStore:
    """The object store this deployment writes artifacts to.

    Public, and used by the laboratory upload route as well as by the artifact
    routes here: both doors put bytes in the same bucket, and a second copy of
    this would be a second place for the deployment's storage configuration to
    be read — which is how one door comes to write to a bucket the other cannot
    find.

    The returned store is the streaming one. Callers pass an iterable of chunks
    and the store never needs the whole body at once, which matters most for
    the route that has just read it off the network.

    Raises:
        HTTPException: 503. The exception's own text is logged rather than
            returned: it can name an endpoint or a credential, and a client
            that can do nothing about either should not receive it.
    """
    try:
        return StreamingS3ArtifactStore(state.settings)
    except Exception as refused:
        # The client library raises its own exception types and this route has
        # no business enumerating them: what it knows is that no store could be
        # built, and that is what the caller is told.
        logger.warning(
            "artifact store unavailable for project traffic: %s", type(refused).__name__
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="this deployment has no artifact store configured",
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

    **The body is never held whole.** It is read in the chunks Starlette
    yields, counted against the cap as it arrives, and written to a temporary
    file. Only after the last byte is in is the file handed to the repository,
    whose store reads it in slices while it hashes and uploads. The previous
    shape called `request.body()`, which made one upload cost 64 MiB of process
    memory and let a member with a loop take the Gateway down for everyone.
    The temporary file is removed in every path, including a refusal, so a
    failed request leaves neither bytes on disk nor a formal Artifact row —
    the store is written and refused before `register` stages anything.

    Raises:
        HTTPException: 413 if the body is larger than this deployment accepts,
            read from `Content-Length` when it is sent and counted while
            reading when it is not. 400 if a new artifact is uploaded with no
            name, or if the supplied filename could not be a storage-key
            component. 502 if the object store refuses the upload, with a fixed
            message that carries no store internals. 503 if there is no object
            store.
    """
    limit = state.settings.gateway_upload_limit_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {limit} bytes",
        )
    if artifact_id is None and not name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="a new artifact needs a name; adding a version does not",
        )
    if filename:
        # The name becomes a component of an object-storage key. An empty one is
        # correct — the repository derives it from the artifact's name — but a
        # supplied one that names a directory or carries a separator would
        # either escape the prefix or become a 500 from `artifact_key` long
        # after the body had been accepted. Refused here, before the body is
        # read, and with the submitted value left out of the answer.
        problem = unusable_filename_reason(filename)
        if problem is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"the filename is not usable: {problem}",
            )

    # Resolved before the body is read so a deployment with no store answers
    # immediately rather than after accepting an upload it cannot keep.
    store = artifact_store(state)

    # Not a context manager: the file has to be closed before it is unlinked,
    # on every path, so both steps live in the `finally` below rather than
    # being split between a `with` exit and a later statement.
    temporary = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w+b", prefix="ravel-upload-", suffix=".body", delete=False
    )
    try:
        stream = request.stream()
        try:
            async for chunk in stream:
                if not chunk:
                    continue
                if temporary.tell() + len(chunk) > limit:
                    # The header may be absent — a chunked upload has no length
                    # to check — so the size is counted as well as declared.
                    # Checking only the header would make the cap advisory.
                    raise UploadTooLarge(limit)
                temporary.write(chunk)
        finally:
            # `async for` closes a generator only when the loop runs to
            # completion. A body that was refused partway would otherwise leave
            # the request's receive generator suspended, so it is closed here
            # on both the refused and the completed path.
            await stream.aclose()
        temporary.flush()
        temporary.seek(0)

        try:
            with state.database.transaction() as session:
                repository = ArtifactRepository(session, standing.project_id, store)
                chunks = _stored_chunks(cast(BinaryIO, temporary))
                if artifact_id is None:
                    artifact, version = repository.register(
                        name=name,
                        chunks=chunks,
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
                        chunks=chunks,
                        created_by=standing.user_id,
                        filename=filename,
                        media_type=media_type,
                        node_id=node_id,
                    )
        except ArtifactStoreError as refused:
            # The store's own text is not returned. It can contain a key, an
            # endpoint, or a request identifier; the caller can act on none of
            # them, and a fixed sentence cannot leak a credential into a log
            # aggregator or a bug report.
            logger.warning(
                "artifact upload to project %s failed in the store: %s",
                standing.project_id,
                type(refused).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="the artifact store refused the upload",
            ) from refused
    except UploadTooLarge as too_big:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {too_big.limit} bytes",
        ) from too_big
    finally:
        temporary.close()
        # `delete=False` plus an explicit unlink rather than `delete=True`:
        # the file has to stay readable while the store consumes it, and the
        # removal has to happen on the same path that closed it, including the
        # 413 path where the upload was refused before anything read it.
        Path(temporary.name).unlink(missing_ok=True)

    return {
        "artifact": artifact.model_dump(mode="json"),
        "version": version.model_dump(mode="json"),
    }


def _stored_chunks(spool: BinaryIO, *, chunk_bytes: int = CHUNK_BYTES) -> Iterator[bytes]:
    """The temporary file as an iterable of bounded slices.

    The repository hands this to the store, which hashes as it reads. Reading
    in fixed slices is what keeps the second half of the upload — hashing and
    the S3 write — from allocating the object too.
    """
    while block := spool.read(chunk_bytes):
        yield block


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


__all__ = [
    "CHUNK_BYTES",
    "MAX_UPLOAD_BYTES",
    "UPLOAD_MEMORY_BYTES",
    "StreamingS3ArtifactStore",
    "UploadTooLarge",
    "artifact_store",
    "router",
]
