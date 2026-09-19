"""A20: a project reaches one of four endings, with a trail that says why.

`acceptance/V0_ACCEPTANCE.md` A20 asks for a project to *end* — SUCCESS,
FAILED, INCONCLUSIVE or TERMINATED — "with final audit trail". The four are
not four labels on one act. Three of them are claims about results, and a claim
about results made while results are still arriving is a claim about nothing;
the fourth is a claim about the work and is available at any moment. Almost
every test here is one of those refusals, because the refusals are what
separate an ending from a status change.

The audit trail is the other half. It is assembled in `ProjectAudit` from the
records themselves rather than stored beside them, and the last tests here read
it back the way a stranger checking the ending would: what ended, on what
decision, and whether the work had actually stopped.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.integration.conftest import Prepared
from tests.integration.master.conftest import Escalation

from ravel.domain.contracts import ProjectSuccessContract
from ravel.domain.dag import DagNode
from ravel.domain.enums import (
    DecisionType,
    NodeStatus,
    NodeType,
    ProjectOutcome,
    ProjectStatus,
)
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.master import MasterService, ProjectAudit
from ravel.state.repositories.records import (
    DecisionRepository,
    DeviationRepository,
)
from ravel.state.services.dag import DecisionDraft

pytestmark = pytest.mark.integration

Prepare = Callable[..., Prepared]
Escalate = Callable[..., Escalation]
Draft = Callable[..., DecisionDraft]
RunToEnd = Callable[..., NodeStatus]


# ── Reaching an ending ──────────────────────────────────────────────────────


def test_a_project_succeeds_once_its_work_has_passed(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """The ordinary ending: the work ran, the result was accepted, it is over."""
    node = prepared()
    run_to_end(node.node_id)

    conclusion = master.conclude(
        ProjectOutcome.SUCCESS,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.ACCEPT_RESULT),
    )

    assert conclusion.status is ProjectStatus.COMPLETED
    assert master.projects.get(master.project_id).status is ProjectStatus.COMPLETED
    assert conclusion.cancelled == (), (
        "nothing was still running, so nothing was stopped; an ending that "
        "cancels work it did not need to is an ending that hid a failure"
    )


@pytest.mark.parametrize(
    ("outcome", "decision_type", "status"),
    [
        (ProjectOutcome.SUCCESS, DecisionType.ACCEPT_RESULT, ProjectStatus.COMPLETED),
        (ProjectOutcome.FAILED, DecisionType.REJECT_RESULT, ProjectStatus.FAILED),
        (
            ProjectOutcome.INCONCLUSIVE,
            DecisionType.CONCLUDE_INCONCLUSIVE,
            ProjectStatus.INCONCLUSIVE,
        ),
    ],
)
def test_each_outcome_is_recorded_as_the_status_it_means(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
    outcome: ProjectOutcome,
    decision_type: DecisionType,
    status: ProjectStatus,
) -> None:
    """All three endings that are claims about results, in one place.

    Parametrized rather than written three times because what is being asserted
    is the *mapping* — outcome to status, and outcome to decision type — and
    three near-identical tests would let one of the three drift.
    """
    node = prepared()
    ended = (
        NodeStatus.PASSED if outcome is ProjectOutcome.SUCCESS else NodeStatus.FAILED
    )
    run_to_end(node.node_id, ended)

    conclusion = master.conclude(
        outcome, role=AgentRole.MASTER, decision=a_decision(decision_type)
    )

    assert conclusion.outcome is outcome
    assert conclusion.status is status
    assert conclusion.decision.decision_type is decision_type


def test_the_ending_is_a_decision_a_reader_can_find(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """The trail's first link: an ending Master is accountable for.

    Recorded like every other DAG change — a typed decision, by an actor, with
    the nodes it affected — so "why did this project stop" is answered by
    querying the Decision Records rather than by reading the DAG and
    reconstructing what Master must have meant.
    """
    node = prepared()
    run_to_end(node.node_id)

    conclusion = master.conclude(
        ProjectOutcome.SUCCESS,
        role=AgentRole.MASTER,
        decision=a_decision(
            DecisionType.ACCEPT_RESULT, rationale="Conductivity rose 21% across the series."
        ),
    )

    stored = DecisionRepository(master.session, master.project_id).get(
        decision_id=conclusion.decision.decision_id
    )
    assert stored.decision_type is DecisionType.ACCEPT_RESULT
    assert stored.authority_check.actor_role == AgentRole.MASTER.value
    assert stored.authority_check.permitted
    assert "21%" in stored.rationale
    assert stored.affected_nodes.cancelled == ()


# ── Stopping ────────────────────────────────────────────────────────────────


def test_terminating_a_running_project_cancels_the_work_it_stops(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    a_decision: Draft,
) -> None:
    """Termination is the one ending that is also an action.

    A project stopped mid-flight has live nodes, and leaving them PLANNED would
    leave a DAG a scheduler could still promote inside a project that has
    ended. The decision names them, so the trail says which work was stopped —
    and a reader does not have to infer it from timestamps.
    """
    first = prepared()
    second = prepared()

    conclusion = master.conclude(
        ProjectOutcome.TERMINATED,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.TERMINATE_PROJECT),
        reason="The lab lost access to the instrument this project depends on.",
    )

    assert conclusion.status is ProjectStatus.CANCELLED
    assert set(conclusion.cancelled) == {first.node_id, second.node_id}
    assert master.dag.node(first.node_id).status is NodeStatus.CANCELLED
    assert master.dag.node(second.node_id).status is NodeStatus.CANCELLED
    stored = DecisionRepository(master.session, master.project_id).get(
        decision_id=conclusion.decision.decision_id
    )
    assert set(stored.affected_nodes.cancelled) == {first.node_id, second.node_id}


def test_termination_leaves_the_work_it_stopped_readable(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    a_decision: Draft,
) -> None:
    """Cancelled, not deleted — A12's "history preserved" for the same reason.

    The node still names the contract it was bound to and the decision that
    created it, so a reader can see that this work was *planned and abandoned*
    rather than never planned at all. A termination that removed the nodes
    would make the project's shape depend on when it was read.
    """
    node = prepared()
    before = master.dag.node(node.node_id)

    master.conclude(
        ProjectOutcome.TERMINATED,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.TERMINATE_PROJECT),
        reason="Stopped by the owner before the first run completed.",
    )

    after = master.dag.node(node.node_id)
    assert after.status is NodeStatus.CANCELLED
    assert after.execution_contract_ref == before.execution_contract_ref
    assert after.decision_ref == before.decision_ref


def test_terminating_needs_a_reason(
    master: MasterService,
    running: Project,
    a_decision: Draft,
) -> None:
    """The one ending whose justification is not in the results.

    A project that failed says why by having failed. A terminated one says
    nothing at all unless Master says it, and "the project stopped" is then
    indistinguishable from a project that ran out of road.
    """
    with pytest.raises(ValueError, match="needs a reason"):
        master.conclude(
            ProjectOutcome.TERMINATED,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.TERMINATE_PROJECT),
            reason="   ",
        )


def test_terminating_is_available_before_any_work_has_ended(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    a_decision: Draft,
) -> None:
    """The ending that is not a claim about results, so it waits for none.

    A project whose first run is still going can be stopped — that is what
    stopping means. The three refusals below are exactly what it is exempt from.
    """
    prepared()

    conclusion = master.conclude(
        ProjectOutcome.TERMINATED,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.TERMINATE_PROJECT),
        reason="The owner withdrew the project.",
    )

    assert conclusion.status is ProjectStatus.CANCELLED


# ── Refusals: the state has to admit the ending ─────────────────────────────


def test_a_project_can_only_end_once(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """A second ending would contradict a recorded one."""
    node = prepared()
    run_to_end(node.node_id)
    master.conclude(
        ProjectOutcome.SUCCESS,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.ACCEPT_RESULT),
    )

    with pytest.raises(ValueError, match="already ended"):
        master.conclude(
            ProjectOutcome.TERMINATED,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.TERMINATE_PROJECT),
            reason="On reflection, stop.",
        )


def test_concluding_with_unfinished_work_is_refused(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """A result still in flight could still change the answer."""
    finished = prepared()
    run_to_end(finished.node_id)
    outstanding = prepared()

    with pytest.raises(ValueError, match="have not ended"):
        master.conclude(
            ProjectOutcome.SUCCESS,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.ACCEPT_RESULT),
        )

    assert master.projects.get(master.project_id).status is ProjectStatus.EXECUTING
    assert master.dag.node(outstanding.node_id).status is NodeStatus.READY


def test_concluding_with_an_unanswered_deviation_is_refused(
    master: MasterService,
    running: Project,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """The check that is easy to miss, and the reason it is here.

    The project's nodes can all be terminal while a Worker's question sits
    unanswered beside a node that was stopped anyway — and concluding then
    would end the project with an escalation nobody answered. So the refusal is
    on the *deviation*, not on the node's status: the node here is CANCELLED,
    which is terminal, and the project still may not conclude.
    """
    escalation = escalated()
    master.dag.cancel_node(
        escalation.node_id,
        role=AgentRole.MASTER,
        decision_ref="dec-stopped-by-hand",
    )

    with pytest.raises(ValueError, match="waiting on Master"):
        master.conclude(
            ProjectOutcome.INCONCLUSIVE,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.CONCLUDE_INCONCLUSIVE),
        )

    assert DeviationRepository(
        master.session, master.project_id
    ).get(deviation_id=escalation.deviation_id).is_open


def test_concluding_without_a_success_contract_is_refused(
    master: MasterService,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """No `running` fixture, deliberately: the project has no frozen success.

    What success means is fixed before execution, not after it. A project that
    never defined it has nothing for "this succeeded" to be measured against,
    and inventing the definition at the end is how a project succeeds by
    lowering the bar.
    """
    registry_project = master.projects.transition(
        master.project_id,
        ProjectStatus.CONTRACT_DEFINED,
        actor_id=AgentRole.MASTER.value,
    )
    assert registry_project.status is ProjectStatus.CONTRACT_DEFINED
    master.projects.transition(
        master.project_id, ProjectStatus.EXECUTING, actor_id=AgentRole.MASTER.value
    )
    node = prepared()
    run_to_end(node.node_id)

    with pytest.raises(ValueError, match="no Project Success Contract"):
        master.conclude(
            ProjectOutcome.SUCCESS,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.ACCEPT_RESULT),
        )


def test_success_with_nothing_that_passed_is_refused(
    master: MasterService,
    running: Project,
    success_contract: ProjectSuccessContract,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """Every node ended, and none of them produced a result to accept.

    The condition the other three checks do not cover: a project can be quiet
    without having answered anything, and "the work stopped" is not "we
    succeeded".
    """
    node = prepared()
    run_to_end(node.node_id, NodeStatus.FAILED)

    with pytest.raises(ValueError, match="no node in this project passed"):
        master.conclude(
            ProjectOutcome.SUCCESS,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.ACCEPT_RESULT),
        )
    assert success_contract.success_criteria, "the contract that is being measured against"


def test_failing_with_nothing_that_passed_is_allowed(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """The mirror of the test above, so the check is not doing more than it says.

    FAILED is a claim about the evidence too, and it is the claim a project
    with no passing node actually supports. If this were refused as well, the
    only reachable ending for a project that answered nothing would be
    termination — which says the project was stopped rather than that it
    answered.
    """
    node = prepared()
    run_to_end(node.node_id, NodeStatus.FAILED)

    conclusion = master.conclude(
        ProjectOutcome.FAILED,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.REJECT_RESULT),
    )

    assert conclusion.status is ProjectStatus.FAILED


def test_an_ending_the_project_cannot_reach_is_refused(
    master: MasterService,
    success_contract: ProjectSuccessContract,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """A project that never started executing has nothing to conclude.

    The status machine does not have CREATED -> COMPLETED, and this asserts
    that Master is told so *before* anything is written. Without the check the
    refusal would come from the registry, after the decision had been recorded
    — leaving a decision describing an ending that never happened.
    """
    node = prepared()
    run_to_end(node.node_id)

    with pytest.raises(ValueError, match="cannot become COMPLETED"):
        master.conclude(
            ProjectOutcome.SUCCESS,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.ACCEPT_RESULT),
        )

    assert len(DecisionRepository(master.session, master.project_id).all()) == 0


def test_an_ending_with_the_wrong_decision_type_is_refused(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """The type is how a reader finds the ending, so it is checked."""
    node = prepared()
    run_to_end(node.node_id)

    with pytest.raises(ValueError, match="ACCEPT_RESULT"):
        master.conclude(
            ProjectOutcome.SUCCESS,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.CHANGE_ROUTE),
        )


def test_only_master_may_end_a_project(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    a_decision: Draft,
) -> None:
    """A11 and A19 in the same breath: the ending is a Master decision.

    A Review Worker that could declare a project successful would be deciding
    what its own verdicts were worth — the merge of two of the three powers the
    role model exists to keep apart.
    """
    prepared()

    for role in (
        AgentRole.RESEARCH,
        AgentRole.REVIEW,
        AgentRole.COMPUTE_WORKER,
        AgentRole.EXPERIMENTAL_WORKER,
    ):
        with pytest.raises(PermissionError, match="may not mutate the DAG"):
            master.conclude(
                ProjectOutcome.TERMINATED,
                role=role,
                decision=a_decision(DecisionType.TERMINATE_PROJECT),
                reason="Stopping this.",
            )

    assert master.projects.get(master.project_id).status is ProjectStatus.EXECUTING


def test_a_refused_ending_leaves_no_decision_behind(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    a_decision: Draft,
) -> None:
    """The write order, asserted where it is observable.

    The decision is written before the change, so every refusal has to happen
    before the decision. Here the refusal is the unanswered work — the change
    would have been the ending, and a decision recording an ending that did not
    happen is the record that exists to make endings attributable, attributing
    one that never occurred.
    """
    prepared()
    decisions = DecisionRepository(master.session, master.project_id)
    before = len(decisions.all())

    with pytest.raises(ValueError, match="have not ended"):
        master.conclude(
            ProjectOutcome.SUCCESS,
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.ACCEPT_RESULT),
        )

    assert len(decisions.all()) == before


# ── The audit trail ─────────────────────────────────────────────────────────


def test_the_audit_trail_can_be_read_before_the_ending(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    a_decision: Draft,
) -> None:
    """`is_concludable` is what a reader asks to explain a wait.

    Not a second enforcement of the rule — the enforcement is in the service,
    next to the write — but the same three conditions stated so that a TUI can
    say *why* a project has not ended instead of showing a status and leaving
    the reader to guess.
    """
    node = prepared()

    audit = ProjectAudit.assemble(master.session, master.project_id)
    assert not audit.is_concludable, "a READY node is work still in flight"
    assert [item.node_id for item in audit.unfinished] == [node.node_id]
    assert audit.passed == ()
    assert not audit.is_finished
    assert audit.success_contract is not None


def test_the_audit_trail_tells_the_whole_ending(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    run_to_end: RunToEnd,
    a_decision: Draft,
) -> None:
    """A20's "with final audit trail", read the way a stranger would read it.

    Everything a reader needs to disagree with the ending is already an
    immutable record, so this asserts the trail is *reachable* from one place
    and says the things that matter: what the project became, which decision
    ended it, whether the work had stopped, and what the project was measured
    against.
    """
    node = prepared()
    run_to_end(node.node_id)
    conclusion = master.conclude(
        ProjectOutcome.SUCCESS,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.ACCEPT_RESULT),
    )

    audit = ProjectAudit.assemble(master.session, master.project_id)

    assert audit.project.status is ProjectStatus.COMPLETED
    assert audit.is_finished
    assert not audit.is_concludable, "an ended project has nothing left to conclude"
    assert audit.unfinished == ()
    assert [item.node_id for item in audit.passed] == [node.node_id]
    assert audit.open_deviations == ()
    assert conclusion.decision.decision_id in {
        decision.decision_id for decision in audit.decisions
    }
    assert audit.success_contract is not None
    assert audit.decisions and audit.reviews, (
        "the decision that ended it and the review that accepted the result are "
        "both part of the trail"
    )
    assert audit.events, "the ending is in the stream a consumer tails"
    assert audit.executions == (), (
        "nothing handed a backend a job in this test, so there is nothing to "
        "report; the field is empty rather than absent because a reader asks "
        "for the trail in one shape whether or not a run happened"
    )


def test_the_audit_trail_shows_a_termination_stopped_live_work(
    master: MasterService,
    running: Project,
    prepared: Prepare,
    a_decision: Draft,
) -> None:
    """The trail of the ending that changes the DAG as well as the project.

    Terminated is the ending a reader is most likely to be asked to justify
    later, and the audit has to show both halves of it: the project stopped,
    and the nodes it stopped. A cancelled node is still an unfinished-ending
    *status* — CANCELLED is terminal — so the two agree, and the events say a
    cancellation happened rather than a completion.
    """
    node = prepared()
    master.conclude(
        ProjectOutcome.TERMINATED,
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.TERMINATE_PROJECT),
        reason="The instrument was decommissioned mid-project.",
    )

    audit = ProjectAudit.assemble(master.session, master.project_id)

    assert audit.project.status is ProjectStatus.CANCELLED
    assert audit.is_finished
    assert audit.unfinished == (), "a cancelled node has ended, however it ended"
    assert audit.passed == ()
    assert master.dag.node(node.node_id).status is NodeStatus.CANCELLED
    terminated = [
        event
        for event in audit.events
        if event.payload.get("to") == ProjectStatus.CANCELLED.value
    ]
    assert terminated, (
        "the event stream is what a consumer tails; an ending that is not in it "
        "is an ending half the system never hears about"
    )


def test_the_audit_trail_of_an_unstarted_project_is_quiet_rather_than_broken(
    master: MasterService,
    a_node: Callable[..., DagNode],
) -> None:
    """A project with no success contract is a legitimate state to read.

    `assemble` is called by the TUI on whatever project the operator is looking
    at, including one that has just been created, so "nothing has happened yet"
    has to be a readable answer rather than an exception the caller must catch.
    """
    a_node(NodeType.RESEARCH)  # a plan exists; nothing has been defined about success

    audit = ProjectAudit.assemble(master.session, master.project_id)

    assert audit.success_contract is None
    assert not audit.is_concludable
    assert not audit.is_finished
    assert audit.decisions == ()
