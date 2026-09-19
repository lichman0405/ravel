"""Mock output is marked as mock, and the mark is enforced rather than trusted.

`acceptance/V0_ACCEPTANCE.md` lists "mock compute/lab data must be explicitly
marked simulated" among the gates that cannot be skipped, and
`docs/06_EXECUTION_AND_REVIEW.md` §6 makes the rule absolute: mock output never
enters the Evidence Ledger as real scientific evidence.

Two claims are tested here, and they are different claims:

1. **Every artifact a mock produces carries the marker.** Asserted against the
   stored row *and* against the bytes in MinIO, because a marker that only
   exists in the database would be lost the moment somebody read the file.
2. **The ledger refuses a source that rests on one.** `EvidenceSourceRepository`
   is the single place a source is written, and the refusal is a guard there
   rather than a convention everywhere else — a convention is enforced by
   whoever remembers it.

The second test is the one that matters. The first could be satisfied by a
comment; the second is what makes the difference between a marked file and a
file that cannot become evidence.
"""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import select

from ravel.domain.artifacts import SIMULATED_KIND, is_simulated, simulated_provenance
from ravel.domain.clock import utcnow
from ravel.domain.enums import AccessStatus, NodeType
from ravel.domain.evidence import EvidenceSource
from ravel.state.database import Database
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceSourceRepository,
    SimulatedEvidenceError,
)
from ravel.state.store import S3ArtifactStore
from ravel.state.tables import ArtifactRow

pytestmark = pytest.mark.integration

SIMULATED_ARTIFACTS = (
    ("mock-compute", NodeType.COMPUTATION, "COMPUTE_SUCCESS"),
    ("mock-lab", NodeType.EXPERIMENT, "LAB_SUCCESS"),
)


@pytest.mark.parametrize(("backend_name", "node_type", "scenario_id"), SIMULATED_ARTIFACTS)
def test_every_artifact_a_mock_produces_is_marked_simulated(
    database: Database,
    artifact_store: S3ArtifactStore,
    prepare,
    compute,
    lab,
    mock_clock,
    backend_name: str,
    node_type: NodeType,
    scenario_id: str,
) -> None:
    node = prepare(node_type=node_type, required_outputs=("conductivity.csv", "notes.json"))
    backend = (
        compute(scenario_id) if node_type is NodeType.COMPUTATION else lab(scenario_id)
    )

    handle = backend.submit(node.request())
    for _ in range(20):
        if backend.status(handle.backend_job_ref).state.is_terminal:
            break
        mock_clock.advance(1.0)
    outputs = backend.collect(handle.backend_job_ref)
    assert outputs.artifacts, f"{scenario_id} produced nothing to mark"

    with database.read_only() as session:
        rows = list(
            session.execute(
                select(ArtifactRow).where(ArtifactRow.artifact_id.in_(outputs.artifacts))
            ).scalars()
        )

    assert len(rows) == len(outputs.artifacts)
    for row in rows:
        assert is_simulated(row.kind), (
            f"artifact {row.name!r} came out of {backend_name} with kind "
            f"{row.kind!r}; simulated output that is not marked is "
            "indistinguishable from a measurement"
        )
        assert row.provenance == simulated_provenance(backend_name, scenario_id), (
            "the marker has to say which backend and which scenario, or a "
            "reader cannot find out what was simulated"
        )
        assert backend_name in row.created_by


def test_the_bytes_themselves_say_they_are_not_a_measurement(
    database: Database, artifact_store: S3ArtifactStore, prepare, compute, mock_clock
) -> None:
    """A file that leaves the database keeps the warning with it.

    The marker column is a fact about a row; the file may be downloaded,
    copied, or opened by somebody who never sees the row.
    """
    node = prepare(required_outputs=("conductivity.csv",))
    backend = compute("COMPUTE_SUCCESS")
    handle = backend.submit(node.request())
    for _ in range(20):
        if backend.status(handle.backend_job_ref).state.is_terminal:
            break
        mock_clock.advance(1.0)
    outputs = backend.collect(handle.backend_job_ref)

    with database.read_only() as session:
        repository = ArtifactRepository(session, node.contract.project_id, artifact_store)
        version = repository.versions(outputs.artifacts[0])[-1]
        stored = repository.read(version)

    assert stored.startswith(b"simulated,not a measurement")
    assert version.content_hash == "sha256:" + hashlib.sha256(stored).hexdigest()


def test_the_ledger_refuses_a_source_resting_on_simulated_bytes(
    database: Database, artifact_store: S3ArtifactStore, prepare, compute, mock_clock
) -> None:
    """The rule, enforced where a source is written.

    A Worker that ran a mock and then cited its output as evidence would be
    fabricating a finding, and this is the call that stops it — not a review of
    the code, and not the Worker's own restraint.
    """
    node = prepare(required_outputs=("conductivity.csv",))
    backend = compute("COMPUTE_SUCCESS")
    handle = backend.submit(node.request())
    for _ in range(20):
        if backend.status(handle.backend_job_ref).state.is_terminal:
            break
        mock_clock.advance(1.0)
    outputs = backend.collect(handle.backend_job_ref)

    with database.transaction() as session:
        sources = EvidenceSourceRepository(session, node.contract.project_id)
        source = EvidenceSource(
            project_id=node.contract.project_id,
            url="https://example.invalid/conductivity.csv",
            title="Conductivity series",
            access_status=AccessStatus.OK,
            retrieved_at=utcnow(),
            content_hash="sha256:" + "0" * 64,
            artifact_ref=outputs.artifacts[0],
        )
        with pytest.raises(SimulatedEvidenceError, match=SIMULATED_KIND):
            sources.record(source)


def test_the_ledger_accepts_a_source_that_rests_on_nothing_simulated(
    database: Database, artifact_store: S3ArtifactStore, prepare
) -> None:
    """The control. A guard that refused everything would pass the test above.

    The artifact here is registered by hand with no `kind`, which is what a
    real fetch would produce, and the source resting on it is stored.
    """
    node = prepare(required_outputs=("conductivity.csv",))
    with database.transaction() as session:
        artifact, _ = ArtifactRepository(
            session, node.contract.project_id, artifact_store
        ).register(
            name="measured.csv",
            chunks=[b"two theta, intensity\n0.1, 41.2\n"],
            created_by="research-worker",
            filename="measured.csv",
        )

    with database.transaction() as session:
        sources = EvidenceSourceRepository(session, node.contract.project_id)
        stored = sources.record(
            EvidenceSource(
                project_id=node.contract.project_id,
                url="https://example.invalid/measured.csv",
                title="A real measurement",
                access_status=AccessStatus.OK,
                retrieved_at=utcnow(),
                content_hash="sha256:" + "1" * 64,
                artifact_ref=artifact.artifact_id,
            )
        )

    assert stored.source_id
    with database.read_only() as session:
        assert EvidenceSourceRepository(session, node.contract.project_id).all()
