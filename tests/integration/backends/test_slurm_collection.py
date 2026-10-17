"""Bringing a real cluster's output back into RAVEL.

`tests/unit/backends/slurm/` covers everything the backend does with a cluster
that needs no state: submitting, polling, cancelling, redaction. What it cannot
cover is `collect`, because collecting is the one call that *writes* — it files
what came back as artifacts, in a real PostgreSQL, into a real object store,
under a real project. A test of that against a double would prove that the
backend calls a method, which is not the interesting claim.

So this file runs the real backend against the scripted transport and a real
database, and asserts the things a double could not: that the bytes in the store
are the bytes the cluster held, that the artifact is attached to the node the
run was for and to the project the run's own submission record named, and that
collecting twice files once.

**The bytes are real and the cluster is not, and that distinction is recorded.**
An artifact collected here carries no `SIMULATED_KIND` — it came off a machine
that ran the job — but it also carries no claim to be evidence. What it is, is
what a Slurm job produced; whether that is admissible is Review's judgement
against the acceptance criteria, and a backend that asserted its own result was
evidence would be deciding its own case.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from tests.integration.conftest import DEFAULT_OUTPUTS, Prepared
from tests.support.slurm import PASSWORD, Connections, ScriptedCluster

from ravel.backends.slurm import SlurmComputeBackend, SlurmConfigurationError, SlurmTarget
from ravel.domain.artifacts import SIMULATED_KIND, Artifact, ArtifactVersion
from ravel.execution.backends import JobHandle, JobRequest
from ravel.state.database import Database
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import S3ArtifactStore, hash_chunks

CONDUCTIVITY, NOTES = DEFAULT_OUTPUTS


def a_manifest(required_outputs: tuple[str, ...]) -> bytes:
    """A preparation manifest, as the materializers write one."""
    return json.dumps(
        {"software": "RASPA", "method": "GCMC", "required_outputs": list(required_outputs)},
        indent=2,
    ).encode("utf-8")


@pytest.fixture
def cluster() -> ScriptedCluster:
    """A cluster with nothing on it."""
    return ScriptedCluster()


@pytest.fixture
def connections(cluster: ScriptedCluster) -> Connections:
    """A factory over the scripted cluster that records what it opens."""
    return Connections(cluster=cluster)


def write_workspace(root: Path, required_outputs: tuple[str, ...]) -> Path:
    """A prepared workspace on this machine, as preparation leaves one.

    The three files the target chain names: the software's input, the job script
    that starts it, and the manifest that says what the run owes. Written by the
    test rather than by preparation because preparation is `tests/integration/
    preparation/`'s subject, and this file is about what a backend does with a
    workspace that already exists.
    """
    root.mkdir(parents=True, exist_ok=True)
    files = {
        "simulation.input": b"FrameworkName alumina\n",
        "job.slurm": (
            b"#!/bin/bash\n#SBATCH --job-name=ravel-abc\n"
            b"exec simulation simulation.input\n"
        ),
        "calculation_manifest.json": a_manifest(required_outputs),
    }
    for name, body in files.items():
        (root / name).write_bytes(body)
    return root


def a_backend(
    connections: Connections, database: Database, store: S3ArtifactStore
) -> SlurmComputeBackend:
    """The real backend, pointed at the scripted cluster and real storage."""
    return SlurmComputeBackend(
        target=SlurmTarget(host="cluster.example.org", username="ravel", password=PASSWORD),
        connect=connections,
        database=database,
        store=store,
    )


@dataclass(frozen=True)
class Run:
    """A job this file has submitted, and everything needed to judge it."""

    prepared: Prepared
    backend: SlurmComputeBackend
    handle: JobHandle
    cluster: ScriptedCluster
    workspace: Path

    def finish(self, payload: dict[str, bytes]) -> None:
        """End the job on the cluster with these files where it ran.

        The files land in the run's remote directory, which is where `collect`
        looks for them, and the job moves to a terminal state — the two things
        that are true of a finished run and are what collection reads.
        """
        remote = next(job["dir"] for job in self.cluster.jobs.values())
        for name, body in payload.items():
            self.cluster.files[f"{remote}/{name}"] = body
        for job in self.cluster.jobs.values():
            job["state"] = "COMPLETED"
            job["exit"] = "0:0"
            job["batch_exit"] = "0:0"

    def rewrite(self, name: str, body: bytes) -> None:
        """Change a file in the run's remote directory after the job ended."""
        remote = next(job["dir"] for job in self.cluster.jobs.values())
        self.cluster.files[f"{remote}/{name}"] = body

    def versions(self, artifact_id: str, database: Database) -> list[ArtifactVersion]:
        with database.transaction() as session:
            return ArtifactRepository(
                session, self.prepared.project_id, self.backend.store
            ).versions(artifact_id)

    def read(self, artifact_id: str, version: int, database: Database) -> bytes:
        """The bytes RAVEL holds for one version, read back the way a reader would."""
        with database.transaction() as session:
            repository = ArtifactRepository(
                session, self.prepared.project_id, self.backend.store
            )
            found = next(
                item
                for item in repository.versions(artifact_id)
                if item.version == version
            )
            return repository.read(found)

    def artifact(self, artifact_id: str, database: Database) -> Artifact:
        with database.transaction() as session:
            return ArtifactRepository(
                session, self.prepared.project_id, self.backend.store
            ).get(artifact_id=artifact_id)


@pytest.fixture
def submitted(
    connections: Connections,
    cluster: ScriptedCluster,
    database: Database,
    artifact_store: S3ArtifactStore,
    prepare: Callable[..., Prepared],
    tmp_path: Path,
) -> Callable[..., Run]:
    """Submit a job for a READY node, through the real backend.

    The node's contract and the workspace's manifest name the same outputs,
    because that is what preparation guarantees: the manifest is written from
    the contract, so a test that let the two disagree would be collecting a run
    RAVEL cannot produce.
    """

    def build(
        *, required_outputs: tuple[str, ...] = DEFAULT_OUTPUTS, **node_options: object
    ) -> Run:
        prepared = prepare(required_outputs=required_outputs, **node_options)
        workspace = write_workspace(tmp_path / "run", required_outputs)
        backend = a_backend(connections, database, artifact_store)
        request: JobRequest = replace(
            prepared.request(), workspace_path=str(workspace), entrypoint="job.slurm"
        )
        return Run(
            prepared=prepared,
            backend=backend,
            handle=backend.submit(request),
            cluster=cluster,
            workspace=workspace,
        )

    return build


def test_a_real_output_is_filed_under_the_project_that_ran_it(
    submitted: Callable[..., Run],
    database: Database,
) -> None:
    """The project is read from the run's own submission record.

    `collect` is handed a reference and nothing else — the port says so — so the
    only description of the run available to it is the one RAVEL wrote into the
    directory the job ran in. Filing a result under a project the backend
    guessed at would be the worst kind of success.
    """
    run = submitted()
    run.finish({CONDUCTIVITY: b"sample,value\na,1.2\n", NOTES: b'{"a": 1}\n'})

    outputs = run.backend.collect(run.handle.backend_job_ref)

    assert outputs.delivered_outputs == DEFAULT_OUTPUTS
    assert len(outputs.artifacts) == 2
    artifact = run.artifact(outputs.artifacts[0], database)
    assert artifact.node_id == run.prepared.node_id
    assert artifact.name == CONDUCTIVITY
    assert artifact.provenance == f"slurm:{run.handle.backend_job_ref.split(':')[1]}"
    # A result off a real machine. Marking it simulated would be the same lie in
    # the other direction.
    assert artifact.kind != SIMULATED_KIND


def test_the_bytes_in_the_store_are_the_bytes_the_cluster_held(
    submitted: Callable[..., Run], database: Database
) -> None:
    """Including bytes that are not text.

    A trajectory is not a document, and a read that decoded it would store a
    file that hashes differently from the one on the cluster.
    """
    run = submitted()
    payload = bytes(range(256)) * 8
    run.finish({CONDUCTIVITY: payload, NOTES: b"{}\n"})

    outputs = run.backend.collect(run.handle.backend_job_ref)

    index = outputs.delivered_outputs.index(CONDUCTIVITY)
    assert run.read(outputs.artifacts[index], 1, database) == payload
    # And the record says what it stored, hashed the way RAVEL hashes — so a
    # reader can compare the artifact row against the cluster without trusting
    # the download that produced it.
    assert outputs.completion_metadata["collected"] == {
        CONDUCTIVITY: hash_chunks([payload])[0],
        NOTES: hash_chunks([b"{}\n"])[0],
    }


def test_collecting_twice_files_one_version(
    submitted: Callable[..., Run], database: Database
) -> None:
    """A retried activity must not multiply the record of one run.

    `collect` is an activity, and Temporal retries activities — a connection
    that dropped between the download and the insert is exactly the failure it
    exists to absorb. If each attempt filed its own artifact, a flaky network
    would leave a node that produced one file looking as though it produced
    five.
    """
    run = submitted()
    run.finish({CONDUCTIVITY: b"a,1\n", NOTES: b"{}\n"})

    first = run.backend.collect(run.handle.backend_job_ref)
    second = run.backend.collect(run.handle.backend_job_ref)

    assert first.artifacts == second.artifacts
    assert first.delivered_outputs == second.delivered_outputs
    for artifact_id in first.artifacts:
        assert len(run.versions(artifact_id, database)) == 1


def test_a_file_that_changed_between_reads_becomes_a_second_version(
    submitted: Callable[..., Run], database: Database
) -> None:
    """Both readings are kept, because both happened.

    A version is immutable, so the honest record of "this is what the directory
    held, and then this is what it held" is a second version of the same
    artifact — not an overwrite, and not a second artifact claiming the run
    produced two files.
    """
    run = submitted()
    run.finish({CONDUCTIVITY: b"a,1\n", NOTES: b"{}\n"})
    first = run.backend.collect(run.handle.backend_job_ref)
    run.rewrite(CONDUCTIVITY, b"a,2\n")

    second = run.backend.collect(run.handle.backend_job_ref)

    index = second.delivered_outputs.index(CONDUCTIVITY)
    artifact_id = second.artifacts[index]
    assert artifact_id == first.artifacts[first.delivered_outputs.index(CONDUCTIVITY)]
    versions = run.versions(artifact_id, database)
    assert [version.version for version in versions] == [1, 2]
    assert run.read(artifact_id, 1, database) == b"a,1\n"
    assert run.read(artifact_id, 2, database) == b"a,2\n"


def test_a_missing_required_output_is_reported_rather_than_raised(
    submitted: Callable[..., Run],
) -> None:
    """The run ended and produced what it produced.

    Raising would make an incomplete delivery an infrastructure fault and re-run
    a whole simulation over a file. RAVEL's completeness check is the layer that
    decides what a missing output means, and it can only decide if the backend
    reports it.
    """
    run = submitted()
    run.finish({CONDUCTIVITY: b"a,1\n"})

    outputs = run.backend.collect(run.handle.backend_job_ref)

    assert outputs.delivered_outputs == (CONDUCTIVITY,)
    assert outputs.completion_metadata["missing_outputs"] == [NOTES]


def test_an_output_the_contract_named_as_a_path_is_found_by_that_path(
    submitted: Callable[..., Run], database: Database
) -> None:
    """A run writes where its software decides to.

    `output/conductivity.csv` is the same required output as the bare name, and
    the path the manifest named is looked for first — so a run that wrote two
    files of one name resolves to the one the contract meant rather than to
    whichever the listing happened to reach first.
    """
    run = submitted(required_outputs=("output/conductivity.csv",))
    run.finish(
        {
            "output/conductivity.csv": b"the one the contract meant\n",
            "conductivity.csv": b"a different file of the same name\n",
        }
    )

    outputs = run.backend.collect(run.handle.backend_job_ref)

    assert outputs.delivered_outputs == ("output/conductivity.csv",)
    assert run.read(outputs.artifacts[0], 1, database) == b"the one the contract meant\n"
    # Stored under the basename, because the artifact's filename is the file's
    # name and its directory is the run's business rather than RAVEL's.
    assert run.artifact(outputs.artifacts[0], database).name == "output/conductivity.csv"


def test_a_run_directory_the_scheduler_forgot_collects_nothing_and_says_why(
    submitted: Callable[..., Run],
) -> None:
    """Accounting retention purges old jobs, records included.

    The alternative to a note is an exception or a fabricated empty result, and
    both are worse than a sentence naming what could not be found.
    """
    run = submitted()
    run.finish({CONDUCTIVITY: b"a,1\n", NOTES: b"{}\n"})
    run.cluster.jobs.clear()
    run.cluster.files = {
        path: body
        for path, body in run.cluster.files.items()
        if not path.endswith("submission.json")
    }

    outputs = run.backend.collect(run.handle.backend_job_ref)

    assert outputs.artifacts == ()
    assert "could not be located" in str(outputs.completion_metadata["note"])


def test_collecting_without_an_object_store_is_refused(
    connections: Connections, database: Database
) -> None:
    """A backend with nowhere to put a result has lost it.

    Raised rather than returning the bytes in memory: a result that exists only
    in a completed activity's return value is a result RAVEL does not have.
    """
    backend = SlurmComputeBackend(
        target=SlurmTarget(host="cluster.example.org", username="ravel"),
        connect=connections,
        database=database,
    )
    with pytest.raises(SlurmConfigurationError) as refused:
        backend.collect(f"slurm:7001:{'0' * 16}")
    assert "object store" in str(refused.value)


def test_the_credential_is_not_in_what_collection_records(
    submitted: Callable[..., Run], cluster: ScriptedCluster
) -> None:
    """The cluster echoes the password on every command.

    A login wrapper that prints its banner to stderr is ordinary, and
    `completion_metadata` is written into an Execution Record — which outlives
    the process that held the secret.
    """
    cluster.stderr_echo = f"\nwarning: authenticated with password {PASSWORD}\n"
    run = submitted()
    run.finish({CONDUCTIVITY: b"a,1\n", NOTES: b"{}\n"})

    outputs = run.backend.collect(run.handle.backend_job_ref)

    assert PASSWORD not in json.dumps(outputs.completion_metadata, default=str)
    assert PASSWORD not in json.dumps(outputs.logs, default=str)
