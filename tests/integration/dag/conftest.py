"""Fixtures for the Scientific DAG integration tests.

`tests/integration/conftest.py` supplies the database, the clean slate, and a
project. What is added here is the plan: a roadmap for the rolling horizon to
have something to say about, and the mutation service, which is the only
supported way to change the DAG.

The helpers are fixtures rather than importable functions because `tests/` is
not a package — pytest puts each test module's own directory on the path, not
the repository root, so a shared helper has to be handed over by pytest itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from sqlalchemy.orm import Session

from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
    ExecutionContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import (
    JoinPolicy,
    NodeType,
    ReviewCheckpoint,
    ReviewOutcome,
)
from ravel.domain.project import Project, RoadmapPhase
from ravel.domain.roles import AgentRole
from ravel.review import ReviewService
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.projects import RoadmapRepository
from ravel.state.services.dag import DagMutationService

#: Four stages, so that "the current stage plus two more" has a stage beyond it.
STAGES = ("Stage 1", "Stage 2", "Stage 3", "Stage 4")


def register_roadmap(database: Database, project: Project, *names: str) -> list[RoadmapPhase]:
    """Commit a roadmap the way Master registers one."""
    with database.transaction() as session:
        repository = RoadmapRepository(session, project.project_id)
        return [
            repository.register(
                RoadmapPhase(project_id=project.project_id, name=name, order=order),
                role=AgentRole.MASTER,
            )
            for order, name in enumerate(names)
        ]


@pytest.fixture
def service(database: Database, project: Project) -> Iterator[DagMutationService]:
    """The mutation service, for a project that already has a four-stage plan.

    The roadmap is committed before this session opens, so the service sees a
    project with a plan rather than one it has to assemble first.

    The transaction commits at teardown rather than rolling back, deliberately:
    several tests assert that a *refused* mutation left nothing behind, and that
    assertion is only worth making if whatever the refused call would have
    written is committed. A rollback would hide exactly the bug those tests
    exist to catch.
    """
    register_roadmap(database, project, *STAGES)
    with database.transaction() as session:
        yield DagMutationService(session, project.project_id)


@pytest.fixture
def a_node(project: Project) -> Callable[..., DagNode]:
    """Build a node in this project, the way production builds one."""

    def build(
        node_type: NodeType = NodeType.RESEARCH,
        *,
        objective: str = "Survey the literature on the dopant series.",
        **overrides: object,
    ) -> DagNode:
        node = DagNode.create(
            project_id=project.project_id,
            node_type=node_type,
            objective=objective,
            created_by="master",
        )
        return node.model_copy(update=overrides) if overrides else node

    return build


@pytest.fixture
def a_join_node(project: Project, a_node: Callable[..., DagNode]) -> Callable[..., DagNode]:
    """Build a node that waits on others.

    A HYPOTHESIS node, because the join is a decision about results rather than
    a piece of work, and its executor is Master, who is the role that decides.
    """

    def build(dependencies: tuple[str, ...], **overrides: object) -> DagNode:
        return a_node(
            NodeType.HYPOTHESIS,
            objective="Decide whether the dopant series is worth a second round.",
            dependencies=dependencies,
            join_policy=overrides.pop("join_policy", JoinPolicy.ALL),
            **overrides,
        )

    return build


@pytest.fixture
def acceptance_contract() -> Callable[[Session, str, str], str]:
    """Write and freeze an acceptance contract. `(session, project, node) -> id`."""

    def write(session: Session, project_id: str, node_id: str) -> str:
        contract = AcceptanceContract(
            project_id=project_id,
            node_id=node_id,
            criteria=(
                AcceptanceCriterion(
                    statement="Conductivity rises by at least 15%.",
                    provenance=CriterionProvenance.USER_REQUIREMENT,
                ),
            ),
        )
        repository = AcceptanceContractRepository(session, project_id)
        repository.add(contract)
        repository.freeze(contract.contract_id)
        return contract.contract_id

    return write


@pytest.fixture
def freeze_criteria(
    service: DagMutationService, acceptance_contract: Callable[[Session, str, str], str]
) -> Callable[[str], str]:
    """Write and freeze acceptance criteria for one of this project's nodes."""

    def freeze(node_id: str) -> str:
        return acceptance_contract(service.session, service.project_id, node_id)

    return freeze


@pytest.fixture
def clear_to_run(service: DagMutationService) -> Callable[[str], None]:
    """Submit the pre-flight PASS that lets a COMPUTATION or EXPERIMENT node run.

    The third thing a node needs before RUNNING, beside its two contracts, and
    the only one that is a *judgement* rather than a record: the DAG refuses the
    transition without it, so a fixture that wants to drive a node to a terminal
    status has to pass this checkpoint the way the runtime does.

    The criteria it names are read from the node's frozen acceptance contract
    rather than supplied by the caller, so the review is about the plan the node
    will actually run. A caller free to name its own could hand the gate a PASS
    over criteria the node is not measured against, which is the mismatch
    `ReviewService` exists to refuse — and a fixture that could produce one
    would be handing the tests a clearance production cannot issue.
    """

    def clear(node_id: str) -> None:
        criteria = AcceptanceContractRepository(
            service.session, service.project_id
        ).frozen_for_node(node_id)
        if criteria is None:
            raise AssertionError(
                f"{node_id} has no frozen acceptance criteria to be cleared against"
            )
        ReviewService(service.session, service.project_id).submit(
            ReviewRecord(
                project_id=service.project_id,
                node_id=node_id,
                checkpoint=ReviewCheckpoint.PRE_RUN,
                frozen_criteria_ref=criteria.contract_id,
                frozen_criteria_version=criteria.version,
                outcome=ReviewOutcome.PASS,
                diagnosis="The criteria are measurable and the contract permits the run.",
            ),
            role=AgentRole.REVIEW,
        )

    return clear


@pytest.fixture
def execution_contract(service: DagMutationService) -> Callable[[str], str]:
    """Write and freeze an Execution Contract for a node, returning its id."""

    def write(node_id: str) -> str:
        contract = ExecutionContract(
            project_id=service.project_id,
            node_id=node_id,
            objective="Measure the conductivity of each sample.",
            allowed_actions=("run_measurement",),
        )
        repository = ExecutionContractRepository(service.session, service.project_id)
        repository.add(contract)
        repository.freeze(contract.contract_id)
        return contract.contract_id

    return write
