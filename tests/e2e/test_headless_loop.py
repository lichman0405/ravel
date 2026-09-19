"""The Phase 7 gate: a project runs from creation to an ending, with no human.

The property under test is the one nothing else in the suite asserts: that the
*closed loop* closes. Every other gate drives one layer — the DAG refuses an
illegal transition, the durable layer survives a killed worker, a mock backend
plays its scenario — and all of them would still pass if nobody had written the
thing that decides what happens next. This is that thing, and these tests are
what say so.

What runs here is real: PostgreSQL, Temporal, a worker, the V0 mock backends.
What is scripted is the *policy* of the two agent powers — the part a model
owns in production, and the part these tests are not about.

The scenarios walk the loop's branches rather than a plausible research plan: a
stage of two nodes that pass, a stage whose second node fails and is replanned,
a worker that stops and is answered, a project a human pauses, and a project
that stops moving with nobody to move it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from tests.e2e.conftest import Headless, Task

from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    DecisionType,
    JoinPolicy,
    NodeStatus,
    NodeType,
    ProjectOutcome,
    ProjectStatus,
    ReviewCheckpoint,
    ReviewOutcome,
    TerminationStatus,
)
from ravel.domain.roles import AgentRole
from ravel.execution.loop import ProjectLoop
from ravel.execution.node_runs import TemporalNodeRuns
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DecisionRepository, RecordRepositories

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(300)]

#: What the two nodes of a scripted stage are for. The second reads the first,
#: so a stage that plans and a DAG that promotes are both exercised: until the
#: measurement passes, the analysis has nothing to read.
MEASURE = "Measure conductivity across the dopant series."
ANALYSE = "Fit the conductivity model to the measurements."


@dataclass
class Nodes:
    """Builds a stage's nodes, and remembers them by the name the test gave them.

    Node identifiers are RAVEL's, so a test that wants to say "the analysis
    depends on the measurement" cannot name one. It names them here instead,
    and the factory resolves the dependency when Master plans the stage.
    """

    project_id: str
    built: dict[str, DagNode] = field(default_factory=dict)

    def task(
        self,
        name: str,
        objective: str,
        *,
        depends_on: str | None = None,
        node_type: NodeType = NodeType.COMPUTATION,
        **terms: object,
    ) -> Task:
        """A piece of work for the scripted Master to plan."""

        def build() -> DagNode:
            node = DagNode.create(
                project_id=self.project_id,
                node_type=node_type,
                objective=objective,
                created_by=AgentRole.MASTER.value,
                dependencies=(self.built[depends_on].node_id,) if depends_on else (),
                # A node with a fan-in must state the rule that fires it; the
                # domain refuses one without, because "waits for something, by
                # an unspecified rule" is not a plan.
                join_policy=JoinPolicy.ALL if depends_on else None,
            )
            self.built[name] = node
            return node

        return Task(build=build, **terms)  # type: ignore[arg-type]

    def __getitem__(self, name: str) -> DagNode:
        return self.built[name]


# ── The gate ────────────────────────────────────────────────────────────────


async def test_a_project_reaches_a_terminal_outcome_with_no_human_input(
    headless: Headless,
) -> None:
    """Creation to COMPLETED, driven only by the loop.

    A01, A05, A06, A08 and A20 in one run, and the run is the assertion: the
    project is created empty and ends with an outcome it reached itself.
    Nothing in this test moves a node, writes a review, or records a decision —
    every one of those is a turn the loop took.
    """
    headless.compute("COMPUTE_SUCCESS")
    nodes = Nodes(headless.project.project_id)
    master = headless.master(
        (
            nodes.task("measure", MEASURE),
            nodes.task("analyse", ANALYSE, depends_on="measure"),
        )
    )
    review = headless.review()

    run = await headless.drive(master, review)

    assert run.finished, f"the loop halted at {run.status.value} after {run.rounds} rounds"
    assert run.status is ProjectStatus.COMPLETED
    assert not run.halted
    assert master.trace == ["contract_defined", "planned", "concluded:SUCCESS"]

    # Both nodes ran, and the one that waited on the other ran second.
    assert [
        headless.status_of(nodes["measure"]),
        headless.status_of(nodes["analyse"]),
    ] == [NodeStatus.PASSED, NodeStatus.PASSED]
    assert review.trace == [
        f"PRE_RUN:{nodes['measure'].display_id}:PASS",
        f"FINAL:{nodes['measure'].display_id}:PASS",
        f"PRE_RUN:{nodes['analyse'].display_id}:PASS",
        f"FINAL:{nodes['analyse'].display_id}:PASS",
    ], "every node is cleared before it runs, and accepted after it does"


async def test_the_run_leaves_the_records_a_reader_needs(headless: Headless) -> None:
    """The aftermath, read the way a person auditing the project would read it.

    A terminal status is not the same thing as a project that can be explained.
    What is asserted here is that the run left what a reader would ask for: an
    ending that says how it ended, a plan that says what it consisted of, an
    execution that says what was produced, and reviews against frozen criteria.
    """
    headless.compute("COMPUTE_SUCCESS")
    nodes = Nodes(headless.project.project_id)
    master = headless.master((nodes.task("measure", MEASURE),))
    review = headless.review()

    await headless.drive(master, review)

    with headless.database.read_only() as session:
        records = RecordRepositories(session, master.project_id)
        decisions = DecisionRepository(session, master.project_id).all()
        node = DagRepository(session, master.project_id).node(nodes["measure"].node_id)
        execution = records.latest_execution(node.node_id)
        reviews = records.reviews.all()

    assert [decision.decision_type for decision in decisions] == [
        DecisionType.CREATE_NODE,
        DecisionType.ACCEPT_RESULT,
    ]
    assert decisions[0].affected_nodes.created == (node.node_id,)
    assert decisions[1].authority_check.permitted

    assert execution is not None, "a node that ran has an Execution Record"
    assert execution.output_refs, "the record names what the run produced"
    assert node.artifact_refs, "what a run produced is attached to the node it came from"
    assert [review.checkpoint for review in reviews] == [
        ReviewCheckpoint.PRE_RUN,
        ReviewCheckpoint.FINAL,
    ]
    assert all(review.outcome is ReviewOutcome.PASS for review in reviews)
    assert all(review.frozen_criteria_ref for review in reviews), (
        "a verdict names the criteria version it was measured against, or it "
        "cannot be told apart from one measured against criteria chosen later"
    )


async def test_a_failure_is_replanned_and_the_project_still_ends(
    headless: Headless,
) -> None:
    """A09: a FAIL is answered by different work, and the failure stays failed.

    The compute mock plays the scientific-failure scenario, so the measurement
    fails and the analysis that waited on it is retired with it — it never ran,
    so it cannot have failed, and CANCELLED is what the DAG says about work a
    replan left no room for. The scripted Master answers by asking the question
    a different way — on the bench rather than in the solver — and the lab mock
    succeeds, so the replacement passes and the project ends in a success. The
    node that failed stays in the DAG as a failed node, because a plan that
    erases its own history cannot be reviewed by anyone.

    The two backends are different mocks because the replacement is different
    *work*: replanning is a change of method, and a replan that ran the same
    thing on the same backend would be a retry wearing a decision record.
    """
    headless.compute("COMPUTE_SCIENTIFIC_FAILURE")
    headless.lab("LAB_SUCCESS")
    nodes = Nodes(headless.project.project_id)
    replacement = Nodes(headless.project.project_id)

    def replan(failed: DagNode) -> tuple[Task, ...]:
        return (
            replacement.task(
                "retry",
                f"Measure again by another method, after {failed.display_id}.",
                node_type=NodeType.EXPERIMENT,
            ),
        )

    master = headless.master(
        (
            nodes.task("measure", MEASURE),
            nodes.task("analyse", ANALYSE, depends_on="measure"),
        ),
        replan=replan,
    )
    review = headless.review()

    run = await headless.drive(master, review)

    assert run.finished, f"the loop halted at {run.status.value}"
    assert run.status is ProjectStatus.COMPLETED
    assert master.trace == [
        "contract_defined",
        "planned",
        "replanned",
        "concluded:SUCCESS",
    ]
    assert headless.status_of(nodes["measure"]) is NodeStatus.FAILED
    assert headless.status_of(nodes["analyse"]) is NodeStatus.CANCELLED, (
        "the node that never ran is retired rather than left waiting on work "
        "that failed: a replan changes the future, and what the failure "
        "stranded is not part of it any more"
    )
    assert headless.status_of(replacement["retry"]) is NodeStatus.PASSED

    with headless.database.read_only() as session:
        decisions = DecisionRepository(session, master.project_id).all()
    replanning = [d for d in decisions if d.decision_type is DecisionType.REPLACE_NODE]
    assert len(replanning) == 1
    assert replanning[0].affected_nodes.created == (replacement["retry"].node_id,)


async def test_a_failure_nobody_answers_ends_the_project(headless: Headless) -> None:
    """The other answer to a failure, and the one a script has to state.

    With no replacement to offer, the script declares the project failed rather
    than leaving a node sitting in FAILED forever. That is the loop's contract
    with Master: a project that has stopped is handed over, and somebody has to
    say what it came to.
    """
    headless.compute("COMPUTE_SCIENTIFIC_FAILURE")
    nodes = Nodes(headless.project.project_id)
    master = headless.master(
        (nodes.task("measure", MEASURE),), outcome=ProjectOutcome.FAILED
    )
    review = headless.review()

    run = await headless.drive(master, review)

    assert run.status is ProjectStatus.FAILED
    assert master.trace == ["contract_defined", "planned", "concluded:FAILED"]
    assert headless.status_of(nodes["measure"]) is NodeStatus.FAILED


async def test_a_worker_that_stops_is_answered_and_the_run_continues(
    headless: Headless,
) -> None:
    """A11 and A12: the escalation path, out to Master and back.

    The lab mock asks for a pressure the contract does not permit, so the
    Worker stops and the node waits at WAITING_DECISION. The loop hands it to
    Master, who widens the contract under a Decision Record; the node is READY
    again and runs a second time under the revised terms.

    The second run is the part worth watching, and three things make it
    possible: the run is identified by the contract version as well as by the
    node, so Temporal starts it rather than refusing it; it is the next attempt
    at the work rather than a second attempt one, so it does not collide with
    the job the stopped run left behind; and the node was moved out of
    WAITING_DECISION, which nothing but Master's answer does.

    **The second run ends at its deadline**, and that is the mock being honest
    rather than a fault. A lab that reported a pressure it cannot reach keeps
    working once it is permitted to use the one it can — the catalogue says
    what it reports and nothing about answering it — so the run is one no
    backend finishes, and the deadline is what RAVEL has for that. Review then
    has no completed result to accept and fails the node, and Master, offered
    no replacement, ends the project the only way left to it: inconclusively.
    """
    headless.lab("LAB_DEVIATION_PRESSURE")
    nodes = Nodes(headless.project.project_id)
    master = headless.master(
        (
            nodes.task(
                "pressurise",
                "Run the pressure series on the doped samples.",
                node_type=NodeType.EXPERIMENT,
                criteria=("The pressure series separates the samples.",),
            ),
        ),
        outcome=ProjectOutcome.INCONCLUSIVE,
    )
    review = headless.review()

    run = await headless.drive(master, review)

    assert master.trace == [
        "contract_defined",
        "planned",
        "answered_deviation",
        "concluded:INCONCLUSIVE",
    ]
    assert run.finished, f"the loop halted at {run.status.value}"
    assert run.status is ProjectStatus.INCONCLUSIVE

    with headless.database.read_only() as session:
        records = RecordRepositories(session, master.project_id)
        node = DagRepository(session, master.project_id).node(
            nodes["pressurise"].node_id
        )
        executions = records.executions.for_node(node.node_id)
        deviations = records.deviations.all()
        decisions = records.decisions.all()

    assert len(deviations) == 1
    assert not deviations[0].is_open, "the escalation was answered"
    assert [d.decision_type for d in decisions] == [
        DecisionType.CREATE_NODE,
        DecisionType.REVISE_EXECUTION_CONTRACT,
        DecisionType.CONCLUDE_INCONCLUSIVE,
    ]
    assert [e.execution_contract_version for e in executions] == [1, 2], (
        "the run that stopped and the run that followed the revision are two "
        "executions under two versions of the contract, not one: "
        f"{[(e.execution_contract_version, e.termination_status) for e in executions]}"
    )
    assert executions[0].termination_status is TerminationStatus.DEVIATION, (
        "the first run ended because a Worker asked a question only Master may "
        "answer; the second is the run that question made possible"
    )


async def test_a_paused_project_is_not_driven_through(headless: Headless) -> None:
    """A pause is the operator's, and the loop stops rather than overriding it.

    Asserted as a *halt* rather than as a failure: the project did not end, and
    saying so is the whole of what the loop should do about it. Resuming is what
    starts it again.
    """
    import asyncio

    headless.compute("COMPUTE_SUCCESS")
    nodes = Nodes(headless.project.project_id)
    master = headless.master((nodes.task("measure", MEASURE),))
    review = headless.review()

    async def pause_once_it_is_running() -> None:
        while True:
            with headless.database.transaction() as session:
                registry = ProjectRegistry(session)
                if registry.get(master.project_id).status is ProjectStatus.EXECUTING:
                    registry.transition(
                        master.project_id,
                        ProjectStatus.PAUSED,
                        actor_id="owner",
                        reason="The operator paused the project.",
                    )
                    return
            await asyncio.sleep(0.02)

    pauser = asyncio.create_task(pause_once_it_is_running())
    try:
        run = await headless.drive(master, review)
    finally:
        await pauser

    assert run.halted, "a paused project is not an ending, and the loop says so"
    assert run.status is ProjectStatus.PAUSED


async def test_a_project_that_cannot_move_halts_rather_than_spinning(
    headless: Headless,
) -> None:
    """The stall detector, which is the reason a stuck project is not a hang.

    A Master that reads the situation and does nothing with it is a project
    that has stopped and a system that has not noticed. The loop notices: it
    counts the rounds that changed nothing and stops, rather than waiting for a
    decision that is never coming.
    """

    class Silent:
        """A Master that takes its turn and decides nothing."""

        def __init__(self) -> None:
            self.turns = 0

        async def act(self, situation: object) -> None:
            self.turns += 1

    master = Silent()
    loop = ProjectLoop(
        database=headless.database,
        project_id=headless.project.project_id,
        master=master,
        review=headless.review(),
        execution=await TemporalNodeRuns.connect(headless.database, headless.settings),
        poll_seconds=0.01,
        max_stalled_rounds=5,
    )

    run = await loop.run()

    assert run.halted
    assert not run.finished
    assert run.stalled_rounds >= 5
    assert master.turns >= 1, "the loop asked before it gave up"


async def test_a_review_that_refuses_a_result_stops_that_branch(
    headless: Headless,
) -> None:
    """A08: FAIL is an outcome, not an error, and the project is told about it.

    The node ran and produced what the contract asked for; Review judged it
    against the frozen criteria and did not accept it. The loop's part is to
    hand Master a project whose work has stopped, which is what ends it.
    """
    headless.compute("COMPUTE_SUCCESS")
    nodes = Nodes(headless.project.project_id)
    master = headless.master(
        (nodes.task("measure", MEASURE),), outcome=ProjectOutcome.INCONCLUSIVE
    )
    review = headless.review({MEASURE: ReviewOutcome.FAIL})

    run = await headless.drive(master, review)

    assert headless.status_of(nodes["measure"]) is NodeStatus.FAILED
    assert run.status is ProjectStatus.INCONCLUSIVE
    assert master.trace == ["contract_defined", "planned", "concluded:INCONCLUSIVE"]
