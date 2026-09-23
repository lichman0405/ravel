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
import threading
import time
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from typing import ClassVar

import pytest
import uvicorn
from fastapi import FastAPI
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

# The Gateway's fixtures, for the same reason: `test_tui.py` drives the console
# against the real application, and "real" means the one `create_app` builds
# with the deployment's own secret rather than a second one assembled here.
from tests.integration.gateway.conftest import (  # noqa: F401
    PASSWORD,
    SECRET,
    TTL_SECONDS,
    account,
    bearer,
    gateway_settings,
    sign_in,
    tokens,
)
from tests.integration.temporal.conftest import RunningWorker

from ravel.backends import MockComputeBackend, MockLabBackend
from ravel.config import Settings
from ravel.domain.contracts import (
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
from ravel.execution.node_runs import ExecutionService
from ravel.execution.temporal.client import NodeRunClient
from ravel.execution.temporal.worker import materializers_for
from ravel.gateway.app import create_app
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.conversation import Answer
from ravel.gateway.stream import Cadence
from ravel.master import ENDING_DECISION, MasterService, ReviseContract
from ravel.mcp.context import ToolContext
from ravel.mcp.scope import ToolScope
from ravel.mcp.tools import IMPLEMENTATIONS
from ravel.review import ReviewService
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ExecutionContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import RoadmapRepository
from ravel.state.repositories.records import DeviationRepository, RecordRepositories
from ravel.state.services.dag import DagMutationService, DecisionDraft
from ravel.state.services.terms import commit_terms
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

    def commit(self, session: Session, project_id: str, node: DagNode) -> None:
        """Write and freeze the two contracts this task's node runs under.

        A script's vocabulary — criteria as the sentences Master wrote them —
        on top of the same act production performs, rather than a second copy of
        it. What freezes and binds a node is one function, so a scripted run and
        a planned one cannot end up holding differently-shaped terms.
        """
        commit_terms(
            session,
            project_id,
            node,
            criteria=tuple(
                AcceptanceCriterion(
                    statement=statement, provenance=CriterionProvenance.USER_REQUIREMENT
                )
                for statement in self.criteria
            ),
            allowed_actions=self.allowed_actions,
            required_outputs=self.required_outputs,
            allowed_retries=self.allowed_retries,
        )


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

        Two moves, and the first is the production one: a project is CREATED
        until something says what would make it succeed, and it cannot reach a
        terminal status from CREATED — so a Master that planned without it would
        build a DAG in a project that could never end. `define_success` is the
        act, through the same service the `commit_success_contract` tool calls,
        because a script that wrote the row and moved the status by hand would
        be covering for the product rather than exercising it: that is exactly
        how the act came to exist in a fixture and nowhere else, and a live run
        found the gap.

        The move to EXECUTING is not here at all. It belongs to the first node
        entering RUNNING, which is the DAG's statement that the project has
        begun, and this script no longer makes it on the project's behalf.
        """
        if situation.project.status is ProjectStatus.CREATED:
            MasterService(session, self.project_id).define_success(
                ProjectSuccessContract(
                    project_id=self.project_id,
                    success_criteria=("The dopant series shows a 15% conductivity gain.",),
                    failure_criteria=("No sample exceeds the control beyond noise.",),
                    unresolved_uncertainty_policy="Conclude inconclusive rather than guess.",
                ),
                role=AgentRole.MASTER,
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
            task.commit(session, self.project_id, node)
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
                    # A revised revision is still the same question, measured
                    # the same way: the new version carries the acceptance
                    # contract forward rather than severing it, so a reviewer
                    # reading the terms still finds what they were frozen for.
                    acceptance_contract_ref=current.acceptance_contract_ref,
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
            task.commit(session, self.project_id, node)
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
class ScriptedWorker:
    """A Worker seat whose turns are scripted, over a real tool context.

    Phase 10H makes the Worker Agent the entry point for work, which means an
    end-to-end test that wants a node to run has to have a seat that starts it.
    This is that seat, and it starts the run the way a live session does —
    through `start_execution`, with the role check, the DAG's preconditions and
    the Execution Service all real. Nothing below the seat is doubled, and no
    scientific decision is scripted here: `start` decides nothing, and `act`
    does nothing, which is exactly what a Worker that has found nothing to say
    does.
    """

    project_id: str
    role: AgentRole
    context: ToolContext
    #: The nodes it was asked to begin, and the live nodes it was handed.
    started: list[str] = field(default_factory=list)
    turns: list[tuple[str, str]] = field(default_factory=list)

    #: The tool each role begins a task through, which is the same act under a
    #: different name: a Worker hands its node to the Execution Service, and
    #: Research takes its node into its own hands. Both refuse a node the DAG
    #: has not cleared, and neither is a decision.
    BEGIN: ClassVar[dict[AgentRole, str]] = {
        AgentRole.COMPUTE_WORKER: "start_execution",
        AgentRole.EXPERIMENTAL_WORKER: "start_execution",
        AgentRole.RESEARCH: "begin_research",
    }

    async def start(self, node: DagNode) -> None:
        self.started.append(node.node_id)
        tool = self.BEGIN[self.role]
        await IMPLEMENTATIONS[tool](self.context)(node_id=node.node_id)

    async def act(self, node: DagNode, situation: Situation) -> None:
        _ = situation
        self.turns.append((node.node_id, node.status.value))

    def close(self) -> None:
        """The seat holds a tool context, not a runtime; nothing to release."""


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
    #: The two Worker seats this project's loop drives, made on first use.
    seats: dict[AgentRole, ScriptedWorker] = field(default_factory=dict)

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
        # The store and the materializers are what `ExecutionRuntime.
        # from_settings` gives a deployment's worker, and a harness worker
        # without them is not a smaller deployment — it is a differently
        # configured one. Both are passed here because this fixture already
        # holds a real store (it hands the same one to the mock backends) and
        # because the alternative is invisible until it is expensive: a
        # contract naming an environment prepares on a deployment and refuses
        # here, and the difference reads as a product fault rather than as a
        # harness that answered for a machine RAVEL does not ship.
        worker = await RunningWorker.start(
            settings,
            registry,
            database,
            store=store,
            materializers=materializers_for(settings),
        )
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

    def compute(self, scenario: str, *, step_seconds: float = 0.2) -> None:
        """Send computation nodes to the real mock, playing a named scenario.

        `step_seconds` is how long the mock holds each state of the scenario, so
        it is what decides when a run finishes: at the default, `COMPUTE_SUCCESS`
        is RUNNING for under half a second. A case about a task that is *still
        in flight* — a seat serving it, a restart happening around it — passes a
        longer step, because "the computation is running" has to still be true
        when the case looks.
        """
        self.registry.register(
            NodeType.COMPUTATION,
            MockComputeBackend(
                database=self.database,
                store=self.store,
                scenario=scenario,
                step_seconds=step_seconds,
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
            compute_worker=self.worker_seat(AgentRole.COMPUTE_WORKER),
            experimental_worker=self.worker_seat(AgentRole.EXPERIMENTAL_WORKER),
            research=self.worker_seat(AgentRole.RESEARCH),
            poll_seconds=poll_seconds,
            max_rounds=max_rounds,
            max_stalled_rounds=max_stalled_rounds,
        )
        return await loop.run()

    def worker_seat(self, role: AgentRole) -> ScriptedWorker:
        """One Worker seat over the real tools, memoized per role.

        Memoized because a seat is per project and per role, and a second one
        for the same project would be a second Worker: the point of the
        `(project, role)` scope is that there is exactly one.
        """
        seat = self.seats.get(role)
        if seat is None:
            seat = ScriptedWorker(
                project_id=self.project.project_id,
                role=role,
                context=ToolContext(
                    scope=ToolScope(project_id=self.project.project_id, role=role),
                    database=self.database,
                    execution=ExecutionService(
                        database=self.database, settings=self.settings
                    ),
                ),
            )
            self.seats[role] = seat
        return seat

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


# ── A Gateway on a real socket ──────────────────────────────────────────────

#: Where the test Gateway listens. Loopback, because a test server reachable
#: from the network is a test server somebody else can reach.
HOST = "127.0.0.1"

#: How long to wait for the server to come up, and for it to go away.
LISTEN_TIMEOUT_SECONDS = 15.0


@dataclass
class ScriptedMasterPort:
    """A `MasterPort` that answers without a model.

    The same bargain this directory makes about `ScriptedMaster`, one layer up:
    `test_tui.py` asks whether the *console* reaches Master through the real
    Gateway, and a DSH runtime would answer that question no better while
    making the answer depend on a network and an API key. What is real here is
    everything between the keypress and the `MasterPort` call — the socket, the
    token, the standing check, the transcript row.

    `heard` is what the port was actually asked, so a test can assert that what
    somebody typed arrived rather than that something did.
    """

    reply: str = "I have the question."
    heard: list[str] = field(default_factory=list)

    async def respond(self, situation: Situation, message: str) -> Answer:
        self.heard.append(message)
        return Answer(text=self.reply, completed=True)


@dataclass
class LiveGateway:
    """The Gateway, listening. Everything a TUI needs is a URL and a port."""

    url: str
    app: FastAPI
    server: uvicorn.Server
    thread: threading.Thread
    master: ScriptedMasterPort

    def stop(self) -> None:
        """Ask the server to exit and wait for the thread to finish.

        `should_exit` rather than killing the thread: uvicorn closes its
        listeners and the event loop on the way out, and a thread that was
        simply abandoned would leave the port bound until the process ended.
        """
        self.server.should_exit = True
        self.thread.join(timeout=LISTEN_TIMEOUT_SECONDS)


def _bound_port(server: uvicorn.Server) -> int:
    """The port uvicorn actually bound.

    Read back from the listening socket rather than chosen in advance. Picking
    a free port and then asking for it leaves a window in which another process
    takes it, and the failure that produces is a test connecting to somebody
    else's server — which is not a failure anybody could diagnose from the
    message.

    Raises:
        AssertionError: if the server reports itself started with nothing
            listening, which would mean the readiness check above was wrong and
            every test using this fixture is about to talk to nothing.
    """
    if not server.servers or not server.servers[0].sockets:
        raise AssertionError("uvicorn reports itself started but is not listening anywhere")
    return int(server.servers[0].sockets[0].getsockname()[1])


@pytest.fixture
def live_gateway(
    database: Database,
    gateway_settings: Settings,
    tokens: TokenService,
) -> Iterator[LiveGateway]:
    """The real application, served over TCP, for one test.

    A real uvicorn rather than `TestClient`, because what `test_tui.py` is
    about is a terminal program talking to a Gateway, and that conversation has
    two halves `TestClient` cannot carry: a socket a WebSocket client can open,
    and a request that travels through an actual server rather than through a
    portal into the same event loop. The extra cost is a thread and a port.

    The cadence is shortened so that a test can watch an event arrive rather
    than serve the deployment's fifteen-second heartbeat. The mechanism is the
    real one; only the clock is a test's, exactly as the integration settings
    shorten the Temporal poll interval.
    """
    master = ScriptedMasterPort()
    application = create_app(
        settings=gateway_settings,
        database=database,
        tokens=tokens,
        master_of=lambda _project_id: master,
        cadence=Cadence(poll_seconds=0.05, heartbeat_seconds=1.0),
    )
    config = uvicorn.Config(
        application,
        host=HOST,
        port=0,
        log_level="warning",
        # The application's lifespan runs, since Phase 11: it starts the
        # Gateway's own heartbeat and records its shutdown, and a test
        # harness that skipped it would be serving an application that is not
        # the one a deployment runs.
        lifespan="on",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="ravel-test-gateway", daemon=True)
    thread.start()

    deadline = time.monotonic() + LISTEN_TIMEOUT_SECONDS
    while time.monotonic() < deadline and not server.started:
        time.sleep(0.01)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=LISTEN_TIMEOUT_SECONDS)
        raise AssertionError("the Gateway did not start listening within the timeout")

    live = LiveGateway(
        url=f"http://{HOST}:{_bound_port(server)}",
        app=application,
        server=server,
        thread=thread,
        master=master,
    )
    try:
        yield live
    finally:
        live.stop()


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
