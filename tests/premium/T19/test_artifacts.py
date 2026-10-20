"""T19: sandbox output becomes evidence only through hash-checked registration.

A run can write bytes. That is not the same as writing an artifact row: the
sandbox has no database credential and no store credential. `collect` copies
the bytes out and hashes them, and registration hashes them again against a
store. This module observes both halves, and the tamper case changes the
supervisor's stored copy to show that the retrieval path notices.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from tests.premium.T19.support import (
    Deployment,
    MemoryArtifactStore,
    execute_command,
    python_argv,
)

from ravel.domain.enums import AccessStatus
from ravel.research.gateway import ResearchSourceGateway, SourceRefused, SourceRequest
from ravel.research.leads import Retrieval
from ravel.sandbox.models import SandboxForbidden, SandboxIntegrityError
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import hash_chunks
from ravel.state.tables import ArtifactRow

pytestmark = pytest.mark.integration

WRITE_ARTIFACT = r"""
from pathlib import Path

root = Path("/workspace/artifacts")
root.mkdir(parents=True, exist_ok=True)
(root / "result.csv").write_bytes(b"x,y\n1,2\n")
Path("/workspace/base.txt").write_bytes(b"base")
(root / "escape").symlink_to("/etc/passwd")
(root / "hard").hardlink_to("/workspace/base.txt")
print("written")
"""


def _artifact_count(database: Any) -> int:
    with database.read_only() as session:
        return int(
            session.execute(select(func.count()).select_from(ArtifactRow)).scalar_one()
        )


def test_sandbox_bytes_are_collected_hashed_and_registered_only_by_ravel(
    deployment: Deployment,
    database: Any,
    project: Any,
) -> None:
    """collect -> hash -> repository registration, with no row before it."""
    project_id = project.project_id
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    _, run = execute_command(
        deployment, handle, authority, python_argv(WRITE_ARTIFACT), timeout=30
    )
    status = provider.status(run)
    assert status.state.value == "SUCCEEDED", (status.detail, status.stderr)

    refs = provider.collect(run)
    assert [ref.name for ref in refs] == ["result.csv"], refs
    ref = refs[0]
    assert ref.content_hash.startswith("sha256:")
    assert ref.size_bytes == len(b"x,y\n1,2\n")

    # The listing reports a symlink as a link and a hard link as a regular
    # file; collection offers only the regular file with one link.
    entries = {
        entry.name: entry for entry in provider.files_list(handle, "artifacts")
    }
    assert entries["result.csv"].kind == "file"
    assert entries["escape"].kind == "symlink"
    assert entries["hard"].kind == "file"
    with pytest.raises(SandboxForbidden, match="hard link"):
        provider.files_read(handle, "artifacts/hard")
    with pytest.raises(SandboxForbidden, match="symbolic link"):
        provider.files_read(handle, "artifacts/escape")

    # The sandbox did not create an artifact row. The only rows are the ones a
    # registration writes.
    assert _artifact_count(database) == 0

    data, digest = provider.artifact_bytes(run, ref.name)
    assert data == b"x,y\n1,2\n"
    assert digest == ref.content_hash

    store = MemoryArtifactStore()
    with database.transaction() as session:
        artifact, version = ArtifactRepository(session, project_id, store=store).register(
            name="sandbox-result.csv",
            chunks=[data],
            created_by=project.created_by,
            filename=ref.name,
            media_type=ref.media_type,
            kind="sandbox_output",
            provenance=f"sandbox run {run.run_id}",
            execution_ref=run.run_id,
        )
    assert version.content_hash == ref.content_hash
    assert version.size_bytes == len(data)
    assert store.get(version.storage_key) == data
    assert _artifact_count(database) == 1
    with database.read_only() as session:
        stored_artifact = ArtifactRepository(session, project_id).get(
            artifact_id=artifact.artifact_id
        )
    assert stored_artifact.name == "sandbox-result.csv"

    # Changing the supervisor's copy is detected before it can be served as
    # the artifact that was collected.
    stored_path = (
        deployment.state_dir
        / "artifacts"
        / run.run_id
        / ref.content_hash.split(":", 1)[1]
    )
    assert stored_path.exists()
    stored_path.write_bytes(b"tampered-after-collection")
    with pytest.raises(SandboxIntegrityError):
        provider.artifact_bytes(run, ref.name)

    provider.destroy(handle, authority)


def test_a_retrieval_whose_hash_does_not_match_its_bytes_is_refused(
    deployment: Deployment,
    database: Any,
    project: Any,
    integration_settings: Any,
) -> None:
    """The Gateway recomputes the hash; it does not trust the retrieval's field."""
    project_id = project.project_id
    actual = b"the bytes that were actually read"
    claimed = hash_chunks([b"some other bytes"])[0]
    retrieval = Retrieval(
        requested_url="https://example.test/paper",
        final_url="https://example.test/paper",
        access_status=AccessStatus.OK,
        retrieved_at=datetime.now(UTC),
        status_code=200,
        media_type="text/plain",
        content_hash=claimed,
        size_bytes=len(actual),
        title="Mismatched retrieval",
        body=actual,
        excerpt="the bytes that were actually read",
    )
    store = MemoryArtifactStore()
    with database.transaction() as session:
        gateway = ResearchSourceGateway(
            session, project_id, settings=integration_settings, store=store
        )
        with pytest.raises(SourceRefused, match="hash"):
            gateway.register(
                SourceRequest.of("https://example.test/paper"),
                retrieval,
                actor_id=project.created_by,
            )
    assert _artifact_count(database) == 0
    assert store.objects == {}
