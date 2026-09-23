"""P11-05: the cluster backend, at the seat a deployment builds it in.

`tests/integration/backends/test_slurm_collection.py` and the unit suite say
what the backend does with a cluster's answers — nine collection cases and a
state vocabulary, all against a transport the test controls. What they cannot
say is what a *deployment* gets when it asks for one, and that is where this
item's two promises live.

**A worker told to use a cluster it was not given refuses to start.** The
alternative is the failure mode this item was written against: a worker that
comes up happily, takes work, and stalls on the first node that reaches the
queue — an hour later, on a node somebody was watching, with the reason in a
traceback rather than in front of the person who started the process. Refusing
at start-up is what makes a missing setting the operator's problem while they
are still at the terminal, and the sentence has to name the variable, because
the person who reads it is the person who can set it.

**The password reaches the worker process and nothing else.** It is the one
credential in RAVEL that is not RAVEL's own, and the two ways it would leak are
structural rather than accidental: into the environment a model session or a
tool server is launched with, and into a string the backend returns — which
becomes a PostgreSQL column, a progress fact, or a log line that outlives the
process that held the secret. Both are asserted here against a deployment that
really has a cluster configured, because `Settings()` in the unit suite has no
password in it at all: the rule is asserted there by *name*, and this is where
it is asserted by value.

The third claim is a gap rather than a result, and saying so is as much the
point of this module as the other two. **Whether a given cluster accepts
RAVEL's job script is a fact about that cluster** — whether the account may
submit, whether the filesystem RAVEL writes into is mounted, whether the
software the contract names is installed — and nothing in this repository can
establish it. The live case below is written and is part of this item's
acceptance, and it skips until somebody supplies an endpoint to point it at.
`acceptance/PHASE11_ACCEPTANCE.md` records that outcome as `BLOCKED_EXTERNAL`,
and the release gate reports the row as skipped rather than as certified, which
is the whole reason the two verdicts are different words.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable

import pytest
import scripts.run_temporal_worker as worker_script
from pydantic import SecretStr
from tests.integration.conftest import Prepared

from ravel.backends.slurm import (
    REDACTED,
    SlurmComputeBackend,
    SlurmConfigurationError,
)
from ravel.config import Settings
from ravel.domain.artifacts import SIMULATED_KIND, Artifact
from ravel.domain.enums import JobState, NodeType
from ravel.domain.preparation import PreparationOutcome
from ravel.domain.project import Project
from ravel.execution.backends import BackendRegistry, JobOutputs, JobStatus
from ravel.execution.temporal.activities import NodeRunActivities, _request
from ravel.execution.temporal.contracts import RunInput
from ravel.preparation import MaterializerRegistry, RaspaMaterializer
from ravel.state.database import Database
from ravel.state.repositories.preparations import PreparationRepository
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import S3ArtifactStore

pytestmark = [pytest.mark.phase11, pytest.mark.acceptance]

#: The role that runs a computation node. A string because that is what
#: `RunInput.actor_id` carries everywhere else.
ACTOR = "compute-worker"

#: A cluster and an account, named the way a deployment names them. Nothing
#: below dials the host except the live case, and that case does not run
#: without coordinates the environment supplies.
CLUSTER_HOST = "cluster.example.org"
CLUSTER_USER = "ravel"

#: The credential, as a *value* the case can search a string for. Deliberately
#: a phrase that appears nowhere else in this process: a leak check whose secret
#: is empty, or is a word the environment holds anyway, passes for the wrong
#: reason and would go on passing after the rule it checks had been removed.
PASSWORD = "correct-horse-battery-staple"

#: How long the live case waits for a real job, and how often it asks. Not a
#: scientific bound — the contract's own `resource_limits` are — but the point
#: past which a case that is still waiting is one that will not finish inside a
#: test run.
LIVE_WAIT_SECONDS = 1800.0
LIVE_POLL_SECONDS = 10.0

#: The structure the live contract names. A minimal CIF: preparation copies and
#: hashes it, and RASPA reads the cell parameters out of it, so what matters is
#: that the software can parse what is here.
FRAMEWORK_NAME = "framework.cif"
FRAMEWORK_BYTES = (
    b"data_live_certification\n"
    b"_cell_length_a 10.0\n_cell_length_b 10.0\n_cell_length_c 10.0\n"
    b"_cell_angle_alpha 90.0\n_cell_angle_beta 90.0\n_cell_angle_gamma 90.0\n"
    b"loop_\n_symmetry_equiv_pos_as_xyz\n'x, y, z'\n"
)

#: Everything a RASPA input cannot be written without. These are the
#: *contract's* terms rather than the case's: a materializer that filled any of
#: them in would be choosing a method, a pressure or a molecule that nobody
#: delegated to software.
RASPA_TERMS: dict[str, str] = {
    "temperature_k": "298.0",
    "pressure_bar": "1.0",
    "cycles": "10000",
    "initialization_cycles": "5000",
    "unit_cells": "2 2 2",
    "framework": FRAMEWORK_NAME,
    "molecule": "CO2",
}

RASPA_LIMITS = {"wall_clock_hours": "1", "cpus_per_task": "1"}

#: The one file RASPA writes for every simulation, under the output directory
#: it creates in the directory it was started from. The live case requires it,
#: which is what makes `collect`'s completeness check mean something: a job that
#: ran and wrote nothing is not a certification of anything.
RASPA_OUTPUT = "output/Output/Results/Output.txt"


def a_cluster(settings: Settings, **overrides: object) -> Settings:
    """The suite's settings with a cluster in them.

    Built from the test's settings rather than from `Settings()`, so the
    database and the task queue stay the test's and only the cluster is this
    case's. The alternative is a settings object that quietly reaches for the
    deployment's database the next time a name in `.env` changes.
    """
    coordinates: dict[str, object] = {
        "slurm_host": CLUSTER_HOST,
        "slurm_username": CLUSTER_USER,
        # A `SecretStr`, because that is what the field is: a deployment's
        # password arrives from the environment through pydantic, and a case
        # that handed the composition a bare string would be testing a settings
        # object no deployment can produce — the backend reads the secret with
        # `get_secret_value`, and a `str` has no such method.
        "slurm_password": SecretStr(PASSWORD),
    }
    coordinates.update(overrides)
    return settings.model_copy(update=coordinates)


def a_slurm_worker(settings: Settings, database: Database) -> BackendRegistry:
    """The registry the worker process builds for `--compute-backend slurm`."""
    return worker_script.build_registry(
        settings, database, worker_script.parse_args(["--compute-backend", "slurm"])
    )


def the_compute_backend(registry: BackendRegistry) -> SlurmComputeBackend:
    """The cluster backend out of a registry, or a failure saying it is not there.

    Narrowing here rather than in each case, so that a registry which stopped
    holding a real backend fails with that sentence instead of an attribute
    error three lines later.
    """
    backend = registry.for_node_type(NodeType.COMPUTATION)
    assert isinstance(backend, SlurmComputeBackend), (
        f"a deployment that named a cluster got {type(backend).__name__} for its "
        "computation nodes"
    )
    return backend


# ── A worker told to use a cluster it was not given ────────────────────────────


def test_p11_05_a_worker_told_to_use_a_cluster_it_was_not_given_refuses_to_start(
    integration_settings: Settings, database: Database
) -> None:
    """The refusal happens at start-up, and it says what to set.

    Both halves are the claim. A worker that started and stalled on its first
    node would be worse than one that refused, and one that refused with "no
    cluster configured" would leave the operator reading the settings file —
    the sentence has to name the variable.

    The last call is the control. With a host and an account the same function
    returns a registry holding a real backend, so what the first two assert is
    a configuration check rather than a function that cannot build one at all.
    """
    without_a_cluster = a_cluster(
        integration_settings, slurm_host=None, slurm_username=None, slurm_password=None
    )
    with pytest.raises(SlurmConfigurationError) as refused:
        a_slurm_worker(without_a_cluster, database)
    message = str(refused.value)
    assert "RAVEL_SLURM_HOST" in message and "RAVEL_SLURM_USERNAME" in message
    # Which of the two refusals this is, and the distinction is not pedantry:
    # the account's own sentence begins "RAVEL_SLURM_HOST is set but", so a
    # deployment with no cluster at all would be told its host is set. Both
    # sentences name both variables, so the check is on what the sentence
    # claims rather than on which names appear in it — and it is against the
    # wording production writes, which is the wording an operator reads.
    assert "needs a cluster" in message, (
        "a deployment with no cluster was refused as though it had one and was "
        "missing an account"
    )

    # A host with no account is the other half of the same configuration, and
    # it is a different sentence: there is a cluster, and no name to submit as.
    without_an_account = a_cluster(integration_settings, slurm_username=None)
    with pytest.raises(SlurmConfigurationError) as refused:
        a_slurm_worker(without_an_account, database)
    assert "RAVEL_SLURM_USERNAME" in str(refused.value)

    the_compute_backend(a_slurm_worker(a_cluster(integration_settings), database))


def test_p11_05_the_cluster_password_reaches_the_worker_and_nothing_else(
    integration_settings: Settings, database: Database
) -> None:
    """The credential is the launcher's, and the strings that leave are clean.

    Three places it could be and must not be, and one where it must:

    1. **A tool server's environment.** Every DSH session and every tool server
       RAVEL starts is launched with `Settings.tool_server_env()`, and the
       Compute Worker's session is a model — "the model never sees the cluster
       password" is a property of that mapping rather than of the model
       declining to look. Checked by value as well as by name: a rename that
       kept the secret travelling would pass a check on the prefix alone.
    2. **The start-up banner.** `_describe_compute` is printed to a terminal,
       written to a log, and collected by whatever reads logs. It names the host
       and the account, which are what an operator needs to recognise which
       cluster this is, and nothing more.
    3. **What the backend hands back.** A cluster whose login wrapper prints a
       banner to stderr is ordinary, and a `detail` line becomes a column in
       PostgreSQL. The redactor is built from the target's own secret rather
       than passed in at each call site, so this asserts the composition.

    And the password *is* in the worker. A backend that authenticated with
    nothing would satisfy all three checks above while being unable to submit
    anything, which is why the last assertion is the one that makes the others
    worth having.
    """
    settings = a_cluster(integration_settings)
    environment = settings.tool_server_env()

    assert PASSWORD not in environment.values(), (
        "the cluster password is in the environment a session is launched with"
    )
    assert not [name for name in environment if name.startswith("RAVEL_SLURM")], (
        "a cluster setting travels to a tool server, which has no cluster to reach"
    )
    # The control: the mapping is not simply empty of everything.
    assert "RAVEL_POSTGRES_DB" in environment

    args = worker_script.parse_args(["--compute-backend", "slurm"])
    banner = worker_script._describe_compute(settings, args)
    assert CLUSTER_HOST in banner and CLUSTER_USER in banner
    assert PASSWORD not in banner

    backend = the_compute_backend(a_slurm_worker(settings, database))
    assert backend.target.password == PASSWORD, (
        "the worker built a backend that cannot authenticate to the cluster"
    )
    assert backend.redact(f"authenticated with {PASSWORD}\n") == (
        f"authenticated with {REDACTED}\n"
    )
    assert backend.redact.mapping({"stderr": f"pw={PASSWORD}"}) == {
        "stderr": f"pw={REDACTED}"
    }


# ── The cluster itself, which nothing in this repository owns ──────────────────


def cluster_from_the_environment() -> Settings:
    """The deployment's own settings, or a skip that names what is missing.

    Read from `Settings()` rather than from the suite's fixture, because the
    cluster belongs to the environment rather than to the test: nobody's
    `ravel_test` database has a scheduler attached to it, and the coordinates a
    real run uses are the ones in the deployment's `.env`.

    Every missing name is listed rather than summarised as "no cluster", so a
    half-configured environment is visible as half-configured.

    Raises:
        pytest.skip: Nothing here can reach a cluster, or nothing here could
            build the workspace the contract needs.
    """
    settings = Settings()
    missing = [
        name
        for name, present in (
            ("RAVEL_SLURM_HOST", bool(settings.slurm_host)),
            ("RAVEL_SLURM_USERNAME", bool(settings.slurm_username)),
            (
                "RAVEL_SLURM_PASSWORD or RAVEL_SLURM_KEY_FILENAME",
                bool(settings.slurm_password or settings.slurm_key_filename),
            ),
            ("RAVEL_RASPA_DATA_DIR", bool(settings.raspa_data_dir)),
        )
        if not present
    ]
    if missing:
        message = (
            "no cluster is configured, so nothing here can be submitted: "
            + "; ".join(missing)
            + ". This is the live half of P11-05, and RAVEL records it as "
            "blocked rather than certified until somebody points it at a real "
            "endpoint."
        )
        if os.environ.get("RAVEL_REQUIRE_SLURM"):
            pytest.fail(message)
        pytest.skip(message)
    return settings


def a_registry_holding(backend: SlurmComputeBackend) -> BackendRegistry:
    """A registry holding the one backend this run uses."""
    registry = BackendRegistry()
    registry.register(NodeType.COMPUTATION, backend)
    return registry


def raspa_materializers(settings: Settings) -> MaterializerRegistry:
    """A deployment that can build a RASPA workspace, pointed at the installation."""
    registry = MaterializerRegistry()
    registry.register(RaspaMaterializer(data_dir=settings.raspa_data_dir))
    return registry


def a_framework_artifact(
    database: Database, store: S3ArtifactStore, project_id: str, body: bytes
) -> None:
    """Put a structure file in the project, the way Master would.

    Registered through the real repository, because what preparation resolves
    is *the newest version under this filename* — a query over rows — and a
    fixture that handed the bytes straight to the materializer would skip the
    part that can be wrong.
    """
    with database.transaction() as session:
        ArtifactRepository(session, project_id, store).register(
            name=FRAMEWORK_NAME,
            chunks=[body],
            created_by="master",
            filename=FRAMEWORK_NAME,
            media_type="chemical/x-cif",
            node_id=None,
        )


async def hand_to_the_cluster(
    *,
    settings: Settings,
    database: Database,
    store: S3ArtifactStore,
    backend: SlurmComputeBackend,
    prepared: Prepared,
) -> str:
    """Build the workspace and submit it, by the path a run really takes.

    `begin_node_run` and `prepare_execution` are the two activities a run
    calls, the request is built by the function the workflow builds it with,
    and the workspace is the one the materializer wrote. Handing the backend a
    directory a test had made up would certify the fixture.

    Returns:
        The backend's reference to the job it submitted.
    """
    activities = NodeRunActivities(
        settings=settings,
        database=database,
        registry=a_registry_holding(backend),
        materializers=raspa_materializers(settings),
        store=store,
    )
    plan = await activities.begin_node_run(
        RunInput(
            project_id=prepared.project_id,
            node_id=prepared.node_id,
            attempt=1,
            execution_contract_version=prepared.contract.version,
            actor_id=ACTOR,
        )
    )
    report = await activities.prepare_execution(plan)
    assert report.outcome is PreparationOutcome.PREPARED, (
        f"the workspace could not be built on this machine: {report.refusal} — "
        f"{report.reason}"
    )

    with database.read_only() as session:
        package = PreparationRepository(session, prepared.project_id).prepared_for_run(
            prepared.node_id, prepared.contract.version
        )
    assert package is not None, "the run reported a workspace it did not record"

    handle = backend.submit(_request(plan, attempt=1, prepared=package))
    return handle.backend_job_ref


async def wait_for_an_ending(backend: SlurmComputeBackend, ref: str) -> JobStatus:
    """Poll the cluster until the job ends, or until this case runs out of time."""
    deadline = asyncio.get_running_loop().time() + LIVE_WAIT_SECONDS
    status = backend.status(ref)
    while not status.state.is_terminal:
        assert asyncio.get_running_loop().time() < deadline, (
            f"the job was still {status.state.value} after {LIVE_WAIT_SECONDS:.0f}s"
        )
        await asyncio.sleep(LIVE_POLL_SECONDS)
        status = backend.status(ref)
    return status


@pytest.mark.live
@pytest.mark.timeout(2400)
async def test_p11_05_a_real_cluster_runs_the_workspace_preparation_built(
    database: Database,
    artifact_store: S3ArtifactStore,
    prepare: Callable[..., Prepared],
    project: Project,
) -> None:
    """The certification no suite in this repository can perform.

    Everything below this case is exercised against a transport a test
    controls, which establishes what the backend does with an answer and says
    nothing about whether a cluster gives one. This case is the other half: a
    real connection, a real `sbatch`, a real wait, and the output the contract
    required coming back as an artifact of the project the run belongs to —
    not marked simulated, because it came off a machine RAVEL does not own and
    marking it either way would be a claim RAVEL has no standing to make.

    It skips, naming every missing setting, until an endpoint is supplied. What
    it proves when it runs is a fact about one cluster and one account, which is
    all a live certification has ever proved — RAVEL's half of the arrangement
    works against something that is not a test double. Whether the *result* is
    scientifically acceptable is Review's, against criteria frozen before the
    run, and nothing here touches that.
    """
    settings = cluster_from_the_environment()
    backend = worker_script.slurm_backend(
        settings,
        database,
        artifact_store,
        worker_script.parse_args(["--compute-backend", "slurm"]),
    )
    a_framework_artifact(database, artifact_store, project.project_id, FRAMEWORK_BYTES)
    prepared = prepare(
        node_type=NodeType.COMPUTATION,
        required_outputs=(RASPA_OUTPUT,),
        parameter_targets=RASPA_TERMS,
        resource_limits=RASPA_LIMITS,
        execution_requirements={"software": "raspa"},
        inputs=(FRAMEWORK_NAME,),
    )

    ref = await hand_to_the_cluster(
        settings=settings,
        database=database,
        store=artifact_store,
        backend=backend,
        prepared=prepared,
    )

    # A job left running on somebody else's cluster is the one outcome this case
    # must not produce, whatever else in it goes wrong. Cancelling a job that
    # has already ended is a no-op on the cluster's side.
    try:
        status = await wait_for_an_ending(backend, ref)
        assert status.state is JobState.COMPLETED, (
            f"the job ended as {status.state.value}: {status.detail}"
        )

        outputs = backend.collect(ref)
        assert outputs.delivered_outputs == (RASPA_OUTPUT,), (
            "the run ended without producing what the contract required: "
            f"{outputs.completion_metadata}"
        )
        assert not any(
            artifact.kind == SIMULATED_KIND
            for artifact in read_back(database, artifact_store, prepared, outputs)
        ), "an artifact collected off a cluster was marked simulated"
    finally:
        with contextlib.suppress(Exception):
            backend.cancel(ref)


def read_back(
    database: Database,
    store: S3ArtifactStore,
    prepared: Prepared,
    outputs: JobOutputs,
) -> list[Artifact]:
    """The artifact rows RAVEL filed for a collection, read out of PostgreSQL.

    Read rather than taken from the return value, because the claim is about
    what was *recorded*: a backend that returned the right ids and wrote no
    rows would satisfy an assertion about the ids alone.
    """
    with database.read_only() as session:
        repository = ArtifactRepository(session, prepared.project_id, store)
        return [
            repository.get(artifact_id=artifact_id) for artifact_id in outputs.artifacts
        ]
