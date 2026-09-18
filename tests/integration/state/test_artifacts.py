"""Artifact bytes: content-addressed, immutable, and not in PostgreSQL.

The gate item has three parts, and each is asserted against the real object
store rather than a fake — a fake that returns whatever it was handed would
prove nothing about the conditional write that does the rejecting.

1. **Content-addressed.** The recorded hash describes the bytes that were
   actually stored, verified by reading them back and re-hashing.
2. **Immutable.** A key that already holds bytes refuses a second write, so a
   corrected file becomes a new version rather than silently replacing the old
   one that some node's result already cites.
3. **Out of the database.** No column anywhere in the schema holds artifact
   bytes. This is the "不把 Artifact binary 塞进 PostgreSQL" rule, written as a
   test so it cannot be undone by accident.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import LargeBinary, text
from sqlalchemy.exc import DatabaseError

from ravel.domain.events import ProjectEventType
from ravel.state.database import Database
from ravel.state.outbox import events_since
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import (
    ArtifactImmutableError,
    ArtifactStoreError,
    S3ArtifactStore,
    hash_chunks,
)
from ravel.state.tables import Base

pytestmark = pytest.mark.integration

#: Deliberately not valid UTF-8: a lab backend writes raw instrument output, and
#: a text round-trip would corrupt it.
BINARY = bytes(range(256)) * 4


def _repository(session, project_id: str, store: S3ArtifactStore) -> ArtifactRepository:
    return ArtifactRepository(session, project_id, store)


# ── Content addressing ──────────────────────────────────────────────────────


def test_the_recorded_hash_describes_the_bytes_that_were_stored(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    with database.transaction() as session:
        _, version = _repository(session, project.project_id, artifact_store).register(
            name="diffraction.csv", chunks=[b"two theta, intensity\n"], created_by="worker-1"
        )

    expected = "sha256:" + hashlib.sha256(b"two theta, intensity\n").hexdigest()
    assert version.content_hash == expected
    assert version.size_bytes == len(b"two theta, intensity\n")

    # The hash is a claim about the object in the store, so it is checked
    # against the object in the store.
    stored = artifact_store.get(version.storage_key)
    assert "sha256:" + hashlib.sha256(stored).hexdigest() == version.content_hash


def test_identical_bytes_produce_the_same_hash(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    """Two runs producing the same file are recognisably the same file."""
    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        _, first = repository.register(name="a.txt", chunks=[b"same"], created_by="w")
        _, second = repository.register(name="b.txt", chunks=[b"same"], created_by="w")

    assert first.content_hash == second.content_hash
    assert first.storage_key != second.storage_key, "distinct artifacts keep distinct objects"


def test_different_bytes_produce_a_different_hash(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        _, first = repository.register(name="a.txt", chunks=[b"before"], created_by="w")
        _, second = repository.register(name="b.txt", chunks=[b"after"], created_by="w")

    assert first.content_hash != second.content_hash


def test_a_chunked_upload_hashes_as_one_stream(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    """A large result is streamed, and streaming must not change the hash."""
    chunks = [BINARY[index : index + 97] for index in range(0, len(BINARY), 97)]
    assert len(chunks) > 1

    with database.transaction() as session:
        _, version = _repository(session, project.project_id, artifact_store).register(
            name="raw.bin", chunks=chunks, created_by="worker-1"
        )

    assert version.size_bytes == len(BINARY)
    assert version.content_hash == hash_chunks([BINARY])[0]
    assert artifact_store.get(version.storage_key) == BINARY


def test_binary_content_survives_the_round_trip(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        _, version = repository.register(name="raw.bin", chunks=[BINARY], created_by="w")
    with database.read_only() as session:
        repository = _repository(session, project.project_id, artifact_store)
        assert repository.read(version) == BINARY


# ── Immutability ────────────────────────────────────────────────────────────


def test_a_key_that_holds_bytes_refuses_a_second_write(
    artifact_store: S3ArtifactStore,
) -> None:
    """The gate: bytes already stored cannot be replaced."""
    key = "ravel/test/immutable/object.bin"
    artifact_store.delete(key)
    artifact_store.put(key, [b"the first result"])

    with pytest.raises(ArtifactImmutableError, match="immutable"):
        artifact_store.put(key, [b"a different result"])

    assert artifact_store.get(key) == b"the first result"


def test_a_refused_write_leaves_no_trace(artifact_store: S3ArtifactStore) -> None:
    """A rejected overwrite must not have written anything before failing."""
    key = "ravel/test/immutable/partial.bin"
    artifact_store.delete(key)
    original = b"original" * 1000
    artifact_store.put(key, [original])

    with pytest.raises(ArtifactImmutableError):
        artifact_store.put(key, [b"replacement" * 1000])

    assert artifact_store.get(key) == original
    assert hash_chunks([artifact_store.get(key)])[0] == hash_chunks([original])[0]


def test_verify_re_hashes_the_stored_bytes(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    """`verify` must read the object, not trust the row it was handed."""
    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        _, version = repository.register(name="raw.bin", chunks=[BINARY], created_by="w")
        assert repository.verify(version) is True

        # Corrupt the object behind the record's back. The only way to do that
        # is to delete and rewrite, because the store refuses to overwrite.
        artifact_store.delete(version.storage_key)
        artifact_store.put(version.storage_key, [b"tampered"])

        assert repository.verify(version) is False


def test_a_version_is_a_durable_record(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    """A recorded hash is history; it is not editable."""
    with database.transaction() as session:
        _, version = _repository(session, project.project_id, artifact_store).register(
            name="raw.bin", chunks=[BINARY], created_by="w"
        )

    with pytest.raises(DatabaseError, match="append-only"), database.transaction() as session:
        session.execute(
            text("UPDATE artifact_versions SET content_hash = 'sha256:0' WHERE version_id = :v"),
            {"v": version.version_id},
        )


# ── Versioning ──────────────────────────────────────────────────────────────


def test_a_corrected_result_becomes_a_new_version(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    """The old bytes stay exactly as the node that produced them left them."""
    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        artifact, first = repository.register(
            name="yield.csv", chunks=[b"yield\n0.41\n"], created_by="worker-1"
        )

    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        second = repository.add_version(
            artifact.artifact_id, chunks=[b"yield\n0.47\n"], created_by="worker-1"
        )

    assert first.version == 1
    assert second.version == 2
    assert first.storage_key != second.storage_key
    assert first.content_hash != second.content_hash

    with database.read_only() as session:
        repository = _repository(session, project.project_id, artifact_store)
        versions = repository.versions(artifact.artifact_id)
        assert [version.version for version in versions] == [1, 2]
        assert repository.read(versions[0]) == b"yield\n0.41\n"
        assert repository.read(versions[1]) == b"yield\n0.47\n"
        assert repository.latest_version_number(artifact.artifact_id) == 2


def test_reregistering_the_same_bytes_still_gets_a_new_key(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    """Version 2 of identical bytes is still a distinct object, not an overwrite."""
    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        artifact, first = repository.register(name="x.txt", chunks=[b"same"], created_by="w")
        second = repository.add_version(artifact.artifact_id, chunks=[b"same"], created_by="w")

    assert first.storage_key != second.storage_key
    assert first.content_hash == second.content_hash
    assert artifact_store.get(first.storage_key) == artifact_store.get(second.storage_key)


# ── Scoping and failure modes ───────────────────────────────────────────────


def test_another_project_cannot_read_these_bytes(
    database: Database, project, other_project, artifact_store: S3ArtifactStore
) -> None:
    with database.transaction() as session:
        _, version = _repository(session, project.project_id, artifact_store).register(
            name="raw.bin", chunks=[BINARY], created_by="w"
        )

    with pytest.raises(ArtifactStoreError, match="belongs to project"), (
        database.read_only()
    ) as session:
        _repository(session, other_project.project_id, artifact_store).read(version)


def test_a_record_whose_bytes_are_gone_reports_an_error(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    """Missing bytes are a fault, not an empty file."""
    with database.transaction() as session:
        repository = _repository(session, project.project_id, artifact_store)
        _, version = repository.register(name="raw.bin", chunks=[BINARY], created_by="w")
        artifact_store.delete(version.storage_key)

    with pytest.raises(ArtifactStoreError, match="could not read"), (
        database.read_only()
    ) as session:
        _repository(session, project.project_id, artifact_store).read(version)


def test_registering_bytes_emits_one_event(
    database: Database, project, artifact_store: S3ArtifactStore
) -> None:
    with database.transaction() as session:
        _, version = _repository(session, project.project_id, artifact_store).register(
            name="raw.bin", chunks=[BINARY], created_by="worker-1"
        )

    with database.read_only() as session:
        events = events_since(session, project.project_id)
    registered = [e for e in events if e.event_type is ProjectEventType.ARTIFACT_REGISTERED]
    assert len(registered) == 1
    assert registered[0].payload["content_hash"] == version.content_hash
    assert registered[0].payload["version"] == 1


# ── The architectural rule ──────────────────────────────────────────────────


def test_no_table_in_the_schema_holds_artifact_bytes() -> None:
    """Artifact binaries live in the object store, never in PostgreSQL."""
    binary_columns = [
        f"{table.name}.{column.name}"
        for table in Base.metadata.tables.values()
        for column in table.columns
        if isinstance(column.type, LargeBinary)
    ]
    assert binary_columns == [], (
        "artifact bytes belong in the object store; these columns would put them "
        f"in the database: {binary_columns}"
    )
