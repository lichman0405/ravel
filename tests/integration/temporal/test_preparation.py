"""A run that needs an environment, from the contract to the directory it runs in.

The unit tests say what the materializer writes. What can only be said here is
that a *run* is what reaches it and what happens to the project afterwards, and
there are three sentences this module exists to be able to say honestly:

1. **The workspace a job is handed is the one preparation built**, for the
   contract version the run is under — read out of the `JobRequest` the backend
   was actually given, because that is what a backend acts on.
2. **A refusal parks the node at `WAITING_DECISION` and submits nothing.** No
   job, no execution record, no second attempt: a contract that cannot be
   turned into a workspace is a question for Master, and a run that started
   anyway would be RAVEL answering it by itself.
3. **A contract that names no environment is unchanged.** Preparation is
   opt-in per contract, and a run whose contract names nothing has to behave
   exactly as it did before there was a preparation layer at all. This is the
   rule that keeps every earlier test in the suite meaningful.

The installation these tests point the materializer at is a fixture directory
of stand-in files, and the stand-ins are labelled as such where they are
written. They are here because what is under test is the *path* — contract,
inputs, files, hashes, manifest, job — and building a real RASPA installation
in a test would be testing something else. `tests/unit/preparation/test_raspa.py`
is where the file contents themselves are asserted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import text
from tests.integration.temporal.conftest import (
    FIRST_CONTRACT_VERSION,
    REQUIRED_OUTPUT,
    InMemoryStore,
    RunningWorker,
)

from ravel.domain.enums import JobState, NodeStatus, NodeType
from ravel.domain.preparation import PreparationRefusal
from ravel.execution.temporal.client import NodeRunClient
from ravel.preparation import (
    LabMaterializer,
    MaterializerRegistry,
    RaspaMaterializer,
)
from ravel.state.database import Database
from ravel.state.repositories.research import ArtifactRepository

pytestmark = [pytest.mark.integration, pytest.mark.e2e]

ACTOR = "compute-worker"

#: The structure file the contract names. The bytes are a stand-in — three
#: lines of a CIF header and a cell parameter — which is all the materializer
#: reads a structure file for here: it copies and hashes it.
FRAMEWORK_NAME = "framework.cif"
FRAMEWORK_BYTES = b"data_stand_in\n_cell_length_a 10.0\n_cell_length_b 10.0\n_cell_length_c 10.0\n"

#: Everything a RASPA input cannot be written without. Present in one place so
#: that a test about a *missing* term removes exactly one of them.
TERMS = {
    "temperature_k": "298.0",
    "pressure_bar": "1.0",
    "cycles": "10000",
    "initialization_cycles": "5000",
    "unit_cells": "2 2 2",
    "framework": FRAMEWORK_NAME,
    "molecule": "CO2",
}

REQUIREMENTS = {"software": "raspa"}

LIMITS = {"wall_clock_hours": "4", "cpus_per_task": "8"}


@pytest.fixture
async def client(execution_settings, temporal_unreachable) -> NodeRunClient:
    return await NodeRunClient.connect(execution_settings)


@pytest.fixture
def installation(tmp_path: Path) -> Path:
    """A RASPA data directory laid out the way a real installation is."""
    root = tmp_path / "share" / "raspa"
    (root / "forcefield").mkdir(parents=True)
    (root / "molecules").mkdir(parents=True)
    (root / "forcefield" / "force_field_mixing_rules.def").write_bytes(b"# stand-in mixing rules\n")
    (root / "forcefield" / "pseudo_atoms.def").write_bytes(b"# stand-in pseudo atoms\n")
    (root / "molecules" / "CO2.def").write_bytes(b"# stand-in CO2 definition\n")
    return root


@pytest.fixture
def materializers(installation: Path) -> MaterializerRegistry:
    """A deployment that can build a RASPA workspace, and only that."""
    registry = MaterializerRegistry()
    registry.register(RaspaMaterializer(data_dir=installation))
    return registry


@pytest.fixture
def lab_materializers() -> MaterializerRegistry:
    """A deployment that can assemble a bench package, and only that.

    Registered by hand rather than through `worker.materializers_for`, because
    what these tests are about is the path from the contract to the directory —
    and a test that used the deployment's own registry would be asserting that
    the deployment happens to register what it happens to register.
    """
    registry = MaterializerRegistry()
    registry.register(LabMaterializer())
    return registry


@pytest.fixture
def registered_framework(database: Database, project, store: InMemoryStore):
    """Put a structure file in the project, and return the bytes that went in.

    Registered through the real repository into the real database, because the
    resolution preparation does — *the newest version under this filename* — is
    a query over rows, and a fixture that handed the bytes to the materializer
    directly would skip the part that can be wrong.
    """
    with database.transaction() as session:
        artifact, version = ArtifactRepository(session, project.project_id, store).register(
            name=FRAMEWORK_NAME,
            chunks=[FRAMEWORK_BYTES],
            created_by="master",
            filename=FRAMEWORK_NAME,
            media_type="chemical/x-cif",
            node_id=None,
        )
    return artifact, version


def _preparations(database: Database, node_id: str) -> list[dict[str, object]]:
    """Every preparation RAVEL recorded for a node, oldest first."""
    with database.read_only() as session:
        rows = session.execute(
            text(
                "SELECT outcome, refusal, materializer, workspace_path, reason, "
                "execution_contract_version, manifest "
                "FROM execution_preparations WHERE node_id = :n "
                "ORDER BY created_at"
            ),
            {"n": node_id},
        ).mappings()
        return [dict(row) for row in rows]


def _node_status(database: Database, node_id: str) -> str:
    with database.read_only() as session:
        return session.execute(
            text("SELECT status FROM dag_nodes WHERE node_id = :n"), {"n": node_id}
        ).scalar_one()


def _counts(database: Database, node_id: str) -> tuple[int, int]:
    """How many jobs and execution records this node has."""
    with database.read_only() as session:
        jobs = session.execute(
            text("SELECT count(*) FROM backend_jobs WHERE node_id = :n"), {"n": node_id}
        ).scalar_one()
        records = session.execute(
            text("SELECT count(*) FROM execution_records WHERE node_id = :n"),
            {"n": node_id},
        ).scalar_one()
    return int(jobs), int(records)


# ── A contract that names an environment ────────────────────────────────────


async def test_a_run_that_needs_an_environment_runs_in_the_workspace_built_for_it(
    database: Database,
    project,
    runnable_node,
    backend,
    registry,
    client,
    materializers: MaterializerRegistry,
    store: InMemoryStore,
    registered_framework,
) -> None:
    """The whole path, once: contract → inputs → workspace → the job's request."""
    node = runnable_node(
        inputs=(FRAMEWORK_NAME,),
        execution_requirements=REQUIREMENTS,
        parameter_targets=TERMS,
        resource_limits=LIMITS,
    )
    backend.states = [JobState.RUNNING, JobState.COMPLETED]

    worker = await RunningWorker.start(
        client.settings, registry, database, materializers=materializers, store=store
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.completeness is not None
    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value

    (preparation,) = _preparations(database, node.node_id)
    assert preparation["outcome"] == "PREPARED"
    assert preparation["materializer"] == "raspa"
    workspace = Path(str(preparation["workspace_path"]))
    assert workspace.is_dir()

    # The job was handed the workspace preparation built, and the file that
    # starts the work in it — read out of the request the backend was given
    # rather than out of the record, because the request is what it acts on.
    (request,) = backend.jobs.values()
    assert request.workspace_path == str(workspace)
    assert request.entrypoint == "job.slurm"
    assert (workspace / request.entrypoint).is_file()

    # The contract's own bytes are in it, unaltered — the file the run reads is
    # the file the project holds, which is the only thing that makes a result
    # traceable to the structure it was computed from.
    assert (workspace / FRAMEWORK_NAME).read_bytes() == FRAMEWORK_BYTES
    assert (workspace / "simulation.input").is_file()
    assert (workspace / "force_field_mixing_rules.def").is_file()
    assert (workspace / "pseudo_atoms.def").is_file()
    assert (workspace / "CO2.def").is_file()

    # The manifest says which version of which artifact the framework came
    # from, so that the workspace can be told apart from one built from a
    # re-registered file.
    manifest = preparation["manifest"]
    assert isinstance(manifest, dict)
    (entry,) = manifest["inputs"]
    assert entry["name"] == FRAMEWORK_NAME
    assert entry["source"] == (
        f"{registered_framework[1].artifact_id}/v{registered_framework[1].version}"
    )
    written = json.loads((workspace / "calculation_manifest.json").read_text())
    assert written == manifest


async def test_a_contract_that_cannot_be_built_parks_the_node_and_starts_nothing(
    database: Database,
    project,
    runnable_node,
    backend,
    registry,
    client,
    store: InMemoryStore,
    registered_framework,
) -> None:
    """A host with no installation refuses, and RAVEL submits nothing.

    The registry holds the real materializer with no data directory, which is
    what a deployment that has RASPA code and no RASPA installed is. The refusal
    it produces is `ENVIRONMENT_UNAVAILABLE` — a machine to fix — rather than
    `UNSUPPORTED_ENVIRONMENT`, which would be a claim about RAVEL that is false.

    **The structure file is registered on purpose.** A materializer reads the
    contract before it reads the host, so a contract naming an input this
    project does not hold refuses as `INCONSISTENT_CONTRACT` — a defect in the
    terms, which would still be a defect once the host was fixed. Only a
    contract with nothing else wrong with it can reach the host check, which is
    what makes the class below a statement about the machine.
    """
    node = runnable_node(
        inputs=(FRAMEWORK_NAME,),
        execution_requirements=REQUIREMENTS,
        parameter_targets=TERMS,
        resource_limits=LIMITS,
    )
    registry_without_an_installation = MaterializerRegistry()
    registry_without_an_installation.register(RaspaMaterializer(data_dir=None))

    worker = await RunningWorker.start(
        client.settings,
        registry,
        database,
        materializers=registry_without_an_installation,
        store=store,
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    # The workflow reports the refusal as what ended the run, and there is no
    # execution behind it: a run that never happened is not a record with an
    # empty field in it.
    assert outcome.refusal is PreparationRefusal.ENVIRONMENT_UNAVAILABLE
    assert outcome.preparation_id is not None
    assert outcome.termination_status is None
    assert outcome.execution_id == ""

    assert _node_status(database, node.node_id) == NodeStatus.WAITING_DECISION.value
    assert _counts(database, node.node_id) == (0, 0)
    assert backend.submit_entries == 0

    (preparation,) = _preparations(database, node.node_id)
    assert preparation["outcome"] == "REFUSED"
    assert preparation["refusal"] == "ENVIRONMENT_UNAVAILABLE"
    assert "RAVEL_RASPA_DATA_DIR" in str(preparation["reason"])
    # Where the workspace would have been built, which is where a host fault is
    # found. The directory is made and left empty, so its *existence* says
    # nothing about whether anything was built — `outcome` is the field that
    # does, and `prepared_for_run` is the read that skips refusals so that no
    # job is ever handed one of these paths.
    workspace = Path(str(preparation["workspace_path"]))
    assert workspace == client.settings.runtime_path(
        "prepared", project.project_id, node.node_id, f"v{FIRST_CONTRACT_VERSION}"
    )
    assert list(workspace.iterdir()) == []


async def test_a_contract_naming_an_environment_nobody_can_build_refuses_it(
    database: Database,
    project,
    runnable_node,
    backend,
    registry,
    client,
    store: InMemoryStore,
) -> None:
    """An empty registry is a deployment that prepares nothing, and says so.

    This is the default runtime — no materializers registered at all — reached
    through the same activity a real run uses. It refuses with
    `UNSUPPORTED_ENVIRONMENT` and names what the deployment *can* build (here,
    nothing), which is a different sentence for Master than a broken host.
    """
    node = runnable_node(
        execution_requirements={"software": "vasp"},
        parameter_targets={"encut": "500"},
    )

    worker = await RunningWorker.start(client.settings, registry, database, store=store)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.refusal is PreparationRefusal.UNSUPPORTED_ENVIRONMENT
    assert _node_status(database, node.node_id) == NodeStatus.WAITING_DECISION.value
    assert _counts(database, node.node_id) == (0, 0)
    (preparation,) = _preparations(database, node.node_id)
    assert preparation["refusal"] == "UNSUPPORTED_ENVIRONMENT"
    assert "the environments it can prepare are none" in str(preparation["reason"])


async def test_the_same_run_prepares_one_workspace_however_often_it_is_read(
    database: Database,
    project,
    runnable_node,
    backend,
    registry,
    client,
    materializers: MaterializerRegistry,
    store: InMemoryStore,
    registered_framework,
) -> None:
    """Preparation is keyed to the terms, so a run has one workspace, not two.

    The activity is retryable — Temporal calls it again whenever its result was
    not recorded — and the node's own status is what stops a second workspace
    being built for the same contract version: the run is only prepared for
    while the node is being moved into RUNNING.
    """
    node = runnable_node(
        inputs=(FRAMEWORK_NAME,),
        execution_requirements=REQUIREMENTS,
        parameter_targets=TERMS,
        resource_limits=LIMITS,
    )
    backend.states = [JobState.RUNNING, JobState.COMPLETED]

    worker = await RunningWorker.start(
        client.settings, registry, database, materializers=materializers, store=store
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        await handle.result()
    finally:
        await worker.stop_gracefully()

    assert len(_preparations(database, node.node_id)) == 1


# ── A contract that names a bench ───────────────────────────────────────────


async def test_a_lab_run_is_handed_the_package_and_the_criteria_it_will_be_judged_by(
    database: Database,
    project,
    runnable_node,
    backend,
    registry,
    client,
    lab_materializers: MaterializerRegistry,
    store: InMemoryStore,
) -> None:
    """The bench path, and the one thing only an integration test can say.

    The criteria in the checklist are not the materializer's and not the test's:
    they are the *frozen acceptance contract bound to this node*, read out of
    the DAG by the activity and handed to preparation. A materializer that
    looked them up itself would be a second reader of authoritative state, free
    to prepare a package against criteria the delivery is not measured by, and
    nothing in a unit test can tell those two apart.
    """
    node = runnable_node(
        node_type=NodeType.EXPERIMENT,
        execution_requirements={"lab": "bench-chemistry"},
        procedure="Equilibrate each sample and record the conductivity.",
        parameter_targets={"temperature_c": "25"},
        resource_limits={"bench_hours": "6", "instrument": "TGA-2"},
        inputs=("batch-17 powder",),
    )
    backend.states = [JobState.RUNNING, JobState.COMPLETED]

    worker = await RunningWorker.start(
        client.settings,
        registry,
        database,
        materializers=lab_materializers,
        store=store,
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.completeness is not None
    (preparation,) = _preparations(database, node.node_id)
    assert preparation["materializer"] == "bench-chemistry"
    workspace = Path(str(preparation["workspace_path"]))

    # The package a person is handed, built where the run happens.
    assert (workspace / "experimental_protocol.md").is_file()
    assert (workspace / "data_collection_template.csv").is_file()
    assert (workspace / "sample_manifest.csv").is_file()

    # The criteria came from the node's own binding, and the checklist names it.
    checklist = json.loads((workspace / "acceptance_checklist.json").read_text())
    assert checklist["acceptance_contract_ref"] == node.acceptance_contract_ref
    (criterion,) = checklist["criteria"]
    assert criterion["statement"] == "Conductivity rises by at least 15%."
    assert criterion["provenance"] == "user_requirement"
    assert criterion["met"] == ""

    # And the run is started from the protocol, not from a command.
    (request,) = backend.jobs.values()
    assert request.entrypoint == "experimental_protocol.md"


async def test_a_lab_contract_that_states_no_procedure_parks_the_node(
    database: Database,
    project,
    runnable_node,
    backend,
    registry,
    client,
    lab_materializers: MaterializerRegistry,
    store: InMemoryStore,
) -> None:
    """A bench contract with a gap in it asks Master rather than a bench.

    The procedure is the one term a laboratory materializer cannot do without
    and cannot supply: writing one from the objective would be choosing a
    method, and no role has delegated that to software. The refusal has to
    reach the same place every other refusal does.
    """
    node = runnable_node(
        node_type=NodeType.EXPERIMENT,
        execution_requirements={"lab": "bench-chemistry"},
        parameter_targets={"temperature_c": "25"},
        inputs=("batch-17 powder",),
    )

    worker = await RunningWorker.start(
        client.settings,
        registry,
        database,
        materializers=lab_materializers,
        store=store,
    )
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert outcome.execution_id == ""
    assert _node_status(database, node.node_id) == NodeStatus.WAITING_DECISION.value
    assert _counts(database, node.node_id) == (0, 0)
    assert backend.submit_entries == 0

    (preparation,) = _preparations(database, node.node_id)
    assert preparation["refusal"] == "MISSING_SCIENTIFIC_PARAMETER"
    assert "states no procedure" in str(preparation["reason"])


# ── A contract that names no environment ────────────────────────────────────


async def test_a_contract_that_names_no_environment_runs_exactly_as_before(
    database: Database,
    project,
    runnable_node,
    backend,
    registry,
    client,
    store: InMemoryStore,
) -> None:
    """Preparation is opt-in, and this is what not opting in looks like.

    No workspace is built, nothing is recorded, and the job is handed no
    workspace — the backend is a scripted one that would happily accept an
    empty path, so what is asserted is the field rather than that no error was
    raised.
    """
    node = runnable_node()
    backend.states = [JobState.RUNNING, JobState.COMPLETED]

    worker = await RunningWorker.start(client.settings, registry, database, store=store)
    try:
        handle = await client.start_node_run(
            project_id=project.project_id,
            node_id=node.node_id,
            actor_id=ACTOR,
            execution_contract_version=FIRST_CONTRACT_VERSION,
        )
        outcome = await handle.result()
    finally:
        await worker.stop_gracefully()

    assert outcome.completeness is not None
    assert outcome.delivered_outputs == (REQUIRED_OUTPUT,)
    assert _node_status(database, node.node_id) == NodeStatus.REVIEWING.value
    assert _preparations(database, node.node_id) == []
    (request,) = backend.jobs.values()
    assert request.workspace_path == ""
    assert request.entrypoint == ""
