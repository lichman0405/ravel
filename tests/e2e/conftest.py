"""Fixtures for the end-to-end gates: a project driven by the loop, headless.

The loop is what these tests are about, and everything under it is real: a real
PostgreSQL, a real Temporal worker, the real V0 mock backends, and the real
services. What is scripted is the two agent powers — Master and Review are
Python objects here rather than DSH sessions — because the question this
directory asks is *whether the machinery closes a project*, and what a model
decides is what `tests/dsh` asks about separately.

**The scripted ports are policy, not mechanism.** `ScriptedMaster` decides what
to do next from a small explicit script and then does it through
`DagMutationService`, `MasterService` and `ReviewService` — the same services
Master's and Review's MCP tools wrap. So a bug in the wiring fails here, and
this gate cannot pass while production is broken in a way it does not share.

**Every transaction here is short and its own.** `act` opens one, writes, and
commits, which is what an MCP tool call does. The loop holds a read-only
transaction while it reads and none while a port runs; a port that borrowed the
loop's session would serialise against the worker's activities on the project's
event counter and hang the test rather than fail it.
"""

from __future__ import annotations

# Fixtures are imported and then used as fixture parameters, which ruff reads as
# a redefinition. That is the pytest idiom.
# ruff: noqa: F811
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

import pytest
from sqlalchemy.orm import Session
from tests.integration.backends.conftest import (  # noqa: F401
    artifact_store,
    clean,
    database,
    integration_settings,
    mock_clock,
    project,
    temporal_unreachable,
)

# The integration fixture under another name, because this directory shortens
# one of its numbers and the fixture it derives from still has to be reachable.
from tests.integration.backends.conftest import (  # noqa: F401
    execution_settings as integration_execution_settings,
)
from tests.integration.temporal.conftest import RunningWorker

from ravel.backends import MockComputeBackend, MockLabBackend
from ravel.config import Settings
from ravel.domain.contracts import (
    AcceptanceContract,
    AcceptanceCriterion,
    CriterionProvenance,
    ExecutionContract,
    ProjectSuccessContract,
)
from ravel.domain.dag import DagNode
from ravel.domain.decisions import CriterionResult, ReviewRecord
from ravel.domain.enums import (
    Confidence,
    DecisionType,
    NodeStatus,
    NodeType,
    ProjectOutcome,
    ProjectStatus,
    ReviewCheckpoint,
    ReviewOutcome,
    TerminationStatus,
)
from ravel.domain.project import Project, RoadmapPhase
from ravel.domain.roles import AgentRole
from ravel.execution.backends import BackendRegistry
from ravel.execution.loop import ProjectLoop, ProjectRun, Situation
from ravel.execution.node_runs import TemporalNodeRuns
from ravel.execution.temporal.client import NodeRunClient
from ravel.master import ENDING_DECISION, MasterService, ReviseContract
from ravel.review import ReviewService
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
    SuccessContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry, RoadmapRepository
from ravel.state.repositories.records import DeviationRepository, RecordRepositories
from ravel.state.services.dag import DagMutationService, DecisionDraft
from ravel.state.store import S3ArtifactStore

#: The phase every scripted project plans into. One stage, so the rolling
#: horizon never has anything to refuse and a test that fails is failing about
#: the loop.
PHASE = "Screen"

#: What the nodes in these tests are asked to produce. Two outputs, so a run
#: that delivers one short is visibly incomplete rather than ambiguous.
OUTPUTS = ("conductivity.csv", "notes.json")



@dataclass(frozen=True, slots=True)
class Task:
    """One node Master plans, with the terms it runs under.

    Terms travel with the task rather than in a second call, because a node
    without frozen criteria can never run: a plan that committed one would have
    a node in it that no worker may start.
    """

    build: Callable[[], DagNode]
    criteria: tuple[str, ...] = ("Conductivity rises by at least 15%.",)
    allowed_actions: tuple[str, ...] = ("run_measurement",)
    required_outputs: tuple[str, ...] = OUTPUTS
    allowed_retries: int = 0

    def node(self) -> DagNode:
        """A fresh node for this task. Fresh, because a node is written once."""
        return self.build()


def commit_terms(session: Session, project_id: str, task: Task, node: DagNode) -> None:
    """Write and freeze the two contracts a node runs under, and bind them."""
    acceptance = AcceptanceContract(
        project_id=project_id,
        node_id=node.node_id,
        criteria=tuple(
            AcceptanceCriterion(
                statement=statement, provenance=CriterionProvenance.USER_REQUIREMENT
            )
            for statement in task.criteria
        ),
    )
    acceptances = AcceptanceContractRepository(session, project_id)
    acceptances.add(acceptance)
    acceptances.freeze(acceptance.contract_id)

    execution = ExecutionContract(
        project_id=project_id,
        node_id=node.node_id,
        objective=node.objective,
        allowed_actions=task.allowed_actions,
        required_outputs=task.required_outputs,
        allowed_retries=task.allowed_retries,
    )
    executions = ExecutionContractRepository(session, project_id)
    executions.add(execution)
    executions.freeze(execution.contract_id)

    dag = DagRepository(session, project_id)
    dag.bind_acceptance_contract(node.node_id, acceptance.contract_id)
    dag.bind_execution_contract(node.node_id, execution.contract_id)


@dataclass
class ScriptedMaster:
    """Master's authority, exercised by a script rather than by a model.

    The script is deliberately dull: freeze what success means, commit the
    first stage, answer an escalation, replace what a failure stranded, and end
    the project. Those are the five things Master does, and a test that wants a
    different ending changes one field rather than writing a second Master.
    """

    database: Database
    project_id: str
    tasks: tuple[Task, ...]
    phase: str = PHASE
    #: What to put in place of work a failure stranded. `None` means the
    #: project does not replan — which is how the tests that end a project on a
    #: failure say so.
    replan: Callable[[DagNode], tuple[Task, ...]] | None = None
    #: What to conclude once nothing is left to run.
    outcome: ProjectOutcome = ProjectOutcome.SUCCESS
    #: What this Master did, in order. A test reads this rather than deriving
    #: from the database what a script was asked to do.
    trace: list[str] = field(default_factory=list)
    planned: tuple[DagNode, ...] = ()

    async def act(self, situation: Situation) -> None:
        """Take the one decision this situation calls for."""
        with self.database.transaction() as session:
            self._act(session, situation)

    # ── The script ──────────────────────────────────────────────────────────

    def _act(self, session: Session, situation: Situation) -> None:
        if situation.open_deviations:
            self._answer(session, situation.open_deviations[0].deviation_id)
            return
        failed = [node for node in situation.nodes if node.status is NodeStatus.FAILED]
        if failed and self.replan is not None and "replanned" not in self.trace:
            self._replan(session, failed[0])
            return
        if not situation.nodes:
            self._plan(session, situation)
            return
        self._conclude(session, situation)

    def _plan(self, session: Session, situation: Situation) -> None:
        """Freeze what success means, start the project, commit the first stage.

        Three moves, and the first two are the registry's: a project is CREATED
        until something says what would make it succeed, and it cannot reach a
        terminal status from CREATED — so a Master that planned without them
        would build a DAG in a project that could never end.
        """
        if situation.project.status.value == "CREATED":
            SuccessContractRepository(session, self.project_id).add_version(
                ProjectSuccessContract(
                    project_id=self.project_id,
                    success_criteria=("The dopant series shows a 15% conductivity gain.",),
                    failure_criteria=("No sample exceeds the control beyond noise.",),
                    unresolved_uncertainty_policy="Conclude inconclusive rather than guess.",
                )
            )
            registry = ProjectRegistry(session)
            for status, reason in (
                (ProjectStatus.CONTRACT_DEFINED, "Success and failure criteria are frozen."),
                (ProjectStatus.EXECUTING, "The first stage is ready to run."),
            ):
                registry.transition(
                    self.project_id,
                    status,
                    actor_id=AgentRole.MASTER.value,
                    reason=reason,
                )
            self.trace.append("contract_defined")

        RoadmapRepository(session, self.project_id).register(
            RoadmapPhase(project_id=self.project_id, name=self.phase, order=0),
            role=AgentRole.MASTER,
        )
        expanded = DagMutationService(session, self.project_id).expand_phase(
            self.phase,
            [task.node() for task in self.tasks],
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=DecisionType.CREATE_NODE,
                rationale="This is the work the first stage consists of.",
                confidence=Confidence.MEDIUM,
            ),
        )
        for task, node in zip(self.tasks, expanded.nodes, strict=True):
            commit_terms(session, self.project_id, task, node)
        self.planned = expanded.nodes
        self.trace.append("planned")

    def _answer(self, session: Session, deviation_id: str) -> None:
        """Answer a Worker's escalation by widening the contract it asked about.

        One of A12's three answers, and the only one this script uses. The
        other two replace the work or end the project, which are different
        scenarios rather than different assertions about this one.

        The revised terms are a *new* contract rather than an edited copy of the
        old one: a version is a row, the row that was frozen stays as it was,
        and a copy that kept its identifier would be an attempt to write the
        same version twice — which is what the primary key on the table is there
        to refuse.
        """
        deviations = DeviationRepository(session, self.project_id)
        deviation = deviations.get(deviation_id=deviation_id)
        current = ExecutionContractRepository(session, self.project_id).for_node(
            deviation.node_id
        )
        MasterService(session, self.project_id).resolve_deviation(
            deviation_id,
            ReviseContract(
                terms=ExecutionContract(
                    project_id=current.project_id,
                    node_id=current.node_id,
                    version=current.version + 1,
                    objective=current.objective,
                    procedure=current.procedure,
                    allowed_actions=(
                        *current.allowed_actions,
                        deviation.requested_action,
                    ),
                    allowed_retries=current.allowed_retries,
                    required_outputs=current.required_outputs,
                )
            ),
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=DecisionType.REVISE_EXECUTION_CONTRACT,
                rationale=(
                    f"The worker asked for {deviation.requested_action!r} and the "
                    "contract did not name it. The action is sound here, so the "
                    "contract is widened rather than the work abandoned."
                ),
                confidence=Confidence.MEDIUM,
            ),
        )
        self.trace.append("answered_deviation")

    def _replan(self, session: Session, failed: DagNode) -> None:
        """Answer a failure with different work, retiring what it stranded."""
        assert self.replan is not None
        tasks = self.replan(failed)
        replacement = DagMutationService(session, self.project_id).replan_after_failure(
            failed.node_id,
            [task.node() for task in tasks],
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=DecisionType.REPLACE_NODE,
                rationale=(
                    f"{failed.display_id} failed. What it stranded is retired and "
                    "the question is asked again by another method."
                ),
                confidence=Confidence.MEDIUM,
            ),
        )
        for task, node in zip(tasks, replacement.created, strict=True):
            commit_terms(session, self.project_id, task, node)
        self.trace.append("replanned")

    def _conclude(self, session: Session, situation: Situation) -> None:
        MasterService(session, self.project_id).conclude(
            self.outcome,
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=ENDING_DECISION[self.outcome],
                rationale="Every node has ended, and this is what they came to.",
                confidence=Confidence.MEDIUM,
            ),
            reason="Nothing is left to run.",
        )
        self.trace.append(f"concluded:{self.outcome.value}")


@dataclass
class ScriptedReview:
    """Review's verdicts, exercised by a script rather than by a model.

    A node's verdict is looked up in `verdicts` by a phrase in its objective,
    so a test can say "this one fails" without knowing an identifier RAVEL
    assigns. What is not named is judged from the record the way a Reviewer
    reads it: a run that completed is a result and the criteria are taken to be
    satisfied by it, and a run that did not complete produced nothing to accept.
    """

    database: Database
    project_id: str
    verdicts: dict[str, ReviewOutcome] = field(default_factory=dict)
    trace: list[str] = field(default_factory=list)

    async def act(self, situation: Situation) -> None:
        for node in situation.needs_pre_flight:
            self._submit(node, ReviewCheckpoint.PRE_RUN, ReviewOutcome.PASS)
        for node in situation.nodes:
            if node.status is NodeStatus.REVIEWING:
                self._submit(node, ReviewCheckpoint.FINAL, self._verdict(node))

    def _verdict(self, node: DagNode) -> ReviewOutcome:
        """What this script says of a node that has handed its result over.

        The default reads the run rather than the objective, and it has to: a
        run that stopped short — a job that failed, a deadline that passed —
        still arrives at REVIEWING, because a failure is a result to be judged
        and not an error to be swallowed. Passing it would be accepting work
        that does not exist, which is exactly what `ReviewService` refuses when
        a verdict does not answer the criteria one by one. A scenario that wants
        a different reading names one in `verdicts`.
        """
        for phrase, outcome in self.verdicts.items():
            if phrase in node.objective:
                return outcome
        with self.database.read_only() as session:
            execution = RecordRepositories(session, self.project_id).latest_execution(
                node.node_id
            )
        assert execution is not None, (
            f"{node.display_id} is in REVIEWING with no Execution Record; a result "
            "reaches Review by being recorded, and there is nothing here to judge"
        )
        if execution.termination_status is TerminationStatus.COMPLETED:
            return ReviewOutcome.PASS
        return ReviewOutcome.FAIL

    def _submit(
        self, node: DagNode, checkpoint: ReviewCheckpoint, outcome: ReviewOutcome
    ) -> None:
        with self.database.transaction() as session:
            criteria = AcceptanceContractRepository(
                session, self.project_id
            ).frozen_for_node(node.node_id)
            assert criteria is not None, f"{node.display_id} has no frozen criteria"
            ReviewService(session, self.project_id).submit(
                ReviewRecord(
                    project_id=self.project_id,
                    node_id=node.node_id,
                    checkpoint=checkpoint,
                    frozen_criteria_ref=criteria.contract_id,
                    frozen_criteria_version=criteria.version,
                    outcome=outcome,
                    # A final verdict answers every criterion the node was
                    # frozen against, one by one — the service refuses a PASS
                    # that leaves one unreported, because a criterion nobody
                    # answered is a criterion nobody checked.
                    criterion_results=(
                        tuple(
                            CriterionResult(
                                criterion_id=criterion.criterion_id,
                                statement=criterion.statement,
                                satisfied=outcome is ReviewOutcome.PASS,
                                observed="measured against the run's outputs",
                            )
                            for criterion in criteria.criteria
                        )
                        if checkpoint is ReviewCheckpoint.FINAL
                        else ()
                    ),
                    diagnosis=f"{checkpoint.value} review: {outcome.value}.",
                ),
                role=AgentRole.REVIEW,
            )
        self.trace.append(f"{checkpoint.value}:{node.display_id}:{outcome.value}")


@dataclass
class Headless:
    """A project the loop drives, with everything below the two agents real."""

    database: Database
    settings: Settings
    project: Project
    store: S3ArtifactStore
    registry: BackendRegistry
    worker: RunningWorker
    client: NodeRunClient

    @classmethod
    async def start(
        cls,
        *,
        database: Database,
        settings: Settings,
        project: Project,
        store: S3ArtifactStore,
    ) -> Headless:
        registry = BackendRegistry()
        worker = await RunningWorker.start(settings, registry, database)
        client = await NodeRunClient.connect(settings)
        return cls(
            database=database,
            settings=settings,
            project=project,
            store=store,
            registry=registry,
            worker=worker,
            client=client,
        )

    def compute(self, scenario: str) -> None:
        """Send computation nodes to the real mock, playing a named scenario."""
        self.registry.register(
            NodeType.COMPUTATION,
            MockComputeBackend(
                database=self.database,
                store=self.store,
                scenario=scenario,
                step_seconds=0.2,
            ),
        )

    def lab(self, scenario: str) -> None:
        """Send experiment nodes to the real mock lab, playing a named scenario."""
        self.registry.register(
            NodeType.EXPERIMENT,
            MockLabBackend(database=self.database, store=self.store, scenario=scenario),
        )

    def master(
        self,
        tasks: tuple[Task, ...],
        *,
        replan: Callable[[DagNode], tuple[Task, ...]] | None = None,
        outcome: ProjectOutcome = ProjectOutcome.SUCCESS,
    ) -> ScriptedMaster:
        return ScriptedMaster(
            database=self.database,
            project_id=self.project.project_id,
            tasks=tasks,
            replan=replan,
            outcome=outcome,
        )

    def review(self, verdicts: dict[str, ReviewOutcome] | None = None) -> ScriptedReview:
        return ScriptedReview(
            database=self.database,
            project_id=self.project.project_id,
            verdicts=verdicts or {},
        )

    async def drive(
        self,
        master: ScriptedMaster,
        review: ScriptedReview,
        *,
        poll_seconds: float = 0.05,
        max_rounds: int = 400,
        max_stalled_rounds: int = 200,
    ) -> ProjectRun:
        """Run the loop until the project ends, or until it stops moving."""
        loop = ProjectLoop(
            database=self.database,
            project_id=self.project.project_id,
            master=master,
            review=review,
            execution=await TemporalNodeRuns.connect(self.database, self.settings),
            poll_seconds=poll_seconds,
            max_rounds=max_rounds,
            max_stalled_rounds=max_stalled_rounds,
        )
        return await loop.run()

    def status_of(self, node: DagNode) -> NodeStatus:
        """A node's status, read fresh. The loop's own view of the project."""
        with self.database.read_only() as session:
            return DagRepository(session, self.project.project_id).node(
                node.node_id
            ).status

    async def stop(self) -> None:
        await self.worker.stop_gracefully()


#: How long a run may last before its own deadline ends it. Short, because one
#: scenario needs it: a lab that reported something it could not do keeps
#: working once permitted and never finishes on its own, so the deadline is what
#: ends that run — and a test that served the deployment's hour would be a test
#: nobody runs. The mechanism is the real one, only a shorter one, exactly as
#: the integration settings shorten the poll interval.
RUN_DEADLINE_SECONDS = 4.0


@pytest.fixture
def execution_settings(integration_execution_settings: Settings) -> Settings:
    """The integration settings, with a run deadline a test can wait out."""
    return integration_execution_settings.model_copy(
        update={"job_deadline_seconds": RUN_DEADLINE_SECONDS}
    )


@pytest.fixture
async def headless(
    database: Database,
    execution_settings: Settings,
    artifact_store: S3ArtifactStore,
    project: Project,
    temporal_unreachable: None,
) -> AsyncIterator[Headless]:
    """A running worker and a Temporal client, for one test's project."""
    run = await Headless.start(
        database=database,
        settings=execution_settings,
        project=project,
        store=artifact_store,
    )
    try:
        yield run
    finally:
        await run.stop()
