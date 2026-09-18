"""Artifact bytes in S3-compatible object storage.

PostgreSQL holds artifact *metadata*; the bytes live here. Three properties
make that split safe rather than merely convenient:

**Content-addressed.** Every write returns the SHA-256 of what it wrote, and
that hash goes into the `ArtifactVersion` row. A reader can therefore verify
that the bytes at a key are the bytes the record describes.

**Immutable.** `put` refuses to overwrite an existing key. Re-running a
computation produces a new version number and a new key; there is no code path
that replaces bytes in place. The refusal is a precondition check and would be
a race on its own — so the write is also conditional (`If-None-Match: *`),
which makes the object store itself reject a concurrent second write rather
than trusting the check.

**Streamed, not buffered.** Content is hashed and uploaded in chunks, so a
multi-gigabyte result does not have to fit in memory to be registered.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol

from ravel.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: Read size for hashing and uploading. Large enough to keep syscall overhead
#: negligible, small enough that memory stays flat regardless of object size.
CHUNK_SIZE = 1024 * 1024

HASH_ALGORITHM = "sha256"


class ArtifactStoreError(RuntimeError):
    """Raised when the object store refuses a write or cannot serve a read."""


class ArtifactImmutableError(ArtifactStoreError):
    """Raised when a write would replace bytes that are already stored."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """What a completed upload produced."""

    key: str
    content_hash: str
    size_bytes: int

    @property
    def digest(self) -> str:
        """The digest without its algorithm prefix."""
        return self.content_hash.split(":", 1)[-1]


class ArtifactStore(Protocol):
    """The object-storage surface RAVEL depends on.

    Small on purpose: a wider interface would be a wider thing to fake, and the
    fake would be the one used in tests.
    """

    def put(self, key: str, chunks: Iterable[bytes], *, media_type: str = "") -> StoredObject:
        """Store bytes under a key, refusing to overwrite."""
        ...

    def get(self, key: str) -> bytes:
        """Read an object's bytes."""
        ...

    def open(self, key: str) -> BinaryIO:
        """Open an object for streaming reads."""
        ...

    def exists(self, key: str) -> bool:
        """Whether an object is present."""
        ...

    def verify(self, key: str, expected_hash: str) -> bool:
        """Whether the bytes at a key still hash to what a record claims.

        Part of the interface rather than an extra on the S3 implementation:
        `ArtifactRepository.verify` calls it, so a store that omitted it would
        satisfy this Protocol and still break the repository at runtime.
        """
        ...

    def delete(self, key: str) -> None:
        """Remove an object. Only used by tests and explicit garbage collection."""
        ...


def hash_chunks(chunks: Iterable[bytes]) -> tuple[str, int]:
    """Hash a stream, returning `sha256:<hex>` and the byte count."""
    digest = hashlib.new(HASH_ALGORITHM)
    size = 0
    for chunk in chunks:
        digest.update(chunk)
        size += len(chunk)
    return f"{HASH_ALGORITHM}:{digest.hexdigest()}", size


class S3ArtifactStore:
    """An `ArtifactStore` backed by any S3-compatible service.

    MinIO in V0. The `endpoint_url` is explicit rather than inferred, because
    the AWS default would silently send research artifacts to a public service
    if the endpoint were ever left unset.
    """

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        client: Any | None = None,
        bucket: str | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._bucket = bucket or self._settings.s3_bucket
        self._client = client if client is not None else self._build_client()

    def _build_client(self) -> Any:
        import boto3
        from botocore.config import Config

        return boto3.client(
            "s3",
            endpoint_url=self._settings.s3_endpoint,
            aws_access_key_id=self._settings.s3_access_key,
            aws_secret_access_key=self._settings.s3_secret_key.get_secret_value(),
            region_name=self._settings.s3_region,
            config=Config(
                signature_version="s3v4",
                # MinIO serves path-style addressing; virtual-host style would
                # resolve `<bucket>.127.0.0.1`, which does not exist.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    @property
    def bucket(self) -> str:
        """The bucket artifacts are written to."""
        return self._bucket

    def ensure_bucket(self) -> None:
        """Create the bucket if it is absent. Idempotent."""
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except Exception:
            self._client.create_bucket(Bucket=self._bucket)
            logger.info("created artifact bucket %s", self._bucket)

    def put(self, key: str, chunks: Iterable[bytes], *, media_type: str = "") -> StoredObject:
        """Hash and store bytes, refusing to replace an existing key.

        The bytes are hashed before the upload so the hash describes exactly
        what was sent, and the upload is conditional so a concurrent writer
        cannot slip between the existence check and the write.

        Raises:
            ArtifactImmutableError: An object already exists at this key.
        """
        buffered, content_hash, size = _buffer_and_hash(chunks)
        extra = {"ContentType": media_type} if media_type else {}
        try:
            self._client.put_object(
                Bucket=self._bucket,
                Key=key,
                Body=buffered,
                **extra,
                **{"IfNoneMatch": "*"},
            )
        except Exception as error:
            if _is_precondition_failure(error):
                raise ArtifactImmutableError(
                    f"{key} already exists in {self._bucket}; artifact versions are "
                    "immutable, so a changed file needs a new version number"
                ) from error
            raise ArtifactStoreError(f"could not store {key}: {error}") from error
        return StoredObject(key=key, content_hash=content_hash, size_bytes=size)

    def get(self, key: str) -> bytes:
        """Read an object's bytes whole."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return bytes(response["Body"].read())
        except Exception as error:
            raise ArtifactStoreError(f"could not read {key}: {error}") from error

    def open(self, key: str) -> BinaryIO:
        """Open an object for streaming reads."""
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"]
        except Exception as error:
            raise ArtifactStoreError(f"could not open {key}: {error}") from error

    def exists(self, key: str) -> bool:
        """Whether an object is present."""
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
        except Exception:
            return False  # are both "not there" for this question
        return True

    def delete(self, key: str) -> None:
        """Remove an object.

        Not part of any research flow: RAVEL never removes a registered
        artifact. It exists for test cleanup and for an operator explicitly
        reclaiming space for a project that was cancelled.
        """
        self._client.delete_object(Bucket=self._bucket, Key=key)

    def verify(self, key: str, expected_hash: str) -> bool:
        """Whether the bytes at a key still hash to what the record claims."""
        actual, _ = hash_chunks(self.iter_chunks(key))
        return actual == expected_hash

    def iter_chunks(self, key: str) -> Iterator[bytes]:
        """Stream an object's bytes in chunks."""
        stream = self.open(key)
        try:
            while True:
                chunk = stream.read(CHUNK_SIZE)
                if not chunk:
                    return
                yield chunk
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()


def _buffer_and_hash(chunks: Iterable[bytes]) -> tuple[bytes, str, int]:
    """Hash a stream and keep it, for the single-shot `put_object` call.

    Returns the whole payload, which is why `put` is documented for the sizes
    RAVEL actually handles; `put_stream` covers the larger case.
    """
    digest = hashlib.new(HASH_ALGORITHM)
    parts: list[bytes] = []
    size = 0
    for chunk in chunks:
        digest.update(chunk)
        parts.append(chunk)
        size += len(chunk)
    return b"".join(parts), f"{HASH_ALGORITHM}:{digest.hexdigest()}", size


def _is_precondition_failure(error: Exception) -> bool:
    """Whether an S3 error means "the key is already taken".

    MinIO and AWS report this differently — a 412 from the conditional write, a
    409 from a concurrent create — so both are matched on the status code
    rather than on a message string that neither vendor guarantees.
    """
    response = getattr(error, "response", None)
    if not isinstance(response, dict):
        return False
    status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    code = response.get("Error", {}).get("Code", "")
    return status in {409, 412} or code in {
        "PreconditionFailed",
        "ConditionalRequestConflict",
        "BucketAlreadyOwnedByYou",
    }
