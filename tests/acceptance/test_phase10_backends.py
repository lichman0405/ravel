"""Phase 10 items about what the five agents are allowed to be reached by.

Two claims, and they are the two halves of one: the work runs on mocks rather
than on a real instrument unless a deployment asks otherwise, and the agent that
now sits beside the work cannot do it, stop it, or answer for it. Phase 10 put a
session in each Worker seat; what it did not do is give either seat a way to the
bench, or let one speak for a bench that has already spoken.

The first claim is checked against the composition a deployment actually builds
— `scripts/run_temporal_worker.build_registry` — and against the tree, because
"the mocks are what runs" is a statement about both: a registry that named a
real adapter without being asked would be the obvious breach, and one that was
imported and never registered would be the quiet one.

**Phase 11 widened the tree on purpose.** P11-05 added a real compute backend — a
cluster over SSH — and it is the one thing here that is not a mock. The item's
premise is therefore no longer "mocks are all there is" but "mocks are all that
runs unless a deployment names the real one, and the real one exists only on the
compute side": there is still no LIMS, no robot lab, no VASP wrapper. The two
assertions below were rewritten to that and left exact rather than widened — an
adapter that appeared without this docstring changing is still a failure.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
import scripts.run_temporal_worker as worker_script
from tests.e2e.conftest import Headless
from tests.e2e.test_headless_loop import Nodes
from tests.integration.conftest import Prepared
from tests.integration.temporal.conftest import await_state

import ravel
from ravel.backends import MockComputeBackend, MockLabBackend, catalogue
from ravel.config import Settings
from ravel.domain.artifacts import is_simulated
from ravel.domain.enums import AccessStatus, NodeStatus, NodeType
from ravel.domain.evidence import EvidenceSource
from ravel.execution.loop import ProjectLoop, Situation
from ravel.state.database import Database
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import RecordRepositories
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceSourceRepository,
    SimulatedEvidenceError,
)
from ravel.state.store import S3ArtifactStore

pytestmark = [pytest.mark.phase10, pytest.mark.timeout(600)]

COMPUTE_OUTPUTS = ("conductivity.csv", "notes.json")
LAB_OUTPUTS = ("experiment_log", "raw_data")
ACTOR = "phase10-worker"

PRESSURE = "LAB_DEVIATION_PRESSURE"

#: Everything `WorkBackend` requires of an implementor: its name, and the five
#: calls the durable layer makes on it. A class with all six is a class RAVEL
#: could hand work to, whether or not anything registers it.
BACKEND_SURFACE = ("name", "submit", "status", "deliver", "collect", "cancel")


class RecordingMaster:
    """A `MasterPort` that records what it was handed instead of deciding.

    Nothing here answers the escalation, for the reason A11 does not: this item
    is about *who the question reaches*, and a Master that answered would be
    the next item's subject wearing this one's name.
    """

    def __init__(self) -> None:
        self.situations: list[Situation] = []

    async def act(self, situation: Situation) -> None:
        self.situations.append(situation)


class RecordingWorker:
    """An `ExecutorPort` that records what the loop gives it instead of doing it.

    `start` records rather than runs, which is what makes this a recorder: a
    seat that began the node would put it in RUNNING, and every assertion below
    is about a node that stayed where it was.
    """

    def __init__(self) -> None:
        self.turns: list[tuple[str, str]] = []
        self.started: list[str] = []

    async def start(self, node: Any) -> None:
        self.started.append(node.node_id)

    async def act(self, node: Any, situation: Situation) -> None:
        _ = situation
        self.turns.append((node.node_id, node.status.value))

    def close(self) -> None:
        """A recording port holds no runtime, so there is nothing to close."""


# ── P10-06 / P10-07 ─────────────────────────────────────────────────────────────────────


def test_p10_06_the_deployment_registers_the_mocks_and_nothing_else(
    database: Database, execution_settings: Settings, artifact_store: S3ArtifactStore
) -> None:
    """P10-06: the backends a deployment runs are the two mocks, exactly.

    `build_registry` is the function the worker process calls, so what it
    returns is what a node's work actually reaches — and the assertion is on
    the classes rather than on their names, because a subclass that computed
    something would answer to the same name. `artifact_store` is asked for
    first so that this case skips rather than fails when the object store the
    mocks write through is not up; that is the one dependency building a
    registry has.
    """
    registry = worker_script.build_registry(
        execution_settings, database, worker_script.parse_args([])
    )

    assert set(registry.by_node_type) == {NodeType.COMPUTATION, NodeType.EXPERIMENT}, (
        "a node type this deployment has no backend for is one nothing can run"
    )
    compute = registry.for_node_type(NodeType.COMPUTATION)
    lab = registry.for_node_type(NodeType.EXPERIMENT)
    assert type(compute) is MockComputeBackend, (
        f"computation nodes now run on {type(compute).__name__}; V0's compute is a "
        "mock, and a real one is a change this item is here to notice"
    )
    assert type(lab) is MockLabBackend, (
        f"experiment nodes now run on {type(lab).__name__}; V0's lab is a mock"
    )
    assert set(registry.by_name) == {compute.name, lab.name}
    assert all("mock" in backend.name for backend in registry.by_name.values()), (
        "a backend's name is where its records say where they came from"
    )


def test_p10_07_nothing_else_in_ravel_can_be_handed_work() -> None:
    """P10-07: mocks, plus the one real compute backend Phase 11 added.

    The registry above says what a deployment *does* register; this says what it
    *could*. Anything with the backend port's surface — a name and the five
    calls the durable layer makes — is something RAVEL could hand work to
    whether or not anything points at it yet, so the tree is walked and the
    answer is compared exactly.

    The list is two mocks and `SlurmComputeBackend`. What is still absent is the
    rest of the sentence this item was written to enforce: no laboratory, no
    LIMS, no robot, and no wrapper around a simulation package. Adding one is a
    Phase 11 item with its own certification, not a line in this set.

    The walk is over RAVEL's own package and stops at the migration revisions,
    which are DDL scripts that nothing imports at runtime.
    """
    found: dict[str, type] = {}
    for module_info in pkgutil.walk_packages(ravel.__path__, prefix="ravel."):
        if ".migrations" in module_info.name:
            continue
        module = importlib.import_module(module_info.name)
        for name, member in inspect.getmembers(module, inspect.isclass):
            if member.__module__ != module_info.name:
                continue
            if all(hasattr(member, attribute) for attribute in BACKEND_SURFACE):
                found[f"{module_info.name}.{name}"] = member

    mocks = {
        "ravel.backends.mocks.MockComputeBackend",
        "ravel.backends.mocks.MockLabBackend",
    }
    real = {"ravel.backends.slurm.backend.SlurmComputeBackend"}
    assert set(found) == mocks | real, (
        f"RAVEL has a backend this item does not know about: "
        f"{sorted(set(found) - mocks - real)}"
    )
    assert set(found) - mocks == real, (
        f"the lab side gained a real implementation: {sorted(set(found) - mocks)}; "
        "P11-06 is where a real laboratory channel arrives, with its own "
        "certification, and nothing here anticipates it"
    )


def test_the_real_compute_backend_is_not_what_a_default_deployment_runs() -> None:
    """The claim the two above rest on, asserted where it can be read.

    `build_registry` with a worker's default arguments builds the mocks, so a
    deployment that says nothing runs a project on one machine. The real backend
    is reached by asking for it by name — and refusing to build it when the
    cluster settings are absent is what keeps that opt-in from turning into a
    stall at the first node that reaches the queue.
    """
    assert worker_script.parse_args([]).compute_backend == "mock"
    assert worker_script.COMPUTE_BACKENDS == ("mock", "slurm")
    assert worker_script.parse_args([]).lab_backend == "mock"
    assert worker_script.LAB_BACKENDS == ("mock",)


# ── P10-W10 / P10-W11 ─────────────────────────────────────────────────────


async def test_p10_w10_every_artifact_a_mock_produced_is_marked_simulated(
    headless: Headless,
) -> None:
    """P10-W10: a mock's output says it is a mock's, in a column not a sentence.

    Phase 10 puts an agent in the Worker seat and has it drive a real run to
    completion, which is the first time mock bytes travel the whole path a real
    one would. The claim is about all of them: every artifact the run produced
    carries the simulated kind, and the kind is what the guards ask rather than
    the provenance prose — a marker that lived in free text would be a marker
    somebody could write convincingly.

    The run is started through the Worker's own tool, so what is asserted is
    about artifacts the Phase 10H chain produced rather than ones a test wrote.
    """
    headless.compute("COMPUTE_SUCCESS")
    measure = "Measure conductivity across the dopant series."
    nodes = Nodes(headless.project.project_id)

    # Driven the whole way rather than started and left: the claim is about
    # artifacts a *passed* run produced, and artifacts are registered as the run
    # delivers them. The Worker seat is the e2e one, so the run begins through
    # `start_execution` — the Phase 10H chain, not a shortcut around it.
    run = await headless.drive(
        headless.master((nodes.task("measure", measure),)), headless.review()
    )
    assert run.finished, f"the loop halted at {run.status.value}"

    node = nodes["measure"]
    assert headless.status_of(node) is NodeStatus.PASSED

    with headless.database.read_only() as session:
        artifacts = [
            artifact
            for artifact in ArtifactRepository(session, headless.project.project_id).all()
            if artifact.node_id == node.node_id
        ]
        node = DagRepository(session, headless.project.project_id).node(node.node_id)

    assert artifacts, (
        "the run passed and produced no artifacts, so there is nothing to mark"
    )
    assert node.artifact_refs, "the artifacts are not attached to the node they came from"
    assert {artifact.artifact_id for artifact in artifacts} == set(node.artifact_refs), (
        "the node names artifacts this project does not hold, or holds artifacts it "
        "does not name"
    )

    unmarked = [a.artifact_id for a in artifacts if not is_simulated(a.kind)]
    assert not unmarked, (
        f"a mock produced artifacts that are not marked simulated: {unmarked}; V0's "
        "compute and lab are mocks, and output that cannot be told apart from a "
        "real measurement is the one thing that must not happen"
    )
    assert all(
        a.provenance.startswith("mock-compute:") and "COMPUTE_SUCCESS" in a.provenance
        for a in artifacts
    ), (
        "an artifact is marked simulated but does not say which mock and which named "
        "scenario produced it, so a reader cannot go and check: "
        f"{sorted({a.provenance for a in artifacts})}"
    )


async def test_p10_w11_a_simulated_artifact_cannot_become_evidence(
    headless: Headless,
) -> None:
    """P10-W11: mock bytes are refused at the Evidence Ledger, not discouraged.

    The other half of W10, and the half that matters: marking output as
    simulated is worth nothing if a later turn can cite it as a finding. So the
    ledger is asked directly, with a source that points at an artifact a mock
    really produced in this project, and it refuses — as a guard, not a
    convention.

    The artifact is one the run under test produced rather than a hand-written
    row, because "can simulated output enter the ledger" is a question about
    bytes that exist, and a fabricated artifact would answer a question about
    the fixture.
    """
    headless.compute("COMPUTE_SUCCESS")
    measure = "Measure conductivity across the dopant series."
    nodes = Nodes(headless.project.project_id)
    run = await headless.drive(
        headless.master((nodes.task("measure", measure),)), headless.review()
    )
    assert run.finished, f"the loop halted at {run.status.value}"

    with headless.database.read_only() as session:
        produced = [
            artifact
            for artifact in ArtifactRepository(session, headless.project.project_id).all()
            if artifact.node_id == nodes["measure"].node_id
        ]
    assert produced, "the run produced nothing, so there is no simulated byte to try"
    simulated = produced[0]

    source = EvidenceSource(
        project_id=headless.project.project_id,
        url=f"artifact://{simulated.artifact_id}",
        title="Conductivity of the doped series",
        access_status=AccessStatus.OK,
        retrieved_at=datetime.now(UTC),
        artifact_ref=simulated.artifact_id,
    )
    with headless.database.transaction() as session:
        ledger = EvidenceSourceRepository(session, headless.project.project_id)
        with pytest.raises(SimulatedEvidenceError) as refused:
            ledger.record(source)

    assert simulated.artifact_id in str(refused.value), (
        "the refusal does not name the artifact, so a reader cannot find what was "
        "cited"
    )
    assert "simulated" in str(refused.value), (
        "the refusal does not say why, and a Worker told only 'no' will try another "
        "route to the same bytes"
    )

    with headless.database.read_only() as session:
        stored = EvidenceSourceRepository(session, headless.project.project_id).all()
    assert stored == [], "a simulated source was recorded despite the refusal"


# ── P10-W15 ─────────────────────────────────────────────────────────────────────


async def test_p10_w15_a_lab_deviation_escalates_rather_than_being_answered(
    headless: Headless, prepare: Callable[..., Prepared]
) -> None:
    """P10-W15: a bench that reports outside its terms still stops the work, and the
    question still goes to Master.

    Phase 10 puts an Experimental Worker agent in the seat, and the risk that
    comes with it is the obvious one: the agent is right there, it can read the
    contract, and a system that let it answer would have merged Execution into
    Decision. So the loop is asked who it hands the turn to when the bench has
    already raised the question, and the answer has to be Master — with the
    Worker given no turn on that node at all, the contract unchanged, and
    nothing about the plan moved.
    """
    scenario = catalogue().lab_scenario(PRESSURE)
    assert scenario.report is not None, f"{PRESSURE} no longer reports anything"
    assert scenario.expected_worker_state == NodeStatus.WAITING_DECISION.value, (
        f"the catalogue now expects {scenario.expected_worker_state!r} of a "
        "report the contract does not permit"
    )

    headless.lab(PRESSURE)
    prepared = prepare(node_type=NodeType.EXPERIMENT, required_outputs=LAB_OUTPUTS)
    await headless.client.start_node_run(
        project_id=headless.project.project_id,
        node_id=prepared.node_id,
        actor_id=ACTOR,
        execution_contract_version=prepared.contract.version,
    )
    await await_state(lambda: headless.status_of(prepared.node) is NodeStatus.WAITING_DECISION)

    master = RecordingMaster()
    compute, experimental = RecordingWorker(), RecordingWorker()
    loop = ProjectLoop(
        database=headless.database,
        project_id=headless.project.project_id,
        master=master,
        review=master,  # never reached: this round ends at Master
        compute_worker=compute,
        experimental_worker=experimental,
        research=RecordingWorker(),
        poll_seconds=0.01,
    )
    await loop.step()

    assert len(master.situations) == 1, "the escalation did not reach Master"
    open_escalations = master.situations[0].open_deviations
    assert len(open_escalations) == 1, (
        "Master was given a round without the question it is a round about"
    )
    assert experimental.turns == [], (
        "the Experimental Worker was handed the bench's question, which is Master's to answer"
    )
    assert compute.turns == [], "the Compute Worker was handed a lab's question"
    assert experimental.started == [], (
        "the Experimental Worker was asked to begin a node that is already under "
        "way and has stopped for an answer; a Worker that could start it again "
        "would be one that could override the stop"
    )

    with headless.database.read_only() as session:
        records = RecordRepositories(session, headless.project.project_id)
        deviation = records.deviations.all()[0]
        contract = ExecutionContractRepository(session, headless.project.project_id).for_node(
            prepared.node_id
        )

    assert deviation.is_open, "the question was answered by something other than Master"
    assert deviation.resolved_by_decision_ref is None
    assert deviation.raised_by.startswith("backend:"), (
        "the bench reported this, and an attribution naming an agent would be the "
        "system claiming a decision nobody made"
    )
    assert deviation.requested_action, "a deviation that does not say what was asked for"
    assert contract.version == prepared.contract.version, (
        "a Worker widened the contract it was supposed to be working under"
    )
    assert headless.status_of(prepared.node) is NodeStatus.WAITING_DECISION, (
        "the node moved while its question was still unanswered"
    )
