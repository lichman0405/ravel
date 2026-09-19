"""Fixtures for the mock-backend gate.

The gate is `IMPLEMENTATION_PLAN.md` Phase 6: each scenario
`acceptance/MOCK_SCENARIOS.yaml` names produces exactly the transition it
specifies, and every record a mock produces is marked as simulated.

**The scenarios are read from the catalogue and never restated here.** A test
that hard-coded "COMPUTE_TIMEOUT ends TIMED_OUT" would pass against a mock
implementing something the acceptance file does not describe, which is the one
failure this gate exists to catch. So the fixtures below take a scenario
*name*, resolve it through `ravel.backends.scenarios`, and hand back both the
backend and the scenario it is playing; the assertions are written against the
scenario's own fields.

Two things the fixtures build rather than fake:

- **A real node with a real frozen contract.** The mock's artifacts carry a
  `node_id` that is a foreign key, and the deviation path reads the contract
  version the job ran under — so a fixture that skipped either would let a test
  pass against a project that could not exist.
- **The real object store.** A mock writes bytes, and "the bytes say they are
  simulated" is a claim about what was stored. `artifact_store` is the same
  MinIO the other integration tests use.
"""

from __future__ import annotations

# Fixtures are imported and then used as fixture parameters, which ruff reads as
# a redefinition. That is the pytest idiom.
# ruff: noqa: F811
from collections.abc import Callable
from dataclasses import dataclass

import pytest
from tests.integration.conftest import (  # noqa: F401
    artifact_store,
    clean,
    database,
    integration_settings,
    project,
)
from tests.integration.temporal.conftest import (  # noqa: F401
    execution_settings,
    temporal_unreachable,
)

from ravel.backends import Catalogue, MockComputeBackend, MockLabBackend, catalogue
from ravel.config import Settings
from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
    ExecutionContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, NodeType
from ravel.domain.roles import AgentRole
from ravel.execution.backends import BackendRegistry, JobRequest
from ravel.execution.temporal.client import NodeRunClient
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.store import S3ArtifactStore

#: The outputs a contract requires when the scenario does not name its own.
#: Two of them, so that a run that delivers one short is visibly incomplete
#: rather than ambiguous.
DEFAULT_OUTPUTS = ("conductivity.csv", "notes.json")


@dataclass(frozen=True)
class Prepared:
    """A node that can be run, and the contract it runs under."""

    node: DagNode
    contract: ExecutionContract

    @property
    def node_id(self) -> str:
        """The node's identifier, so a test does not have to reach through."""
        return self.node.node_id

    @property
    def project_id(self) -> str:
        return self.contract.project_id

    def request(self, *, attempt: int = 1) -> JobRequest:
        """The work a Worker would hand a backend for this node."""
        return JobRequest(
            project_id=self.contract.project_id,
            node_id=self.node.node_id,
            attempt=attempt,
            execution_contract_ref=self.contract.contract_id,
            execution_contract_version=self.contract.version,
            objective=self.contract.objective,
            required_outputs=self.contract.required_outputs,
            is_retry=attempt > 1,
        )


@pytest.fixture
def scenarios() -> Catalogue:
    """The acceptance catalogue, which is what the tests are written against."""
    return catalogue()


@pytest.fixture
def prepare(
    database: Database, project
) -> Callable[..., Prepared]:
    """Build a READY node with a frozen contract, the way production does.

    Assembled through the same repositories production uses, because the
    preconditions that let a node run are part of what is being tested: a
    fixture that wrote rows directly could hand a mock a contract that was
    never frozen, and the run would then be exercising a state the system
    cannot reach.
    """

    def build(
        *,
        node_type: NodeType = NodeType.COMPUTATION,
        required_outputs: tuple[str, ...] = DEFAULT_OUTPUTS,
        allowed_actions: tuple[str, ...] = ("run_measurement",),
        allowed_ranges: dict[str, str] | None = None,
        allowed_substitutions: tuple[str, ...] = (),
        allowed_retries: int = 0,
    ) -> Prepared:
        with database.transaction() as session:
            dag = DagRepository(session, project.project_id)
            node = dag.add_node(
                DagNode.create(
                    project_id=project.project_id,
                    node_type=node_type,
                    objective="Measure conductivity across the dopant series.",
                    created_by="master",
                ),
                role=AgentRole.MASTER,
                decision_ref="dec-1",
            )
            acceptance = AcceptanceContract(
                project_id=project.project_id,
                node_id=node.node_id,
                criteria=(
                    AcceptanceCriterion(
                        statement="Conductivity rises by at least 15%.",
                        provenance=CriterionProvenance.USER_REQUIREMENT,
                    ),
                ),
            )
            acceptance_repository = AcceptanceContractRepository(session, project.project_id)
            acceptance_repository.add(acceptance)
            acceptance_repository.freeze(acceptance.contract_id)

            contract = ExecutionContract(
                project_id=project.project_id,
                node_id=node.node_id,
                objective="Measure the conductivity of each sample.",
                allowed_actions=allowed_actions,
                allowed_ranges=allowed_ranges or {},
                allowed_substitutions=allowed_substitutions,
                required_outputs=required_outputs,
                allowed_retries=allowed_retries,
            )
            contracts = ExecutionContractRepository(session, project.project_id)
            contracts.add(contract)
            contracts.freeze(contract.contract_id)

            dag.bind_acceptance_contract(node.node_id, acceptance.contract_id)
            dag.bind_execution_contract(node.node_id, contract.contract_id)
            return Prepared(
                node=dag.transition_node(
                    node.node_id, NodeStatus.READY, actor_id="scheduler"
                ),
                contract=contract,
            )

    return build


@pytest.fixture
def compute(
    database: Database, artifact_store: S3ArtifactStore, mock_clock: FakeClock
) -> Callable[..., MockComputeBackend]:
    """A compute mock playing one named scenario.

    On the test's own clock unless a caller asks for another. The default is the
    injected one on purpose: a mock left on `time.monotonic` does not fail, it
    simply never advances, and a test then fails with a state sequence that
    stopped one step short — which reads as a bug in the mock.
    """

    def build(
        scenario: str,
        *,
        clock: Callable[[], float] | None = None,
        step: float = 0.5,
    ) -> MockComputeBackend:
        return MockComputeBackend(
            database=database,
            store=artifact_store,
            scenario=scenario,
            step_seconds=step,
            clock=clock or mock_clock,
        )

    return build


@pytest.fixture
def lab(
    database: Database, artifact_store: S3ArtifactStore, mock_clock: FakeClock
) -> Callable[..., MockLabBackend]:
    """A laboratory mock playing one named scenario, on the test's own clock."""

    def build(
        scenario: str, *, clock: Callable[[], float] | None = None
    ) -> MockLabBackend:
        return MockLabBackend(
            database=database,
            store=artifact_store,
            scenario=scenario,
            clock=clock or mock_clock,
        )

    return build


class FakeClock:
    """A clock a test moves by hand.

    Injected rather than slept through, because these tests are about which
    transition a scenario produces and when — and a test that slept would be
    asserting that the machine was not busy.
    """

    def __init__(self, at: float = 0.0) -> None:
        self.at = at

    def __call__(self) -> float:
        return self.at

    def advance(self, seconds: float) -> None:
        self.at += seconds


@pytest.fixture
def mock_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def temporal_registry(
    database: Database, artifact_store: S3ArtifactStore
) -> Callable[[str], BackendRegistry]:
    """A registry whose experiment nodes are run by the named lab scenario.

    Used by the tests that need a whole run — the ones whose scenario ends in a
    node status rather than in a backend status. The mock runs on the real
    clock there, because the thing driving it is a real Temporal worker waiting
    real seconds.
    """

    def build(scenario: str) -> BackendRegistry:
        registry = BackendRegistry()
        registry.register(
            NodeType.EXPERIMENT,
            MockLabBackend(
                database=database, store=artifact_store, scenario=scenario
            ),
        )
        return registry

    return build


@pytest.fixture
async def client(execution_settings: Settings, temporal_unreachable) -> NodeRunClient:
    """A client for the runs whose scenario ends in a *node* status.

    Only the deviation and incomplete-delivery tests need this. Everything else
    in this directory drives a backend through its own port, which needs no
    Temporal at all — and the ones that do need it skip rather than fail when
    the frontend is down, which `temporal_unreachable` is for.
    """
    return await NodeRunClient.connect(execution_settings)
