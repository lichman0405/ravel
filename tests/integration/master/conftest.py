"""Fixtures for Master's own decisions: escalations and endings.

`tests/integration/conftest.py` supplies the database, the clean slate, the
project, and `prepare` — a node ready to run with its contracts frozen. What
is added here is the state Master is asked to act *on*: a Worker that has
stopped and said why, and a project that has got somewhere worth concluding.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest
from tests.integration.conftest import Prepared, build_prepared

#: The readback tests drive the *seat's* server as well as Master's — the record
#: they read back is one Research wrote, in another process — so they need the
#: mapping the runtime launches a `(project, role)` server with. That builder
#: belongs to `tests/integration/roles`, and this is the same borrow
#: `tests/integration/research/conftest.py` makes for the reading surface.
from tests.integration.roles.conftest import RoleEnvironment, role_environment

from ravel.domain.contracts import ExecutionContract, ProjectSuccessContract
from ravel.domain.enums import Confidence, DecisionType, NodeStatus, ProjectStatus
from ravel.domain.execution import DeviationRecord
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.master import MasterService
from ravel.state.database import Database
from ravel.state.repositories.contracts import SuccessContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DeviationRepository
from ravel.state.services.dag import DecisionDraft

__all__ = ["RoleEnvironment", "role_environment"]

#: The action a Worker asks about in these tests. Deliberately absent from the
#: contract `prepare` writes, so every escalation starts from a request the
#: contract is silent about — which is what a deviation *is*.
REFUSED_ACTION = "run_extra_rinse"


@dataclass(frozen=True, slots=True)
class Escalation:
    """A stopped Worker, and the records that say so."""

    deviation: DeviationRecord
    prepared: Prepared

    @property
    def deviation_id(self) -> str:
        return self.deviation.deviation_id

    @property
    def node_id(self) -> str:
        return self.prepared.node_id

    @property
    def contract(self) -> ExecutionContract:
        """The contract the run was under, which is what a revision revises."""
        return self.prepared.contract


@pytest.fixture
def master(database: Database, project: Project) -> Iterator[MasterService]:
    """Master's service for a project, inside the caller's transaction.

    Committed rather than rolled back at teardown, for the same reason the DAG
    package's `service` is: several tests assert that a *refused* decision left
    nothing behind, and that assertion is only worth making if whatever the
    refused call would have written is committed.
    """
    with database.transaction() as session:
        yield MasterService(session, project.project_id)


@pytest.fixture
def prepared(master: MasterService) -> Callable[..., Prepared]:
    """Build a runnable node inside Master's own transaction.

    Not the shared `prepare` fixture, and the difference is a transaction:
    that one opens a session of its own and commits, and Master's session is
    already open and already writing events for the project. Two transactions
    emitting events for one project serialise on the event counter row, and the
    second one does not fail — it waits, until the test times out at five
    minutes with a stack trace pointing at an INSERT that is doing nothing
    wrong. Master's decisions are one unit of work, so the nodes they are made
    about are built in that unit too.

    `build_prepared` is the shared implementation, so a node built here is the
    same node the other packages build.
    """

    def build(**options: object) -> Prepared:
        return build_prepared(
            master.session,
            project_id=master.project_id,
            **options,  # type: ignore[arg-type]
        )

    return build


@pytest.fixture
def escalated(
    master: MasterService, prepared: Callable[..., Prepared]
) -> Callable[..., Escalation]:
    """Drive a node to the state a deviation leaves it in.

    Three moves, in the order the runtime makes them: the node runs, the
    backend reports something the contract does not name, and the Worker stops
    and escalates. The node lands at `WAITING_DECISION` because that is where
    `finish_node_run` puts it — the status that means "blocked on Master", and
    the only one from which A12's three answers make sense.
    """

    def build(
        *, requested_action: str = REFUSED_ACTION, **contract: object
    ) -> Escalation:
        runnable = prepared(**contract)  # type: ignore[arg-type]
        dag = DagRepository(master.session, master.project_id)
        dag.transition_node(
            runnable.node_id, NodeStatus.RUNNING, actor_id="compute-worker"
        )
        deviation = DeviationRepository(master.session, master.project_id).raise_(
            DeviationRecord(
                project_id=master.project_id,
                node_id=runnable.node_id,
                execution_contract_ref=runnable.contract.contract_id,
                requested_action=requested_action,
                description=(
                    f"the contract does not list the action {requested_action!r}"
                ),
                raised_by="backend:mock-compute",
            )
        )
        dag.transition_node(
            runnable.node_id, NodeStatus.WAITING_DECISION, actor_id="compute-worker"
        )
        return Escalation(deviation=deviation, prepared=runnable)

    return build


@pytest.fixture
def success_contract(
    database: Database, project: Project
) -> ProjectSuccessContract:
    """Freeze what success means, which is what A20 measures a conclusion against."""
    contract = ProjectSuccessContract(
        project_id=project.project_id,
        success_criteria=("The dopant series shows a 15% conductivity gain.",),
        failure_criteria=("No sample exceeds the control by more than noise.",),
        unresolved_uncertainty_policy="Conclude inconclusive rather than guess.",
    )
    with database.transaction() as session:
        return SuccessContractRepository(session, project.project_id).add_version(
            contract
        )


@pytest.fixture
def running(master: MasterService, success_contract: ProjectSuccessContract) -> Project:
    """A project that has started work.

    Two moves through the registry, in the order production makes them: the
    contracts are defined, and the project begins executing. Both are needed
    before an ending is even *reachable* — a project cannot go from CREATED to
    COMPLETED, and `MasterService.conclude` refuses the ending rather than
    discovering that halfway through recording it.

    It depends on the success contract because that is what CONTRACT_DEFINED
    means: a project has somewhere to be going. The dependency is also what
    keeps fixture order out of the tests' hands — pytest sets this up before
    the body runs, and this is a transaction of its own, so it has to happen
    before Master's session writes anything.
    """
    registry = ProjectRegistry(master.session)
    registry.transition(
        master.project_id,
        ProjectStatus.CONTRACT_DEFINED,
        actor_id=AgentRole.MASTER.value,
        reason="Success and failure criteria are frozen.",
    )
    return registry.transition(
        master.project_id,
        ProjectStatus.EXECUTING,
        actor_id=AgentRole.MASTER.value,
        reason="The first stage is ready to run.",
    )


@pytest.fixture
def a_decision() -> Callable[..., DecisionDraft]:
    """A decision draft of a given type, with a rationale that says something."""

    def build(
        decision_type: DecisionType,
        *,
        rationale: str = "The evidence supports this and nothing else does.",
        **overrides: object,
    ) -> DecisionDraft:
        return DecisionDraft(
            decision_type=decision_type,
            rationale=rationale,
            confidence=Confidence.MEDIUM,
            **overrides,  # type: ignore[arg-type]
        )

    return build


@pytest.fixture
def run_to_end(master: MasterService) -> Callable[..., NodeStatus]:
    """Drive a prepared node to a terminal status the way the runtime would.

    Along the real path — RUNNING, REVIEWING, PASSED — rather than by writing a
    status, which the DAG's transition table would refuse from READY anyway. A
    node that has already been run is left alone, so a test can end two nodes
    and call this on both without tracking which came first.
    """
    dag = DagRepository(master.session, master.project_id)

    def drive(node_id: str, outcome: NodeStatus = NodeStatus.PASSED) -> NodeStatus:
        if not dag.node(node_id).is_terminal:
            dag.transition_node(node_id, NodeStatus.RUNNING, actor_id="compute-worker")
            if outcome is NodeStatus.PASSED:
                dag.transition_node(
                    node_id, NodeStatus.REVIEWING, actor_id="compute-worker"
                )
            dag.transition_node(node_id, outcome, actor_id="review")
        return dag.node(node_id).status

    return drive
