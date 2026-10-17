"""P11-11: the whole scientific chain, run once, certified end to end.

Phase 11 built the parts of a real project — a source read again by the page it
is on (P11-02), a research result a Master can read back (P11-03), a package
prepared for a bench (P11-04), a computation that runs on real hardware
(P11-05, P11-07), a bench channel a person answers on (P11-06), services that
stay up (P11-10) — and every part has a suite of its own. What no one of those
suites says is what *this* item is: that a project runs through them in
sequence, and that what one leg produces is what the next leg was planned from.

Two cases, and the difference between them is who does the thinking.

**The first is scripted and complete.** Master, Review and the three seats are
scripts; everything under them belongs to the deployment — PostgreSQL, Temporal,
the real materializers, the real `MockComputeBackend`, the real
`HumanLabBackend`, and the real tool handlers, reached through a seat's own
context. One project is planned in two stages: a stage that reads, and a stage
that measures, where the second is planned only *after* the first has ended and
its claim is in the ledger, because the criteria it writes cite that claim.
That ordering is the item. A chain of parts that each pass their own suite can
still fail to join, and this is where the join is asserted: a downstream node
whose acceptance criterion is `LITERATURE_DERIVED` with the evidence row's
identifier in it, a DAG dependency on the node that produced the row, a bench
package built from the contract Master wrote, and an ending that cites the row
it rested on.

**The second is live, and partial.** Five real agents drive one project from its
work order to an ending, with the two things software cannot supply provided by
the case: the Internet, which Research really searches, and a person at the
bench, which the test plays — filing bytes through the function the Gateway's
upload route calls and delivering through the real signal. What it asserts is
the *shape* of the run — an ending with Master's decision behind it, five seats
that took a turn, and a ledger with nothing simulated in it — because which of
A20's endings it reaches is a scientific result and not this file's to fix.

**What is not certified here, and cannot be from this host.** No Slurm host is
configured, so no computation runs on the cluster and the computation leg uses
the mock, whose bytes are marked simulated everywhere they appear. And a human
at a bench is not something a test can schedule, so in both cases the person is
this process. `acceptance/PHASE11_ACCEPTANCE.md` §P11-11 records both as the
item's external blockers, with what each leaves uncovered.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session
from tests.acceptance.phase10_support import (
    project_status,
    seat_scope,
    seat_tool,
    until,
)
from tests.acceptance.test_phase10_live import (
    ENDING_DECISION_FOR,
    LIVE_OBJECTIVE,
    RUN_BUDGET_SECONDS,
    turns_taken,
    until_ended,
    what_it_was_doing,
)
from tests.e2e.conftest import Headless, ScriptedMaster, ScriptedReview, Task
from tests.integration.conftest import a_source
from tests.support.lab import (
    CONDITIONS,
    LIMITS,
    PROCEDURE,
    REQUIREMENTS,
    SAMPLES,
    UPLOADER,
    upload,
)
from tests.support.lab import (
    OUTPUTS as BENCH_OUTPUTS,
)

from ravel.backends.lab import HumanLabBackend
from ravel.backends.mocks import MockComputeBackend
from ravel.config import Settings
from ravel.domain.artifacts import is_simulated
from ravel.domain.contracts import ProjectSuccessContract, ResearchContract
from ravel.domain.dag import DagNode, JoinPolicy
from ravel.domain.enums import (
    ClaimClass,
    Confidence,
    CriterionProvenance,
    DecisionType,
    EvidenceSourceTier,
    NodeStatus,
    NodeType,
    ProjectOutcome,
    ProjectStatus,
    ReviewOutcome,
)
from ravel.domain.lab import LabHandover
from ravel.domain.project import Project, RoadmapPhase
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TERMINAL_PROJECT_STATUSES
from ravel.execution.loop import Situation
from ravel.execution.supervisor import ProjectSupervisor
from ravel.execution.temporal.contracts import ExternalResult
from ravel.master import ENDING_DECISION, MasterService
from ravel.state.database import Database
from ravel.state.repositories.contracts import (
    AcceptanceContractRepository,
    ResearchContractRepository,
)
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.lab import LabHandoverRepository
from ravel.state.repositories.projects import ProjectRegistry, RoadmapRepository
from ravel.state.repositories.records import RecordRepositories
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
    ResearchRecordRepository,
)
from ravel.state.services.dag import DagMutationService, DecisionDraft
from ravel.state.store import S3ArtifactStore

pytestmark = [pytest.mark.phase11, pytest.mark.acceptance, pytest.mark.timeout(3600)]

#: The source the scripted case's reading leg reads. A URL rather than bytes,
#: because what the seat does with a source is what P11-02 certifies; this case
#: is about where the claim it produces ends up.
SOURCE_URL = "https://example.org/articles/niobium-stability"

#: The claim the reading leg records, and the question it is recorded under.
#: Written as a sentence a source could carry, because that is what the ledger
#: holds and what the measuring stage's criteria are derived from.
CLAIM = "Niobium doping at 3 mol% raised conductivity by 18% up to 480 hours."
QUESTION = "Which dopant keeps conductivity above the threshold?"
UNKNOWN = "Nothing above 500 hours."

#: The phrase the scripted Review recognises the reading leg by. A RESEARCH node
#: has no Execution Record — its result is a Research Record — so a script that
#: judges a node from the run it left cannot judge this one, and is given a
#: sentence to recognise instead. Distinctive on purpose: every other node is
#: judged from what it ran, which is the stronger of the two readings.
READING_VERDICT_PHRASE = "published literature"

#: Every body the person files carries this in its first line. The bench is a
#: person RAVEL hands a package to, and nobody has been handed one: the files
#: below are written by the test, so a reader who opens one must not be able to
#: mistake it for a measurement. What these two cases certify about the
#: laboratory leg is the *path* — a file filed against an output the package
#: owed completes the handover, writes an Execution Record, and reaches Review —
#: and L-29 is the part of the bench that no run here can certify.
STUB_MARK = "certification harness stub — not a measurement"


def stubbed(output: str) -> bytes:
    """The body the person files for one output the package owed.

    Shaped by the name, because a person answers a *package*: the live Master
    writes its own `required_outputs` and nothing here knows what they will be
    when this file is written, which is exactly why the delivery is derived from
    the handover rather than from a constant. Every body carries `STUB_MARK`,
    and that line is the whole of what makes a stub legible to whatever reads
    the ledger afterwards.
    """
    if output.lower().endswith(".csv"):
        return f"# {STUB_MARK}\nt_minutes,reading\n0,1.38\n30,1.41\n".encode()
    if output.lower().endswith(".json"):
        return json.dumps(
            {"mark": STUB_MARK, "t_minutes": [0, 30], "reading": [1.38, 1.41]}
        ).encode()
    return (
        f"# {STUB_MARK}\n\n"
        f"Filed against `{output}`, which the package this answers required. The "
        "bytes are the test's: this run has no bench behind it, and the file says "
        "so rather than carrying numbers whose provenance a reader would have to "
        "guess.\n"
    ).encode()

#: How often the person looks at the bench. Tight, because the wait they are
#: answering has a run deadline under it that this case does not set.
BENCH_POLL_SECONDS = 0.05

#: How long the hands may wait for work to reach them. The scripted case is
#: seconds long; the wait is here so that a chain which never reaches a leg
#: fails as that, rather than hanging the suite.
HANDS_BUDGET_SECONDS = 300.0


# ── The scripted Master, and the move the chain is built on ───────────────────


@dataclass
class CertifyingMaster(ScriptedMaster):
    """Master's authority, scripted, planning the second stage from the first.

    `ScriptedMaster` plans one stage and then only answers. That shape cannot
    express what this item certifies, because the move is *planning against what
    the last leg found*: a stage of work whose criteria are derived from a claim
    that did not exist when the project was created, and could not have been
    written before the reading was done.

    So this master plans the reading stage at the start, and the measuring stage
    only once the reading stage has ended — through the same
    `DagMutationService` and `commit_terms` a live Master's tools call, with the
    claim's identifier in the Decision Record that creates the nodes and in the
    provenance of the criteria they are frozen against.
    """

    #: The stage that reads, planned the way `ScriptedMaster` plans one.
    evidence_phase: str = "Evidence"
    #: The stage that measures, planned after the reading has ended.
    measure_phase: str = "Measure"
    #: What the measuring stage consists of: a template rather than a script,
    #: because each task is given the claim's identifier at plan time and there
    #: is nothing to cite until the reading is done.
    downstream: tuple[Task, ...] = ()
    #: The claim the second stage was planned from. Empty until there is one,
    #: and cited by the ending as well: a conclusion that rests on a reading
    #: should say which one.
    cited: tuple[str, ...] = ()
    #: Node identifier per stage, filled in as each stage is expanded. A cell
    #: rather than a value because the measuring stage's nodes name the reading
    #: node as a dependency and the DAG mints that identifier when the reading
    #: stage is written — so the closure that builds them reads it here, after
    #: the plan exists rather than before.
    upstream: dict[str, str] = field(default_factory=dict)
    #: What the second stage was expanded into. The stage is a stage and not a
    #: node, so the record of it is every identifier it minted rather than the
    #: first — a plan that produced one node where two were asked for is the
    #: failure this records the evidence of.
    measured: tuple[str, ...] = ()
    #: What Master read back once the reading had ended, as the readback tool
    #: answered it. The claim crosses from one stage to the next through
    #: Master's own reading of it, so the script reads through that tool rather
    #: than around it: a case that queried the ledger directly would certify a
    #: path nothing in production takes.
    readback: dict[str, Any] | None = None
    #: Whether the measuring stage has been planned yet.
    staged: bool = False

    async def act(self, situation: Situation) -> None:
        """Read the reading back, then take the decision the situation calls for.

        The read is asynchronous and opens its own read-only session — the
        handler does, the way it does over MCP — while the decision is taken
        inside the loop's write transaction. So the read happens first and
        outside it: a Master decides what to plan next *from* a result it has
        already read, not from one it fetches while holding the pen.
        """
        if self.readback is None and situation.nodes and all(
            node.is_terminal for node in situation.nodes
        ):
            self.readback = await self.read_the_reading()
        await super().act(situation)

    async def read_the_reading(self) -> dict[str, Any]:
        """What the reading delivered, through the tool Master reads it with."""
        context = seat_scope(self.database, self.project_id, AgentRole.MASTER)
        read = seat_tool("read_research_result", context)
        return await read(self.reading_node_id)

    def _act(self, session: Session, situation: Situation) -> None:
        """Take the one decision this situation calls for.

        The parent's script with one state added, and the order is the point: an
        escalation is still answered first, a project with no plan is still
        planned, and the ending is still last — but between the reading stage
        ending and the project ending sits the planning of the measuring stage.
        """
        if situation.open_deviations:
            self._answer(session, situation.open_deviations[0].deviation_id)
            return
        if not situation.nodes:
            self._plan_evidence(session, situation)
            return
        if not self.staged:
            # Waiting is a decision too: a stage that has not finished is not
            # something to plan over, and a node still running is not an ending.
            if all(node.is_terminal for node in situation.nodes):
                self._plan_measure(session, situation)
            return
        self._conclude(session, situation)

    def _plan_evidence(self, session: Session, situation: Situation) -> None:
        """Freeze what was asked and what success means, then plan the reading.

        The three acts a Master performs before any work exists, in the order
        the domain requires: what the user wants (`ResearchContract`), what
        would make the project succeed (`ProjectSuccessContract`), and the first
        stage of the roadmap. The node is a RESEARCH one, whose executor is the
        Research seat, so what begins it is that seat's own tool and not a
        script.

        Both contracts go through the call the corresponding tool makes — the
        service for success, the repository the tool wraps for the research
        contract — rather than around it, because a project whose question was
        never written down is a project the rest of this case would be
        certifying anyway.
        """
        if situation.project.status is ProjectStatus.CREATED:
            MasterService(session, self.project_id).define_success(
                ProjectSuccessContract(
                    project_id=self.project_id,
                    success_criteria=(
                        "The measurement agrees with the series the source reports.",
                    ),
                    failure_criteria=("Neither the source nor the bench reports a gain.",),
                    unresolved_uncertainty_policy=(
                        "Conclude inconclusive rather than guess."
                    ),
                ),
                role=AgentRole.MASTER,
            )
            ResearchContractRepository(session, self.project_id).commit(
                ResearchContract(
                    project_id=self.project_id,
                    original_user_goal=(
                        "Find out what the literature reports about niobium-doped "
                        "titanium dioxide, and measure the series it describes."
                    ),
                    scientific_problem=QUESTION,
                    research_hypotheses=(CLAIM,),
                    target_metrics=("conductivity gain >= 15%",),
                    acceptance_strategy=(
                        "Read the literature, then measure the series it reports."
                    ),
                    known_constraints=("Bench time is limited.",),
                    prohibited_actions=("No testing on live reactors.",),
                ),
                role=AgentRole.MASTER,
            )
            self.trace.append("contracts_defined")

        RoadmapRepository(session, self.project_id).register(
            RoadmapPhase(project_id=self.project_id, name=self.evidence_phase, order=0),
            role=AgentRole.MASTER,
        )
        expanded = DagMutationService(session, self.project_id).expand_phase(
            self.evidence_phase,
            [task.node() for task in self.tasks],
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=DecisionType.CREATE_NODE,
                rationale=(
                    "The project opens by reading what is already published about "
                    "the question, before anything is measured."
                ),
                confidence=Confidence.MEDIUM,
            ),
        )
        for task, node in zip(self.tasks, expanded.nodes, strict=True):
            task.commit(session, self.project_id, node)
        self.planned = expanded.nodes
        self.upstream[self.evidence_phase] = expanded.nodes[0].node_id
        self.trace.append("planned_evidence")

    def _plan_measure(self, session: Session, situation: Situation) -> None:
        """Plan the measuring stage against the claim the reading produced.

        What the criteria cite is a *row*, and the row arrives here from the
        readback rather than from a second query: the identifier a reviewer, a
        reviewer's query, and this file's assertions all resolve to is the one
        Master itself was given.
        """
        _ = situation
        assert self.readback is not None, (
            "the measuring stage is being planned without Master having read "
            "what the reading produced, which is the join this item certifies"
        )
        claims = self.readback["claims"]
        assert claims, (
            "Master's readback of the reading stage returned no claim, so there "
            "is nothing for the measuring stage to have been planned from"
        )
        claim = claims[0]
        self.cited = (claim["evidence_id"],)
        tasks = tuple(
            replace(task, criterion_ref=claim["evidence_id"]) for task in self.downstream
        )
        RoadmapRepository(session, self.project_id).register(
            RoadmapPhase(project_id=self.project_id, name=self.measure_phase, order=1),
            role=AgentRole.MASTER,
        )
        expanded = DagMutationService(session, self.project_id).expand_phase(
            self.measure_phase,
            [task.node() for task in tasks],
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=DecisionType.CREATE_NODE,
                rationale=(
                    f"The reading recorded {claim['statement']!r} against "
                    f"{len(claim['source_refs'])} source(s). The work that "
                    "measures the series is planned against that claim, and its "
                    "criteria carry the claim's identifier."
                ),
                confidence=Confidence.MEDIUM,
                evidence_refs=(claim["evidence_id"],),
            ),
        )
        for task, node in zip(tasks, expanded.nodes, strict=True):
            task.commit(session, self.project_id, node)
        self.planned = (*self.planned, *expanded.nodes)
        self.upstream[self.measure_phase] = expanded.nodes[0].node_id
        self.measured = tuple(node.node_id for node in expanded.nodes)
        self.staged = True
        self.trace.append("planned_measure")

    @property
    def reading_node_id(self) -> str:
        assert self.planned, "the reading stage has not been planned"
        return self.planned[0].node_id

    def _conclude(self, session: Session, situation: Situation) -> None:
        """End the project, and say what the ending rests on.

        The parent's ending with one field filled in. A conclusion is a
        scientific claim like any other, and the Decision Record behind it is
        where such a claim's provenance belongs: an ending that cites nothing is
        an ending no later reader can check against the ledger.
        """
        _ = situation
        MasterService(session, self.project_id).conclude(
            self.outcome,
            role=AgentRole.MASTER,
            decision=DecisionDraft(
                decision_type=ENDING_DECISION[self.outcome],
                rationale=(
                    "Every node has ended. The reading delivered a claim and the "
                    "measurement ran against it, so this is what they came to."
                ),
                confidence=Confidence.MEDIUM,
                evidence_refs=self.cited,
            ),
            reason="Nothing is left to run.",
        )
        self.trace.append(f"concluded:{self.outcome.value}")


# ── The two things that are not software ─────────────────────────────────────


def node_status(headless: Headless, node_id: str) -> NodeStatus:
    """One node's status, read fresh from PostgreSQL."""
    with headless.database.read_only() as session:
        return DagRepository(session, headless.project.project_id).node(node_id).status


def the_reading_node(headless: Headless) -> DagNode | None:
    """The project's RESEARCH node, once the DAG has one.

    Found rather than passed in, because the node does not exist until Master
    plans it — and the identifier the DAG mints is not something a case can
    predict. This is what a seat does too: a session is convened for a project
    and finds the work it is there for.
    """
    with headless.database.read_only() as session:
        nodes = DagRepository(session, headless.project.project_id).nodes()
    reading = [node for node in nodes if node.node_type is NodeType.RESEARCH]
    return reading[0] if reading else None


def waiting_handover(headless: Headless) -> LabHandover | None:
    """The work a bench is holding, if any, read from the handover table."""
    with headless.database.read_only() as session:
        waiting = LabHandoverRepository(session, headless.project.project_id).waiting()
    return waiting[0] if waiting else None


async def the_reading(headless: Headless, *, source_id: str) -> None:
    """The model behind the Research seat, for the length of one node.

    A RESEARCH node's work *is* the reading, done in the seat's own session:
    there is no run to hand to a backend and no workflow behind it, which is why
    `begin_research` moves the node and returns. So a scripted run has to supply
    the reading, and this supplies it the way a session does — through the
    seat's own context and the real handlers, writing an Evidence row that rests
    on the registered source, then handing the node over.
    """
    node_id = ""
    deadline = time.monotonic() + HANDS_BUDGET_SECONDS
    while time.monotonic() < deadline and node_id == "":
        found = the_reading_node(headless)
        if found is not None:
            node_id = found.node_id
        else:
            await asyncio.sleep(BENCH_POLL_SECONDS)
    assert node_id, (
        "no RESEARCH node was planned within "
        f"{HANDS_BUDGET_SECONDS:.0f}s, so the reading this case is about never began"
    )

    await until(
        lambda: node_status(headless, node_id) is NodeStatus.RUNNING,
        timeout=HANDS_BUDGET_SECONDS,
    )
    context = seat_scope(
        headless.database, headless.project.project_id, AgentRole.RESEARCH
    )
    record_evidence = seat_tool("record_evidence", context)
    submit = seat_tool("submit_research_record", context)
    await record_evidence(
        node_id=node_id,
        statement=CLAIM,
        claim_class=ClaimClass.FACT.value,
        source_refs=[source_id],
    )
    await submit(
        node_id=node_id,
        question=QUESTION,
        recommended_followups=["Measure the 5 mol% series as well."],
        unknowns=[UNKNOWN],
        report="One source read; one claim recorded against it.",
    )


async def the_bench(headless: Headless, *, budget: float = HANDS_BUDGET_SECONDS) -> None:
    """The person at the bench, for the length of one run.

    A laboratory run is handed to somebody outside RAVEL and waits for them, and
    the only way a test can be on the far side of that wait is to *be* the
    person. This waits for a handover to appear, files the bytes the package
    asked for through the function the Gateway's upload route calls, delivers
    through the real signal, and leaves — a bench that has answered once has
    nothing else to do.

    It decides nothing, and it answers the package rather than a script: the
    outputs it files are the ones the handover RAVEL wrote required, one body
    each, and the delivery reports those names and no more. The live Master
    writes its own `required_outputs` and there is no way for this process to
    know them in advance — a person who filed a fixed pair of names at a package
    that asked for others would be answering nothing, and the run would sit on
    its external deadline until it timed out. The bodies say what they are
    (`STUB_MARK`) because no bench stands behind them.
    """
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        handover = waiting_handover(headless)
        if handover is not None:
            owed = handover.required_outputs
            for output in owed:
                upload(
                    headless.database,
                    headless.store,
                    handover,
                    output,
                    stubbed(output),
                    by=UPLOADER,
                )
            await headless.client.deliver_external_result(
                node_id=handover.node_id,
                execution_contract_version=handover.execution_contract_version,
                result=ExternalResult(
                    summary=(
                        f"The bench answered all {len(owed)} output(s) the package "
                        f"owed. {STUB_MARK}."
                    ),
                    delivered_outputs=tuple(owed),
                ),
            )
            return
        if project_status(headless.database, headless.project.project_id) in (
            TERMINAL_PROJECT_STATUSES
        ):
            return
        await asyncio.sleep(BENCH_POLL_SECONDS)
    raise AssertionError(
        f"the bench waited {budget:.0f}s for work and was never handed any, with "
        "the project still running: a run whose plan holds an experiment and "
        "never reaches the bench is the failure this case is about"
    )


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def project(database: Database, clean: None) -> Project:
    """The certification's project, opened as the work order it answers.

    Defined here rather than taken from the shared fixture because the objective
    *is* an input to a live Master: a project described as "find a better dopant"
    would be planned however the model felt that morning, and this file is about
    a chain rather than about one plan.
    """
    with database.transaction() as session:
        return ProjectRegistry(session).create(
            title="Dopant screen, certified",
            objective=LIVE_OBJECTIVE,
            created_by="phase11-certification",
        )


@pytest.fixture(scope="module")
def credential() -> str:
    """The model credential a live turn needs, or a skip."""
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    message = (
        "DEEPSEEK_API_KEY is unset, so no live agent turn can run. Export it, or "
        "set RAVEL_REQUIRE_DSH=1 to make this a failure rather than a skip."
    )
    if os.environ.get("RAVEL_REQUIRE_DSH"):
        pytest.fail(message)
    pytest.skip(message)


# ── P11-11, first case: the whole chain, scripted ─────────────────────────────


async def test_p11_11_one_project_runs_the_whole_chain_to_a_conclusion(
    headless: Headless,
    database: Database,
    artifact_store: S3ArtifactStore,
) -> None:
    """The chain, once, with every leg joined to the one before it.

    What is asserted is not that each leg works — six suites say that — but what
    crosses between them. The claim the reading leg records is the row the
    measuring stage's criteria cite and the row the ending cites; the bench is
    handed the package Master's contract describes and the person's bytes come
    back as an Execution Record; the computation runs on the mock and its bytes
    are marked simulated everywhere they appear; and no simulated byte is in the
    ledger. A chain of parts that each pass their own suite can still fail to
    join, and this is the case that would catch it.
    """
    headless.compute("COMPUTE_SUCCESS")
    headless.registry.register(NodeType.EXPERIMENT, HumanLabBackend(database=database))
    project_id = headless.project.project_id
    registered = a_source(
        database, project_id, url=SOURCE_URL, tier=EvidenceSourceTier.B
    )

    # The measuring stage's nodes depend on the reading node, which does not
    # exist until the reading stage is expanded — so the closure that builds
    # them reads the identifier out of the master's `upstream` cell, which is
    # filled before the measuring stage is planned. A dependency is the DAG's
    # own record of where work came from, and it is immutable once written,
    # which is why it is stated at creation or not at all.
    upstream: dict[str, str] = {}

    def downstream_node(objective: str, node_type: NodeType) -> DagNode:
        return DagNode.create(
            project_id=project_id,
            node_type=node_type,
            objective=objective,
            created_by=AgentRole.MASTER.value,
            dependencies=(upstream["Evidence"],),
            join_policy=JoinPolicy.ALL,
        )

    reading = Task(
        build=lambda: DagNode.create(
            project_id=project_id,
            node_type=NodeType.RESEARCH,
            objective=(
                "Report what the published literature says about niobium-doped "
                "titanium dioxide, naming the source actually read."
            ),
            created_by=AgentRole.MASTER.value,
        ),
        criteria=(),
        allowed_actions=("read_source",),
        required_outputs=("reading_notes.md",),
    )
    computation = Task(
        build=lambda: downstream_node(
            "Compute the conductivity of the doped series at 300 K.",
            NodeType.COMPUTATION,
        ),
        criteria=("The series reproduces the gain the source reports.",),
        allowed_actions=("run_calculation",),
        required_outputs=("conductivity.csv", "notes.json"),
        criterion_provenance=CriterionProvenance.LITERATURE_DERIVED,
    )
    measurement = Task(
        build=lambda: downstream_node(
            "Measure undoped TiO2 at 300 K on the bench.", NodeType.EXPERIMENT
        ),
        criteria=("The measurement reports a value with an uncertainty.",),
        allowed_actions=("run_measurement",),
        required_outputs=BENCH_OUTPUTS,
        criterion_provenance=CriterionProvenance.LITERATURE_DERIVED,
        # The terms a bench package is built from. `LabMaterializer` refuses a
        # contract that states no procedure, no samples, no conditions and no
        # outputs, because filling any of them in would be software making a
        # scientific decision — so these are the laboratory support module's own
        # terms, the vocabulary that materializer is certified against.
        procedure=PROCEDURE,
        inputs=SAMPLES,
        parameter_targets=CONDITIONS,
        resource_limits=LIMITS,
        execution_requirements=REQUIREMENTS,
    )

    master = CertifyingMaster(
        database=database,
        project_id=project_id,
        tasks=(reading,),
        downstream=(computation, measurement),
        upstream=upstream,
        outcome=ProjectOutcome.SUCCESS,
    )
    review = ScriptedReview(
        database=database,
        project_id=project_id,
        verdicts={READING_VERDICT_PHRASE: ReviewOutcome.PASS},
    )

    hands = asyncio.create_task(the_reading(headless, source_id=registered.source_id))
    bench = asyncio.create_task(the_bench(headless))
    try:
        run = await headless.drive(master, review)
        await asyncio.wait_for(hands, timeout=60.0)
        await asyncio.wait_for(bench, timeout=60.0)
    finally:
        for task in (hands, bench):
            if not task.done():  # pragma: no cover - a chain that hung
                task.cancel()

    assert run.finished, (
        f"the loop stopped with the project still {run.status.value} after "
        f"{run.rounds} rounds, so the chain did not close.\n"
        f"What it was doing:\n{what_it_was_doing(database, project_id)}"
    )
    assert run.status is ProjectStatus.COMPLETED
    assert master.trace == [
        "contracts_defined",
        "planned_evidence",
        "planned_measure",
        "concluded:SUCCESS",
    ], (
        "Master did not do the four things the chain is: freeze what was asked "
        f"and what success means, read, measure against the reading, conclude — {master.trace}"
    )

    with database.read_only() as session:
        nodes = {
            node.node_id: node
            for node in DagRepository(session, project_id).nodes()
        }
        records = RecordRepositories(session, project_id)
        decisions = records.decisions.all()
        reviews = records.reviews.all()
        claims = EvidenceRepository(session, project_id).for_task(master.reading_node_id)
        sources = EvidenceSourceRepository(session, project_id).readable()
        artifacts = {
            artifact.artifact_id: artifact
            for artifact in ArtifactRepository(session, project_id).all()
        }
        handovers = LabHandoverRepository(session, project_id).all()
        # Asked inside the block, like every other read here. A repository kept
        # past the block begins a second transaction on a session whose
        # connection was already returned, and nothing returns that one: it sits
        # `idle in transaction` holding a read lock until the next test's
        # TRUNCATE times out. This case did exactly that, and the full matrix —
        # which runs the live case straight after this one — is what caught it.
        research_records = ResearchRecordRepository(session, project_id).for_node(
            master.reading_node_id
        )
        # One node's runs, keyed by node, for the same reason.
        executions = {
            node_id: records.executions.for_node(node_id) for node_id in nodes
        }
        criteria = {
            node_id: AcceptanceContractRepository(session, project_id).frozen_for_node(
                node_id
            )
            for node_id in nodes
        }

    reading_node = nodes[master.reading_node_id]
    measured = [nodes[node_id] for node_id in master.measured]

    # ── Who wrote the plan ───────────────────────────────────────────────────
    # Every node in the project names Master as its creator, which is the
    # narrowest form of the claim the whole architecture rests on: no seat
    # writes to the DAG. A worker may widen its own contract by asking and
    # cannot touch the plan; the columns that say so are on the nodes.
    assert {node.created_by for node in nodes.values()} == {AgentRole.MASTER.value}, (
        "a node in the plan was not created by Master, so something else wrote "
        f"to the DAG: {sorted({node.created_by for node in nodes.values()})}"
    )
    assert {decision.authority_check.actor_role for decision in decisions} == {
        AgentRole.MASTER.value
    }, "a decision was taken by something other than Master"
    assert all(decision.authority_check.permitted for decision in decisions), (
        "a decision was recorded as one Master was not permitted to take"
    )
    attributed = {
        node_id
        for decision in decisions
        if decision.decision_type is DecisionType.CREATE_NODE
        for node_id in decision.affected_nodes.created
    }
    assert set(nodes) <= attributed, (
        "a node is in the plan with no decision that created it, so the plan "
        f"holds work nothing authorized: {sorted(set(nodes) - attributed)}"
    )

    # ── The reading leg ──────────────────────────────────────────────────────
    assert reading_node.status is NodeStatus.PASSED, (
        f"{reading_node.display_id} ended {reading_node.status.value}: the "
        "reading leg did not produce a result the Review would accept"
    )
    assert [claim.statement for claim in claims] == [CLAIM], (
        "the ledger does not hold the claim the reading seat recorded"
    )
    assert claims[0].source_refs == (registered.source_id,), (
        "the claim does not cite the source it was read from"
    )
    assert [source.source_id for source in sources] == [registered.source_id], (
        "the project's readable sources are not exactly the one the seat read"
    )
    assert research_records, (
        "the reading node has no Research Record, so nothing was handed over"
    )

    # Master's own reading of that leg, against the ledger it read from: the
    # claim the measuring stage was planned from is the row a second reader
    # finds, and the record Master was told about is the one the seat filed.
    readback = master.readback
    assert readback is not None and readback["has_result"], (
        "Master read the reading stage back and found no Research Record in it, "
        "so the handover P11-03 certifies did not happen in this run"
    )
    assert readback["node"]["status"] == NodeStatus.PASSED.value, (
        "the reading stage was read back before a verdict had passed it"
    )
    assert [claim["evidence_id"] for claim in readback["claims"]] == [
        claims[0].evidence_id
    ], (
        "what Master read back through its own tool is not what the ledger holds: "
        "the join would rest on two different readings of the same leg"
    )

    # ── The join ─────────────────────────────────────────────────────────────
    assert len(measured) == 2, (
        f"the measuring stage is {len(measured)} node(s) rather than two: the "
        "second stage did not get planned from the first"
    )
    for node in measured:
        assert node.dependencies == (reading_node.node_id,), (
            f"{node.display_id} does not depend on the node the reading was done "
            "on, so the DAG does not record where its criteria came from"
        )
        assert node.join_policy is JoinPolicy.ALL
        frozen = criteria[node.node_id]
        assert frozen is not None, f"{node.display_id} was never frozen against criteria"
        assert [criterion.provenance for criterion in frozen.criteria] == [
            CriterionProvenance.LITERATURE_DERIVED
        ], (
            f"{node.display_id}'s criterion does not record that it came from the "
            "literature, so a later reader cannot tell it from a requirement"
        )
        assert [criterion.provenance_ref for criterion in frozen.criteria] == [
            claims[0].evidence_id
        ], (
            f"{node.display_id}'s criterion does not cite the claim it was derived "
            "from, which is the join this item certifies"
        )

    branching = [
        decision for decision in decisions if decision.evidence_refs
    ]
    assert [decision.evidence_refs for decision in branching] == [
        (claims[0].evidence_id,),
        (claims[0].evidence_id,),
    ], (
        "the decisions that planned the measuring stage and ended the project do "
        "not both cite the claim they rest on"
    )
    assert ENDING_DECISION_FOR[run.status] in {
        decision.decision_type for decision in decisions
    }, "the project ended without a Decision Record of the kind its status means"

    # ── The measuring legs ───────────────────────────────────────────────────
    assert reviews, "the run reached an ending no Review verdict had a part in"

    bench_node = next(node for node in measured if node.node_type is NodeType.EXPERIMENT)
    (bench_execution,) = executions[bench_node.node_id]
    assert bench_execution.backend == HumanLabBackend.name, (
        "the laboratory leg did not run on the bench backend, so no person was "
        "ever handed it"
    )
    assert bench_execution.delivery_is_complete, (
        "the bench's delivery was recorded as short, so the person's answer did "
        "not cover what the contract required"
    )
    delivered = [artifacts[ref] for ref in bench_execution.output_refs]
    assert [artifact.created_by for artifact in delivered] == [UPLOADER, UPLOADER], (
        "the record does not say who filed what the bench produced"
    )
    assert len(handovers) == 1, "the run handed work to a bench twice, or never did"
    assert handovers[0].required_outputs == BENCH_OUTPUTS, (
        "the package owed something other than what the contract required"
    )

    compute_node = next(
        node for node in measured if node.node_type is NodeType.COMPUTATION
    )
    (compute_execution,) = executions[compute_node.node_id]
    simulated = [artifacts[ref] for ref in compute_execution.output_refs]
    assert simulated, "the computation ran and left no artifacts behind"
    assert all(is_simulated(artifact.kind) for artifact in simulated), (
        "the mock's output is not marked simulated, so a later reader cannot "
        "tell it from a measurement"
    )
    assert {artifact.artifact_id for artifact in simulated}.isdisjoint(
        {ref for claim in claims for ref in claim.source_refs}
    ), "a simulated artifact is cited as the source of a claim in the ledger"


# ── P11-11, second case: the whole chain, live ────────────────────────────────


@pytest.mark.live
async def test_p11_11_live_agents_drive_the_whole_chain_to_a_conclusion(
    headless: Headless,
    credential: str,
    live_settings: Settings,
    tmp_path: Path,
) -> None:
    """P11-11's certification half: five real agents, a person, one ending.

    The supervisor is pointed at a database and nothing else. It discovers the
    project, builds five seats from the pinned harness, and drives the loop until
    the project ends; every turn is a real model call against a real tool server,
    the research leg reaches the real Internet, and the laboratory leg — if the
    plan reaches one — is answered by this process playing the person at the
    bench.

    What is asserted is the shape of the run, not one ending: the project reached
    one of A20's four endings, every seat took at least one live turn, the plan
    held all three kinds of work, and no simulated byte was admitted as evidence.
    Which ending it is belongs to the science. A run that ended FAILED with a
    verdict behind it has demonstrated as much about the architecture as one that
    ended COMPLETED, and a case that demanded COMPLETED would be grading the
    model's science rather than RAVEL's chain.

    The laboratory leg is conditional for the same reason: whether the plan
    reaches a bench is a decision the live Master makes from its contract, and a
    Master that declines to commit an experiment has decided something correct.
    What is not conditional is that a bench which *is* handed work is answered —
    a project that ended with a handover still open is the finding, and the
    assertion below is written to catch exactly that.

    The computation leg is conditional the same way, and the condition is the
    deployment's rather than the model's: this host prepares RASPA inputs and
    bench packages and nothing else, and the RASPA materializer will not choose
    a simulation's parameters, so a Master whose deliverable is a calculation it
    cannot express as a RASPA run re-commits it as something that can run. What
    that costs is one thing this case therefore does not certify — that a *live*
    run's mock artifacts are marked — and the scripted case is where it is
    certified, on a run the mock really did. The docstring says so rather than
    letting a green live run stand for more than it covers.
    """
    headless.compute("COMPUTE_SUCCESS")
    headless.registry.register(
        NodeType.EXPERIMENT, HumanLabBackend(database=headless.database)
    )
    project_id = headless.project.project_id

    # The runtimes live in a temporary root: an unattended run leaves a working
    # directory for every scope it started, and a test should not add four of
    # them to the checkout. The contact address is carried over from the live
    # settings because RAVEL refuses to fetch anonymously, and a research seat
    # launched without one would fail on the configuration rather than on the
    # science.
    settings = headless.settings.model_copy(
        update={
            "runtime_dir": tmp_path / "runtime",
            "research_contact_email": live_settings.research_contact_email,
        }
    )
    supervisor = ProjectSupervisor(
        database=headless.database,
        settings=settings,
        poll_seconds=1.0,
        loop_poll_seconds=0.5,
        loop_max_rounds=80,
    )

    person = asyncio.create_task(
        the_bench(headless, budget=RUN_BUDGET_SECONDS), name="phase11-bench-person"
    )
    task = asyncio.create_task(supervisor.run(), name="phase11-certification-run")
    try:
        status = await until_ended(
            headless.database, project_id, budget=RUN_BUDGET_SECONDS
        )
        turns = turns_taken(supervisor, project_id)
    finally:
        supervisor.stop()
        try:
            await asyncio.wait_for(task, timeout=120.0)
        except TimeoutError:  # pragma: no cover - a supervisor that will not stop
            task.cancel()
            raise
        person.cancel()
        # Cancelled, then collected. A person who gave up on a project that was
        # still running has already failed this case, and a task that raised and
        # was never awaited would have its exception dropped on the floor — the
        # bench is the half of this run no assertion can reach from here.
        with contextlib.suppress(asyncio.CancelledError):
            await person

    assert status in TERMINAL_PROJECT_STATUSES, status
    silent = [role for role, count in turns.items() if count == 0]
    assert not silent, (
        f"the project reached {status.value} without these seats ever taking a "
        f"live turn: {silent}; every seat is a real agent, and one the run never "
        f"reached is a seat the architecture does not have. Turns taken: {turns}.\n"
        f"What it was doing at the end:\n{what_it_was_doing(headless.database, project_id)}"
    )

    with headless.database.read_only() as session:
        nodes = DagRepository(session, project_id).nodes()
        records = RecordRepositories(session, project_id)
        decisions = records.decisions.all()
        reviews = records.reviews.all()
        executions = records.executions.all()
        claims = EvidenceRepository(session, project_id).facts()
        sources = EvidenceSourceRepository(session, project_id).readable()
        handovers = LabHandoverRepository(session, project_id).all()
        artifacts = {
            artifact.artifact_id: artifact
            for artifact in ArtifactRepository(session, project_id).all()
        }

    assert nodes, "the project ended with no plan, so nobody planned anything"
    assert decisions, (
        "a project that ended without a Decision Record ended without Master "
        "having decided anything, which is not an ending RAVEL can produce"
    )
    assert reviews, "the run reached an ending no Review verdict had a part in"
    assert executions, (
        "the project ended without a single Execution Record: no work was run, so "
        "the Workers drove a plan nobody executed"
    )
    assert {node.node_type for node in nodes} >= {
        NodeType.RESEARCH,
        NodeType.COMPUTATION,
        NodeType.EXPERIMENT,
    }, (
        "the work order asked for the literature, for the series to be computed "
        "and for the undoped value to be measured, and the plan has no bench of "
        f"one of those kinds in it: {sorted(node.node_type.value for node in nodes)}"
    )
    assert ENDING_DECISION_FOR[status] in {
        decision.decision_type for decision in decisions
    }, (
        f"the project ended {status.value} without the Decision Record that "
        "ending means, so nothing formal says what it was decided to be"
    )

    # Only the research leg writes to the ledger, and only from bytes it read:
    # every source a claim rests on was reached, hashed, and stored. A source
    # whose retrieval failed is not in this list, which is the point — the
    # ledger holds what was read rather than what was attempted.
    assert all(source.content_hash and source.snapshot_ref for source in sources), (
        "a source in the ledger has no hash or no stored snapshot, so nothing "
        "behind it can be read again"
    )
    cited = {ref for claim in claims for ref in claim.source_refs}
    if claims:
        assert cited <= {source.source_id for source in sources}, (
            "a claim cites a source that was never successfully read"
        )

    # And the mock's bytes stayed out of it. Two statements, and they are not
    # one statement: everything this run computed it computed on the mock, and
    # everything the mock wrote is marked simulated; and nothing simulated is
    # cited as the ground of a claim, which has to hold whether or not the run
    # computed anything at all.
    #
    # Whether a live plan *reaches* a computation is not this case's to require,
    # and the first of the two is therefore asserted over the runs that exist
    # rather than over the run that might have. This deployment has one software
    # materializer and it is RASPA's, which refuses to choose a simulation's
    # parameters rather than inventing them; a Master that has to compute
    # something it cannot express as a RASPA run re-commits the deliverable in
    # another form, and the record of this item's live run shows exactly that —
    # `software/python` refused for having no materializer, `software/raspa`
    # refused for naming no temperature or pressure or cycles, and the work
    # re-committed as a research node. The marking itself is certified on the
    # scripted case above, where the mock really did the work.
    compute_nodes = {
        node.node_id for node in nodes if node.node_type is NodeType.COMPUTATION
    }
    computed = [
        execution for execution in executions if execution.node_id in compute_nodes
    ]
    for execution in computed:
        assert execution.backend == MockComputeBackend.name, (
            f"a computation ran on {execution.backend!r}, and this case's premise "
            "is that the computation leg runs on the mock — this harness registers "
            "that one and asks it for a scenario. A real compute backend wired in "
            "here means this case has to be re-read rather than passed"
        )
        produced = [artifacts[ref] for ref in execution.output_refs]
        assert produced and all(is_simulated(one.kind) for one in produced), (
            "the run used the mock compute backend and left none of its output "
            "marked simulated, so a reader cannot tell what was simulated"
        )
    simulated = {
        artifact.artifact_id
        for artifact in artifacts.values()
        if is_simulated(artifact.kind)
    }
    assert simulated.isdisjoint(cited), (
        "a simulated artifact is cited as the source of a claim in the ledger, "
        "which is the one thing the ledger must never hold"
    )

    unfinished = [handover for handover in handovers if not handover.is_terminal]
    assert not unfinished, (
        f"the project ended with {len(unfinished)} bench handover(s) still open: "
        "RAVEL gave a person work and then ended the project while they held it, "
        "and a person cannot be asked for something a closed project will not read"
    )
