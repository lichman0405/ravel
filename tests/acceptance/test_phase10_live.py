"""The two Phase 10 items that cannot be shown with anything scripted.

Seventeen of Phase 10's twenty items are about structure — who holds which tool,
which process survives which death, what a record says — and structure can be
demonstrated with the model's seats filled by scripts, which is what the rest of
this directory does. Two items are not like that:

- **P10-17** asks whether the *whole thing* works when every seat is a live
  agent. The supervisor discovers the project, Master plans it, a Worker starts
  the work, the mock backend produces it, Review judges it, and Master ends it —
  with no script anywhere and nobody at a keyboard. A version of this with
  scripted seats would be a test of the scripts. It is two cases, because that
  question has two halves that a live model can answer differently: a
  certification run whose objective asks for work, where every seat must be
  reached, and a research-driven run whose objective asks a question, where the
  ending belongs to the science and only the run's record is asserted.
- **P10-18** asks whether Research is still real now that four other agents
  have been wired in beside it. The tempting shortcut — a canned page — would
  satisfy every assertion about the *shape* of an Evidence Ledger row while
  proving that RAVEL can write a row, which was never in doubt.

Both cost money and network, so both are marked `live` and skip without
`DEEPSEEK_API_KEY`. `RAVEL_REQUIRE_DSH=1` turns that skip into a failure, which
is how a release gate runs them: an item that passes by not running is the one
failure this directory exists to make visible.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import pytest
from tests.dsh.mcp_probe import ProbeResult, ToolCall, probe
from tests.e2e.conftest import Headless
from tests.integration.conftest import Prepared
from tests.integration.roles.conftest import RoleEnvironment

from ravel.config import Settings
from ravel.domain.artifacts import is_simulated
from ravel.domain.enums import AccessStatus, DecisionType, NodeStatus, NodeType, ProjectStatus
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import TERMINAL_PROJECT_STATUSES
from ravel.execution.supervisor import ProjectSupervisor
from ravel.research.gateway import SNAPSHOT_KIND
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import RecordRepositories
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceSourceRepository,
    ResearchRecordRepository,
)
from ravel.state.store import ArtifactStore, S3ArtifactStore

pytestmark = [pytest.mark.phase10, pytest.mark.live, pytest.mark.timeout(3600)]

#: The five seats, in the order `docs/02` lists them. Every one of them has to
#: take a live turn for P10-17 to mean what it says.
SEATS = (
    AgentRole.MASTER,
    AgentRole.REVIEW,
    AgentRole.COMPUTE_WORKER,
    AgentRole.EXPERIMENTAL_WORKER,
    AgentRole.RESEARCH,
)

#: What the certification's requester asks for: three pieces of work, named,
#: each with the input it needs, and none of them waiting on another. Master
#: decides the plan — that is the point of a live Master — so this says what the
#: requester wants and nothing about how to stage it, which seat does what, or
#: in what order.
#:
#: What it does *not* do is leave the plan's shape to something the run might
#: answer "not yet", and that is the whole reason it is a work order rather than
#: a question. Two earlier wordings were questions, and each failed this item for
#: a different reason.
#:
#: The first made the laboratory half wait on the computational half — "compute
#: the candidate series first, then confirm the leading candidate with a
#: laboratory measurement" — which put the fifth seat out of reach by
#: construction. Under V0's mock backends a passing computation is not something
#: a live Review will give: it reads `kind: simulated`, `provenance:
#: mock-compute:COMPUTE_SUCCESS` and the note saying the output is not admissible
#: as evidence, and it fails the node on every criterion written against real
#: work. That verdict is correct and the marking is the point of the mock
#: backends. The consequence is what matters: no candidate is ever nominated, so
#: an experiment to confirm one would measure nothing, so a live Master declines
#: to commit one and says so in writing, and the item fails on a plan that was
#: *right*.
#:
#: The second stated the two benches as independent deliverables and left the
#: question in place — "establish whether niobium doping raises the conductivity
#: of TiO2 by at least 15% ... I will put the two together myself". A live Master
#: then did something no wording can forbid and no assertion can call wrong: it
#: planned the literature first, read two research records that reported the
#: protocols could not be fixed from what was retrievable, and concluded
#: INCONCLUSIVE — a correct scientific ending, written up at length, for a
#: question whose evidence did not settle it. The run had reached three seats.
#:
#: The difference between the two failures is the difference between a question
#: and a work order, and it is not about how the plan is staged. A question has
#: one deliverable — the answer — so a run that cannot produce it is *finished*,
#: and ending there is right. A work order has three, so stopping after the first
#: is an incomplete delivery rather than a result. Nothing a bench needs in order
#: to start is left to Research's findings: the composition grid and the
#: reference value the computation needs, and the specimen and conditions the
#: measurement needs, are all stated here, so no bench waits on what the
#: literature did or did not yield.
#:
#: Stating an input is not the same as stating a premise, and saying so once was
#: not enough. An earlier wording described the specimen as
#: "characterised" without saying that it already was, and a live Master read the
#: word as a precondition nobody had established: "Deliverable (3) needs a
#: physical specimen and an instrument, and the project has no evidence yet that
#: either exists. Committing a measurement node blind would either sit
#: unexecutable in the plan or invite a value to be filled in from literature."
#: It then spent a research task establishing availability and concluded
#: INCONCLUSIVE with four nodes and no experimental seat — a careful reading of a
#: work order that never said the bench was ready. A requester who has a bench
#: says so, so the objective now does, and says as well that no deliverable is
#: gated on another's findings.
LIVE_OBJECTIVE = (
    "I need three pieces of work done, reported separately; none of them depends "
    "on another and each is worth having on its own. (1) Check what the published "
    "literature says about niobium-doped titanium dioxide and report it, naming "
    "the sources you actually opened. (2) Compute the electrical conductivity of "
    "the Nb_xTi_(1-x)O2 series for x = 0.00, 0.05 and 0.10 at 300 K, taking "
    "1.0 S/cm as the undoped reference point of the series. (3) Measure the "
    "electrical conductivity of undoped TiO2 at 300 K in the laboratory, with an "
    "uncertainty, on a characterised undoped specimen. The bench is ready for "
    "this — the specimen is characterised and in hand and the instrument is "
    "available — so treat that as given rather than something to establish "
    "first. Do not hold one of these back until another is finished, do not gate "
    "one of them on what another turns up, and do not treat a partial answer to "
    "any of them as a reason to stop."
)

#: The objective of the case that lets the run end wherever its evidence points:
#: a question, with no work order in it and no deliverable promised. This is the
#: commoner shape of a real project, and it is the shape the certification above
#: deliberately is not.
RESEARCH_OBJECTIVE = (
    "Establish whether niobium doping raises the conductivity of titanium dioxide "
    "by at least 15% over the undoped baseline, and say what the comparison rests on."
)

#: The record a node's work leaves behind, by the seat that does the work. A
#: node's type decides who may do it — `NODE_EXECUTOR`, in the domain — and each
#: of those seats reports through exactly one kind of record, so the record a
#: node has is what says which seat carried it out.
WORK_RECORDS: dict[NodeType, str] = {
    NodeType.RESEARCH: "research",
    NodeType.COMPUTATION: "execution",
    NodeType.EXPERIMENT: "execution",
}

#: The statuses a node reaches only after its work was delivered and judged: a
#: verdict is written against a delivery, so a node in one of these had work
#: done on it by somebody.
CARRIED_OUT: frozenset[NodeStatus] = frozenset(
    {NodeStatus.PASSED, NodeStatus.FAILED, NodeStatus.PARTIAL}
)

#: The Decision a project's own ending is recorded as, by the status it reaches.
#: Every ending goes through `conclude_project`, which writes one of these, so
#: the pairing is exact: a project in one of these statuses has that Decision
#: behind it, or it did not end the way its status says.
ENDING_DECISION_FOR: dict[ProjectStatus, DecisionType] = {
    ProjectStatus.COMPLETED: DecisionType.ACCEPT_RESULT,
    ProjectStatus.FAILED: DecisionType.REJECT_RESULT,
    ProjectStatus.INCONCLUSIVE: DecisionType.CONCLUDE_INCONCLUSIVE,
    ProjectStatus.CANCELLED: DecisionType.TERMINATE_PROJECT,
}

#: How long the whole autonomous run may take. Generous: every round that needs
#: a decision is a real model turn, and the cost of a slow one is a wait rather
#: than a wrong answer.
RUN_BUDGET_SECONDS = 2700.0

#: How often the wait looks at the project. The run is minutes long; a tight
#: poll would only make the failure arrive at the same moment.
POLL_SECONDS = 2.0

#: A source RAVEL can read without a subscription, for P10-18. arXiv is open
#: access by definition, so a refusal here would be a fact about the network
#: rather than about the source.
READABLE_URL = "https://arxiv.org/abs/1606.00335"

#: How stale a `retrieved_at` may be and still be this run's. The assertion is
#: that the timestamp is when the bytes were read, so anything older than the
#: test can only have come from somewhere else.
FRESHNESS_SECONDS = 900.0


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


@pytest.fixture
def project(database: Database, clean: None) -> Project:
    """The certification's project, stated as the work order P10-17 asks about.

    Defined here rather than taken from the shared fixture because the
    objective *is* the input to a live Master: a project described as "find a
    better dopant" would be planned however the model felt that morning, and
    the item is about the five seats running a project, not about one plan.
    """
    with database.transaction() as session:
        return ProjectRegistry(session).create(
            title="Dopant screen, driven",
            objective=LIVE_OBJECTIVE,
            created_by="phase10-live",
        )


@pytest.fixture
async def research_headless(
    clean: None,
    database: Database,
    execution_settings: Settings,
    artifact_store: S3ArtifactStore,
    temporal_unreachable: None,
) -> AsyncIterator[Headless]:
    """`headless`, driving a project that *asks* the screening question rather
    than ordering the work, so the run may end wherever its evidence points.

    Built here rather than by taking `headless` and pointing the supervisor at a
    second project, for the reason this module defines its own `project` at all:
    the project *is* the input to a live Master, and `headless` binds the
    project it was constructed with — the certification's. One project per live
    run is the only shape this item has ever been about; two live projects in
    one database would be two runs the supervisor discovers, drives and ends, at
    twice the cost and with neither one's ending attributable to its objective.
    """
    with database.transaction() as session:
        project = ProjectRegistry(session).create(
            title="Dopant screen, open question",
            objective=RESEARCH_OBJECTIVE,
            created_by="phase10-live",
        )
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


def status_of(database: Database, project_id: str) -> ProjectStatus:
    """The project's status, read out of PostgreSQL rather than remembered."""
    with database.read_only() as session:
        return ProjectRegistry(session).get(project_id).status


async def until_ended(database: Database, project_id: str, *, budget: float) -> ProjectStatus:
    """Wait for the project to reach an ending, and say which one if it does not.

    The wait is on the authoritative record, not on a return value from the
    supervisor: what has to be true is that the *project* ended, and a
    supervisor that stopped with the project still running would be the
    opposite of the finding.

    A run that does not end is the most expensive thing this file can do, so the
    refusal carries the state it was refusing about. That is not decoration: one
    of these runs failed on a review verdict whose wording had been sampled by a
    monitor rather than captured, and a diagnosis that is gone cannot say which
    of "the plan was refused" and "the plan was never read" it was.
    """
    deadline = time.monotonic() + budget
    status = status_of(database, project_id)
    while status not in TERMINAL_PROJECT_STATUSES and time.monotonic() < deadline:
        await asyncio.sleep(POLL_SECONDS)
        status = status_of(database, project_id)
    if status in TERMINAL_PROJECT_STATUSES:
        return status
    raise AssertionError(
        f"the project was still {status.value} after {budget:.0f}s of unattended "
        "driving; a project that never ends is the failure this item is about.\n"
        f"What it was doing at the end:\n{what_it_was_doing(database, project_id)}"
    )


def what_it_was_doing(database: Database, project_id: str) -> str:
    """The project's nodes and the verdicts on them, as one paragraph.

    Read from PostgreSQL rather than from anything the run kept in memory, for
    the reason the whole item exists: the record is what is left when the
    process is gone. Long diagnosis text is truncated, because this is a
    failure message and not an archive — the records themselves are the archive,
    and the run's project is still in the database when this prints.
    """
    with database.read_only() as session:
        nodes = DagRepository(session, project_id).nodes()
        records = RecordRepositories(session, project_id)
        reviews = records.reviews.all()
        deviations = records.deviations.all()
        executions = records.executions.all()
    lines = [
        f"  {node.display_id} {node.node_type.value} {node.status.value}: "
        f"{node.objective[:120]}"
        for node in nodes
    ] or ["  no nodes were ever planned"]
    lines.extend(
        f"  review {review.checkpoint.value} {review.outcome.value} on "
        f"{review.node_id}: {' '.join(review.diagnosis.split())[:300]}"
        for review in reviews
    )
    lines.extend(
        f"  deviation on {deviation.node_id}: "
        f"{' '.join(deviation.requested_action.split())} — "
        f"{' '.join(deviation.description.split())[:200]}"
        for deviation in deviations
    )
    lines.append(
        f"  {len(executions)} execution record(s)"
        + (
            ": "
            + "; ".join(
                f"{record.termination_status.value} "
                f"{record.completeness.verdict.value} on {record.node_id}"
                for record in executions
            )
            if executions
            else ""
        )
    )
    return "\n".join(lines)


def turns_taken(supervisor: ProjectSupervisor, project_id: str) -> dict[str, int]:
    """How many live turns each seat took, read off the pool that served them.

    Read *before* the supervisor is stopped, because stopping it closes every
    runtime and the count is the thing being asked for.

    Asked of the pool rather than of whatever runtime is alive now, because an
    unattended run is long and reaping is routine: a seat that worked early and
    has been idle since has no live runtime, and reading `live_runtime(...)`
    would report it as a seat that was never asked to do anything. That is the
    false failure this item exists to make impossible — it must fail because a
    seat was not reached, never because a runtime was closed.
    """
    pool = supervisor.pool
    assert pool is not None, "the supervisor ran without starting a pool"
    return {role.value: pool.turns_served(project_id, role) for role in SEATS}


# ── P10-17 ─────────────────────────────────────────────────────────────────────


async def test_p10_17_five_live_agents_drive_a_project_to_an_ending(
    headless: Headless, credential: str, tmp_path: Path
) -> None:
    """P10-17: unattended, with every seat a real agent and no script anywhere.

    This is the item Phase 10 exists for: the certification half of it. The
    supervisor is pointed at a database and nothing else; it discovers the
    project, builds five seats from the pinned harness, and drives the loop
    until the project ends. Every turn in that loop is a real model call
    against a real tool server, and the work the plan calls for runs on the
    mock backends through Temporal.

    What is asserted is deliberately about the *shape* of the run rather than
    about one ending: the project reached one of A20's four endings, every one
    of the five seats took at least one live turn, and the run left work behind
    it — nodes, a decision, a verdict, an Execution Record. Which of the four
    endings it is belongs to the science, and a project that ended FAILED with
    a verdict behind it has demonstrated exactly as much about the architecture
    as one that ended COMPLETED.

    Reaching all five seats is a claim about the plan as well as about the
    seats, so the objective is what makes it a fair claim: it asks for three
    pieces of work, one per work seat, with each bench's inputs given. The
    other case, below, is the same architecture under an objective that asks a
    question instead, and it asserts no such thing.
    """
    headless.compute("COMPUTE_SUCCESS")
    headless.lab("LAB_SUCCESS")
    project_id = headless.project.project_id

    # The runtimes live in a temporary root: an unattended run leaves a working
    # directory behind for every scope it started, and a test should not add
    # four of them to the checkout.
    settings = headless.settings.model_copy(update={"runtime_dir": tmp_path / "runtime"})
    supervisor = ProjectSupervisor(
        database=headless.database,
        settings=settings,
        poll_seconds=1.0,
        loop_poll_seconds=0.5,
        loop_max_rounds=80,
    )

    task = asyncio.create_task(supervisor.run(), name="phase10-live-supervisor")
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

    assert status in TERMINAL_PROJECT_STATUSES, status
    silent = [role for role, count in turns.items() if count == 0]
    assert not silent, (
        f"the project reached {status.value} without these seats ever taking a "
        f"live turn: {silent}; every seat is a real agent in Phase 10, and one "
        f"that the run never reached is a seat the architecture does not have. "
        f"Turns taken: {turns}. The objective asks for three pieces of work, one "
        f"per work seat, and names the inputs each bench needs, so a plan that "
        f"reaches fewer seats than that is the finding.\n"
        f"What it was doing at the end:\n{what_it_was_doing(headless.database, project_id)}"
    )

    with headless.database.read_only() as session:
        dag = DagRepository(session, project_id)
        nodes = dag.nodes()
        records = RecordRepositories(session, project_id)
        decisions = records.decisions.all()
        reviews = records.reviews.all()
        executions = records.executions.all()

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
        NodeType.COMPUTATION,
        NodeType.EXPERIMENT,
    }, (
        "the objective asked for the series to be computed and for the undoped "
        "value to be measured, and the plan has neither bench in it: "
        f"{sorted(node.node_type.value for node in nodes)}"
    )


async def test_p10_17_a_research_driven_run_ends_on_its_evidence(
    research_headless: Headless, credential: str, tmp_path: Path
) -> None:
    """P10-17, second case: what a research-driven run may end as, and who may
    do the work it holds.

    The certification above asks whether all five seats can be reached, and its
    objective is written so that they can be. This case asks the other half of
    the same item: what a run driven by real agents is *allowed* to end as.

    Under V0's mock backends a project driven by a question has three honest
    endings and no dishonest one. COMPLETED needs a live Review to accept a
    `simulated` artifact as a measurement and it will not — that refusal is
    `P10-W10`/`P10-W11` and it is the point of the mocks. FAILED and
    INCONCLUSIVE are claims about evidence the seats did or did not find.
    CANCELLED is a stop somebody chose. Which of them this run reaches belongs
    to the science: a case that demanded one of them would be asserting that a
    live model must find an answer, which is not a property of the
    architecture, and the ending it demanded would be the one it got whether or
    not the evidence supported it.

    What is asserted is what the architecture owns. The project ended, and the
    ending is a Decision that names it. Every node that was carried out was
    carried out by the seat that owns its type: a RESEARCH node leaves a
    Research Record and no Execution Record, a COMPUTATION or EXPERIMENT node
    the other way round. Two seats reach the DAG for a node, and neither may
    reach it for the other's.

    This case exists because a live run failed the certification half doing
    exactly this — planning the literature, reading research records that named
    what could not be established, and concluding INCONCLUSIVE in writing. That
    run was right and the assertion it failed was the certification's: a seat
    the project never needed was reported as a seat the architecture lost.
    """
    research_headless.compute("COMPUTE_SUCCESS")
    research_headless.lab("LAB_SUCCESS")
    project_id = research_headless.project.project_id

    settings = research_headless.settings.model_copy(
        update={"runtime_dir": tmp_path / "runtime"}
    )
    supervisor = ProjectSupervisor(
        database=research_headless.database,
        settings=settings,
        poll_seconds=1.0,
        loop_poll_seconds=0.5,
        loop_max_rounds=80,
    )

    task = asyncio.create_task(supervisor.run(), name="phase10-live-open-question")
    try:
        status = await until_ended(
            research_headless.database, project_id, budget=RUN_BUDGET_SECONDS
        )
        turns = turns_taken(supervisor, project_id)
    finally:
        supervisor.stop()
        try:
            await asyncio.wait_for(task, timeout=120.0)
        except TimeoutError:  # pragma: no cover - a supervisor that will not stop
            task.cancel()
            raise

    with research_headless.database.read_only() as session:
        nodes = DagRepository(session, project_id).nodes()
        records = RecordRepositories(session, project_id)
        decisions = records.decisions.all()
        research = ResearchRecordRepository(session, project_id).all()
        executions = records.executions.all()

    assert status in TERMINAL_PROJECT_STATUSES, status

    # A run that named no work at all has not demonstrated anything about the
    # seats, so the plan is checked before the plan's contents are.
    assert nodes, (
        "the project reached its ending without ever planning anything, so there "
        "was no work for any seat to be given and this case has nothing to say. "
        f"Turns taken: {turns}"
    )

    # The ending is Master's judgement and it is written down: a project that
    # stopped without a Decision saying which of A20's endings this is has not
    # ended, it has merely ceased.
    ending = ENDING_DECISION_FOR[status]
    written = sorted({decision.decision_type.value for decision in decisions}) or ["none"]
    assert any(decision.decision_type is ending for decision in decisions), (
        f"the project is {status.value} and no Decision records that ending; the "
        f"decisions written were {', '.join(written)}.\n"
        f"What it was doing at the end:\n"
        f"{what_it_was_doing(research_headless.database, project_id)}"
    )

    researched = {record.node_id for record in research}
    executed = {record.node_id for record in executions}
    for node in nodes:
        owner = WORK_RECORDS.get(node.node_type)
        if owner is None:
            continue
        if owner == "research":
            assert node.node_id not in executed, (
                f"{node.display_id} is a {node.node_type.value} node and has an "
                "Execution Record: a duration was opened for work the Research "
                "seat does inside its own turn, so a Worker acted on a node the "
                "DAG did not give it."
            )
            if node.status in CARRIED_OUT:
                assert node.node_id in researched, (
                    f"{node.display_id} reached {node.status.value} — a state only "
                    "a judged delivery reaches — with no Research Record behind "
                    "it, so something other than the Research seat delivered it."
                )
        else:
            assert node.node_id not in researched, (
                f"{node.display_id} is a {node.node_type.value} node and has a "
                "Research Record: the Research seat answered for work the DAG "
                "gives a Worker."
            )
            if node.status in CARRIED_OUT:
                assert node.node_id in executed, (
                    f"{node.display_id} reached {node.status.value} — a state only "
                    "a judged delivery reaches — with no Execution Record behind "
                    "it, so something other than a Worker carried it out."
                )


# ── P10-18 ─────────────────────────────────────────────────────────────────────


def _payload(result: ProbeResult, index: int, what: str) -> dict[str, Any]:
    """One call's structured payload, with the ways it can be absent ruled out."""
    call: ToolCall = result.calls[index]
    assert not call.failed, f"{what} failed: {call.error}"
    assert call.payload is not None, f"{what} returned no structured payload"
    return call.payload


def _registering(node_id: str) -> Callable[[tuple[ToolCall, ...]], dict[str, Any]]:
    """`register_source`'s arguments, once `open_source` has answered.

    The reference names bytes held in the tool server's own memory, so it is
    read off the earlier reply rather than constructed: there is no way to
    register a source this process did not open.
    """

    def arguments(outcomes: tuple[ToolCall, ...]) -> dict[str, Any]:
        opened = outcomes[0]
        assert not opened.failed, f"open_source failed: {opened.error}"
        assert opened.payload is not None, "open_source returned nothing structured"
        return {"retrieval_ref": opened.payload["retrieval_ref"], "node_id": node_id}

    return arguments


async def test_p10_18_research_still_reads_the_real_internet(
    live_role_environment: RoleEnvironment,
    research_task: Prepared,
    project: Project,
    database: Database,
    artifact_store: ArtifactStore,
) -> None:
    """P10-18: the ledger row is a real reading, and Phase 10 did not soften that.

    Four agents were added beside Research in this phase, and every one of them
    acts on mock backends. The risk that comes with that is not that Research
    starts using a mock — nothing here registers one — but that "simulated" and
    "read" stop being distinguishable to a reader of the ledger. So this case
    goes out to a real open-access source through the real Research tool server,
    registers what it read, and checks the row against the retrieval rather than
    against itself: the URL is where the bytes came from, the hash is recomputed
    here from the snapshot in the object store, the timestamp is this run's, and
    the tier comes with the rule that assigned it.

    The two mock backends are not registered in this project at all, which is
    the point: a project's evidence is real because of where it came from, not
    because of what else the deployment happens to run.
    """
    node_id = research_task.node_id
    result = await probe(
        live_role_environment.for_project(project, AgentRole.RESEARCH),
        calls=(
            ("open_source", {"url": READABLE_URL}),
            ("register_source", _registering(node_id)),
        ),
    )

    opened = _payload(result, 0, "open_source")
    registered = _payload(result, 1, "register_source")
    source = registered["source"]
    assert isinstance(source, dict)
    assert registered["was_read"] is True, (
        "the ledger row does not describe a retrieval, so nothing was read"
    )

    assert source["url"].startswith("https://"), (
        f"evidence from something other than the open web: {source['url']!r}"
    )
    assert opened["access_status"] == AccessStatus.OK.value, opened["note"]
    assert source["retrieved_at"] == opened["retrieved_at"], (
        "the row's timestamp is not the retrieval's, so it records something else"
    )

    # The bytes, read back out of the object store and hashed here rather than
    # compared against a value RAVEL reported about itself.
    snapshot_ref = registered["snapshot_ref"]
    assert snapshot_ref is not None, "no snapshot was stored, so nothing is checkable"
    stored = artifact_store.get(str(snapshot_ref))
    assert f"sha256:{hashlib.sha256(stored).hexdigest()}" == source["content_hash"], (
        "the recorded hash is not the hash of the bytes that were kept"
    )

    # The tier is RAVEL's judgement, and it travels with the rule that produced
    # it so that a reader who disagrees can see which rule to argue with.
    assert registered["tier"]["tier"] and registered["tier"]["rule"], (
        f"a source was rated without a rule: {registered['tier']!r}"
    )

    with database.read_only() as session:
        persisted = EvidenceSourceRepository(session, project.project_id).get(
            source_id=str(source["source_id"])
        )
        assert persisted is not None, "the row was reported but never written"
        artifacts = ArtifactRepository(session, project.project_id)
        behind = [
            artifact
            for artifact in artifacts.all()
            if artifact.artifact_id == persisted.artifact_ref
        ]
        stored_versions = artifacts.versions(str(persisted.artifact_ref))

    assert persisted.url == source["url"]
    assert persisted.content_hash == source["content_hash"]
    assert len(behind) == 1, "the snapshot the row names is not in this project"
    assert snapshot_ref in {version.storage_key for version in stored_versions}, (
        "the object the tool reported is not a version of the artifact the row "
        "points at, so the row and the bytes that were read are two different "
        "things that happen to agree about a hash"
    )
    snapshot = behind[0]
    assert snapshot.kind == SNAPSHOT_KIND and not is_simulated(snapshot.kind), (
        "the artifact behind an evidence row is RAVEL's own snapshot of a page it "
        f"read; {snapshot.kind!r} is the mark a backend's output carries, and the "
        "two must not be reachable through each other"
    )
    assert snapshot.provenance == persisted.url, (
        "the snapshot does not say which URL it is a snapshot of, so a reader "
        "cannot tell a page RAVEL read from a result RAVEL produced"
    )

    assert persisted.retrieved_at is not None, (
        "a readable source with no retrieval time cannot be checked against the "
        "run that read it"
    )
    age = time.time() - persisted.retrieved_at.timestamp()
    assert 0 <= age <= FRESHNESS_SECONDS, (
        f"the row says it was retrieved {age:.0f}s ago, which is not this run; a "
        "canned timestamp would satisfy every other assertion here"
    )
