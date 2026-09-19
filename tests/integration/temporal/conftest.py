"""Fixtures for the durable-execution tests.

These are the only tests in the suite that run a real Temporal worker, and the
reason is the same reason the rest of `tests/integration` exists: the property
being asserted — that killing a worker mid-run changes nothing about the
project — is a property of Temporal and PostgreSQL, not of a Python object. A
fake workflow runner would prove that our code calls the methods we think it
calls, which is not in doubt.

The backend is a scripted test double rather than the V0 mock. It is a double
because these tests are about the durable layer: what it must do to a backend
is `submit`, `status`, `deliver`, `collect`, and `cancel`, and the double is
written to answer those from a script so a test can put a run into the state it
needs. The V0 mocks arrive in Phase 6 with the scenario gate that checks them
against `acceptance/MOCK_SCENARIOS.yaml`.
"""

from __future__ import annotations

# Fixtures are imported and then used as fixture parameters, which ruff reads as
# a redefinition. That is the pytest idiom.
# ruff: noqa: F811
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from uuid import uuid4

import pytest
from tests.integration.conftest import (  # noqa: F401
    clean,
    database,
    integration_settings,
    project,
)

from ravel.config import Settings
from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
    ExecutionContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.enums import FailureClass, JobState, NodeStatus, NodeType
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.execution.backends import (
    BackendRegistry,
    ExternalDelivery,
    JobHandle,
    JobOutputs,
    JobRequest,
    JobStatus,
)
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository

#: What the node under test is asked to produce. One required output, so that a
#: run that delivers nothing is visibly incomplete rather than ambiguously so.
REQUIRED_OUTPUT = "conductivity.csv"


@dataclass
class ScriptedBackend:
    """A backend that does what a test tells it to.

    Written to the port's rules rather than around them: `submit` is idempotent
    in `(project_id, node_id, attempt)` and counts how many times it was
    entered, which is how a test can tell a resumed run from a re-run one. The
    count is of *entries*, not of jobs created — a re-entered `submit` that
    returns the job it already has is exactly the behaviour the durable layer
    depends on, and counting only job creations would hide a double call.
    """

    name: str = "scripted-test-backend"
    #: The states each poll reports, in order. The last one repeats, so a run
    #: that is never ended stays where the script left it.
    states: list[JobState] = field(default_factory=lambda: [JobState.RUNNING])
    #: What a given attempt does instead, for tests that care about a retry
    #: ending differently from the attempt before it. Keyed by attempt number,
    #: because that is the only thing that distinguishes one job from the next —
    #: and because a test that had to rewrite `states` at the right moment would
    #: be racing the workflow, which retries as fast as the poll interval allows.
    scripts: dict[int, list[JobState]] = field(default_factory=dict)
    #: What `collect` says was delivered. Empty means nothing was.
    delivered_outputs: tuple[str, ...] = (REQUIRED_OUTPUT,)
    artifacts: tuple[str, ...] = ()
    logs: tuple[str, ...] = ()
    failure_class: FailureClass | None = None
    #: Called on entry to `submit`, before anything is recorded. A test uses it
    #: to fail the call, which is how a run is made to look like one whose
    #: worker died between the backend accepting work and the row being written.
    on_submit: Callable[[JobRequest], None] | None = None
    #: Called on entry to `status`. Tests block here to hold an activity open.
    on_status: Callable[[str], None] | None = None

    submit_entries: int = 0
    deliver_entries: int = 0
    cancel_entries: int = 0
    jobs: dict[str, JobRequest] = field(default_factory=dict)
    by_key: dict[tuple[str, str, int], str] = field(default_factory=dict)
    at: dict[str, int] = field(default_factory=dict)
    deliveries: dict[str, ExternalDelivery] = field(default_factory=dict)

    def submit(self, request: JobRequest) -> JobHandle:
        """Take work on, once per attempt however many times this is called."""
        self.submit_entries += 1
        if self.on_submit is not None:
            self.on_submit(request)
        key = (request.project_id, request.node_id, request.attempt)
        existing = self.by_key.get(key)
        if existing is not None:
            return JobHandle(
                backend_job_ref=existing, state=JobState.SUBMITTED, backend_state="ACCEPTED"
            )
        reference = f"{self.name}-job-{len(self.jobs) + 1}"
        self.by_key[key] = reference
        self.jobs[reference] = request
        self.at[reference] = 0
        return JobHandle(
            backend_job_ref=reference, state=JobState.SUBMITTED, backend_state="ACCEPTED"
        )

    def status(self, backend_job_ref: str) -> JobStatus:
        if self.on_status is not None:
            self.on_status(backend_job_ref)
        script = self.scripts.get(
            self.jobs[backend_job_ref].attempt, self.states
        )
        index = self.at[backend_job_ref]
        state = script[min(index, len(script) - 1)]
        self.at[backend_job_ref] = index + 1
        return JobStatus(
            state=state,
            backend_state=state.value,
            failure_class=self.failure_class if state is JobState.FAILED else None,
            detail=f"scripted: {state.value}",
        )

    def deliver(self, backend_job_ref: str, delivery: ExternalDelivery) -> JobStatus:
        """Record what arrived and move the job on, as a lab answering would."""
        self.deliver_entries += 1
        self.deliveries[backend_job_ref] = delivery
        return JobStatus(
            state=JobState.COMPLETED, backend_state="ANSWERED", detail=delivery.summary
        )

    def collect(self, backend_job_ref: str) -> JobOutputs:
        return JobOutputs(
            artifacts=self.artifacts,
            logs=self.logs,
            delivered_outputs=self.delivered_outputs,
        )

    def cancel(self, backend_job_ref: str) -> bool:
        self.cancel_entries += 1
        return True


@pytest.fixture
def backend() -> ScriptedBackend:
    """The backend one test's runs are handed to."""
    return ScriptedBackend()


@pytest.fixture
def registry(backend: ScriptedBackend) -> BackendRegistry:
    """A registry that sends computation and experiment nodes to the double.

    Both node types point at it because the durable layer must not care which
    kind of work it is running: if the workflow needed a different shape for an
    experiment, that would be a difference the workflow knows about.
    """
    registry = BackendRegistry()
    registry.register(NodeType.COMPUTATION, backend)
    registry.register(NodeType.EXPERIMENT, backend)
    return registry


@pytest.fixture
def runnable_node(
    database: Database, project: Project
) -> Callable[..., DagNode]:
    """Build a node that is READY, with its criteria and contract frozen.

    Assembled the way production assembles one, because the preconditions that
    let a node run are the point: a fixture that wrote the row directly would
    let a run start from a node that could never have reached READY.
    """

    def build(
        *,
        node_type: NodeType = NodeType.COMPUTATION,
        required_outputs: tuple[str, ...] = (REQUIRED_OUTPUT,),
        allowed_retries: int = 0,
    ) -> DagNode:
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
            acceptance_repository = AcceptanceContractRepository(
                session, project.project_id
            )
            acceptance_repository.add(acceptance)
            acceptance_repository.freeze(acceptance.contract_id)

            execution = ExecutionContract(
                project_id=project.project_id,
                node_id=node.node_id,
                objective="Measure the conductivity of each sample.",
                allowed_actions=("run_measurement",),
                required_outputs=required_outputs,
                allowed_retries=allowed_retries,
            )
            execution_repository = ExecutionContractRepository(session, project.project_id)
            execution_repository.add(execution)
            execution_repository.freeze(execution.contract_id)

            dag.bind_acceptance_contract(node.node_id, acceptance.contract_id)
            dag.bind_execution_contract(node.node_id, execution.contract_id)
            return dag.transition_node(
                node.node_id, NodeStatus.READY, actor_id="scheduler"
            )

    return build


@pytest.fixture
def execution_settings(integration_settings: Settings) -> Settings:
    """Settings whose activities reach the test database and time out quickly.

    The poll interval is shortened because every poll is a durable timer, and a
    test that waited five seconds per poll would spend its whole budget asleep.

    The activity timeout is shortened for a reason that is less obvious and more
    important: Temporal cannot tell a worker that died from one that is merely
    slow, so an activity belonging to a killed worker is not retried until its
    `start_to_close` expires. That wait *is* the recovery time after a crash,
    and a test that had to serve the deployment's two minutes would be a test
    nobody runs. It is still the real mechanism, only a shorter one.

    The values are read from `Settings` by the activity, and it is the activity
    that puts them in the run plan — which is what carries them into history.

    **A task queue per test.** A workflow outlives the test that started it: if
    the test fails while a run is under way, the workflow stays in Temporal, and
    the next test's worker polls the same queue with the same registry and picks
    up the abandoned run — driving it against a database the next test has since
    truncated. A private queue means a test only ever runs the work it started,
    and a failure stays the failing test's to explain.
    """
    return integration_settings.model_copy(
        update={
            "temporal_task_queue": f"ravel-v0-test-{uuid4().hex}",
            "job_poll_seconds": 0.2,
            "job_deadline_seconds": 60.0,
            "job_activity_timeout_seconds": 5.0,
        }
    )


@pytest.fixture
def temporal_unreachable(integration_settings: Settings) -> Iterator[None]:
    """Skip rather than fail when the Temporal frontend is not running."""
    import socket

    host, _, port = integration_settings.temporal_host.partition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=1):
            pass
    except OSError as error:
        pytest.skip(f"Temporal is not reachable; run scripts/dev_up.sh ({error})")
    yield
