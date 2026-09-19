"""A12: Master answers the escalation, and the answer is one of three things.

`acceptance/V0_ACCEPTANCE.md` A12 asks for a deviation to be resolved "by
authorized decision: revised contract/new node/termination; history
preserved". The three answers are not three flavours of one operation — each
changes something different, and each is refused when the state does not admit
it.

What this file is really about is the gap between *closing* a deviation and
*answering* it. A resolution that marks the escalation resolved without
unblocking the Worker leaves a project that looks like it made a decision and
a node that will stop in the same place on its next run. That is the failure
mode the two checks in `_revise_contract` exist to prevent — the terms must be
for the node that asked, and they must permit the action it asked about — and
it is why the first test asserts that the Worker's *next* question gets a
different answer.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from tests.integration.master.conftest import REFUSED_ACTION, Escalation

from ravel.domain.contracts import ExecutionContract
from ravel.domain.dag import DagNode
from ravel.domain.enums import DecisionType, NodeStatus, NodeType
from ravel.domain.roles import AgentRole
from ravel.master import (
    MasterService,
    ReplaceWork,
    ReviseContract,
    Terminate,
)
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.records import (
    DecisionRepository,
    DeviationRepository,
)
from ravel.state.services.dag import DecisionDraft

pytestmark = pytest.mark.integration

Escalate = Callable[..., Escalation]
Draft = Callable[..., DecisionDraft]


def _revision(escalation: Escalation, **overrides: object) -> ExecutionContract:
    """A second version of the node's contract, permitting what was refused."""
    terms = escalation.contract
    return ExecutionContract(
        project_id=terms.project_id,
        node_id=terms.node_id,
        version=terms.version + 1,
        objective=terms.objective,
        procedure=str(overrides.get("procedure", "Rinse once more between runs.")),
        allowed_actions=(REFUSED_ACTION, *terms.allowed_actions),
        required_outputs=terms.required_outputs,
    )


# ── Revising the contract ───────────────────────────────────────────────────


def test_a_revised_contract_unblocks_the_worker(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """The revision has to change what the Worker's next question gets.

    `permits` is the lookup the Worker's rule is built on, and it reads the
    newest version of the node's contract. Asserting the deviation is closed
    would pass just as well if the revision had changed nothing — so this
    asserts the answer the Worker would receive.
    """
    escalation = escalated()
    assert not master.executions.permits(escalation.node_id, REFUSED_ACTION)

    resolved = master.resolve_deviation(
        escalation.deviation_id,
        ReviseContract(terms=_revision(escalation)),
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
    )

    assert resolved.revised is not None
    assert resolved.revised.version == escalation.contract.version + 1
    assert master.executions.permits(escalation.node_id, REFUSED_ACTION), (
        "the Worker's rule is a lookup against the newest contract; a revision "
        "the lookup does not see has not unblocked anything"
    )
    assert not resolved.deviation.is_open


def test_the_revision_is_recorded_against_the_node_it_changed(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """`modified`, not `created` — the node is the one that was already there."""
    escalation = escalated()

    resolved = master.resolve_deviation(
        escalation.deviation_id,
        ReviseContract(terms=_revision(escalation)),
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
    )

    affected = DecisionRepository(master.session, master.project_id).get(
        decision_id=resolved.decision.decision_id
    ).affected_nodes
    assert affected.modified == (escalation.node_id,)
    assert affected.created == () and affected.cancelled == ()


def test_revising_the_contract_the_run_was_under_is_still_a_new_version(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """The node keeps the reference it was bound to; the terms move forward.

    A node's `execution_contract_ref` is fixed when it is bound, which is what
    stops a finished result being measured against terms chosen afterwards. So
    a revision is a new version rather than a rewrite, and the DAG's own link
    to the old one survives as the record of what the run started under.
    """
    escalation = escalated()

    master.resolve_deviation(
        escalation.deviation_id,
        ReviseContract(terms=_revision(escalation)),
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
    )

    node = master.dag.node(escalation.node_id)
    assert node.execution_contract_ref == escalation.contract.contract_id, (
        "the node still points at the contract the run began under"
    )
    assert master.executions.for_node(escalation.node_id, version=1).contract_id == (
        escalation.contract.contract_id
    )


def test_a_revision_that_does_not_permit_the_action_is_refused(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """The check that separates closing a deviation from answering one."""
    escalation = escalated()
    terms = _revision(escalation)
    silently_narrower = terms.model_copy(update={"allowed_actions": ("run_measurement",)})

    with pytest.raises(ValueError, match="does not permit"):
        master.resolve_deviation(
            escalation.deviation_id,
            ReviseContract(terms=silently_narrower),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
        )

    assert DeviationRepository(
        master.session, master.project_id
    ).get(deviation_id=escalation.deviation_id).is_open, (
        "a revision that answers nothing leaves the escalation open"
    )


def test_a_revision_for_another_node_is_refused(
    master: MasterService,
    escalated: Escalate,
    a_node: Callable[..., DagNode],
    a_decision: Draft,
) -> None:
    """Terms are written for a node, and a revision answers the node that asked.

    The other node names a real node of this project rather than an invented
    identifier, because the check being asserted is about *whose* terms these
    are — and a made-up identifier would pass it for the wrong reason on a day
    the check became a lookup.
    """
    escalation = escalated()
    other = a_node(NodeType.COMPUTATION, objective="Something else entirely.")

    with pytest.raises(ValueError, match="answers the node that asked"):
        master.resolve_deviation(
            escalation.deviation_id,
            ReviseContract(
                terms=ExecutionContract(
                    project_id=master.project_id,
                    node_id=other.node_id,
                    version=2,
                    objective="Something else entirely.",
                    allowed_actions=(REFUSED_ACTION,),
                )
            ),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
        )


def test_a_revision_that_does_not_come_after_the_contract_is_refused(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """A version that does not supersede anything would not be read.

    `for_node` returns the newest version, so terms numbered at or below the
    existing one would leave the Worker looking at the old contract — the
    revision would be a row nothing consults.
    """
    escalation = escalated()
    terms = _revision(escalation).model_copy(
        update={"version": escalation.contract.version}
    )

    with pytest.raises(ValueError, match="comes after the contract it revises"):
        master.resolve_deviation(
            escalation.deviation_id,
            ReviseContract(terms=terms),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
        )


# ── Doing something else instead ────────────────────────────────────────────


def test_replacing_the_work_cancels_the_node_that_asked(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
    a_node: Callable[..., DagNode],
) -> None:
    """The escalation's node has stopped, and Master decides not to restart it."""
    escalation = escalated()

    resolved = master.resolve_deviation(
        escalation.deviation_id,
        ReplaceWork(
            replacements=(
                a_node(NodeType.COMPUTATION, objective="Simulate the rinse instead."),
            )
        ),
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.REPLACE_NODE),
    )

    assert resolved.cancelled == (escalation.node_id,)
    assert master.dag.node(escalation.node_id).status is NodeStatus.CANCELLED
    assert len(resolved.created) == 1
    assert master.dag.node(resolved.created[0].node_id).status is NodeStatus.PLANNED


def test_the_replaced_node_keeps_its_history(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
    a_node: Callable[..., DagNode],
) -> None:
    """"History preserved" is the half of A12 that a rewrite would lose.

    The node is cancelled rather than deleted or reused: it still exists, still
    names the contract it ran under, and still carries the decision that
    created it — so a reader can see that this work was started and abandoned
    rather than never planned.
    """
    escalation = escalated()
    before = master.dag.node(escalation.node_id)

    master.resolve_deviation(
        escalation.deviation_id,
        ReplaceWork(
            replacements=(
                a_node(NodeType.COMPUTATION, objective="Simulate the rinse instead."),
            )
        ),
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.REPLACE_NODE),
    )

    after = master.dag.node(escalation.node_id)
    assert after.status is NodeStatus.CANCELLED
    assert after.execution_contract_ref == before.execution_contract_ref
    assert after.decision_ref == before.decision_ref, (
        "the cancelled node still belongs to the decision that created it, not "
        "to the one that stopped it"
    )


def test_replacing_work_with_nothing_is_refused(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    escalation = escalated()

    with pytest.raises(ValueError, match="commits no nodes"):
        master.resolve_deviation(
            escalation.deviation_id,
            ReplaceWork(replacements=()),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.REPLACE_NODE),
        )


# ── Stopping ────────────────────────────────────────────────────────────────


def test_terminating_ends_the_project_and_closes_the_deviation(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """The one answer that is also an ending, and the deviation says which.

    Termination reached from a deviation and termination reached through
    `conclude` are one act; what this asserts is that the deviation is closed
    *by the decision that ended the project*, so a reader can see that this
    escalation is what stopped it.
    """
    escalation = escalated()

    resolved = master.resolve_deviation(
        escalation.deviation_id,
        Terminate(reason="The rinse the lab needs is outside the project's scope."),
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.TERMINATE_PROJECT),
    )

    assert resolved.conclusion is not None
    assert resolved.conclusion.status.value == "CANCELLED"
    assert resolved.deviation.resolved_by_decision_ref == (
        resolved.decision.decision_id
    )
    assert master.dag.node(escalation.node_id).status is NodeStatus.CANCELLED


# ── Refusals ────────────────────────────────────────────────────────────────


def test_only_master_may_answer_a_deviation(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """A11's other half: the Worker escalates, and does not answer itself."""
    escalation = escalated()

    for role in (
        AgentRole.RESEARCH,
        AgentRole.REVIEW,
        AgentRole.COMPUTE_WORKER,
        AgentRole.EXPERIMENTAL_WORKER,
    ):
        with pytest.raises(PermissionError, match="may not mutate the DAG"):
            master.resolve_deviation(
                escalation.deviation_id,
                ReviseContract(terms=_revision(escalation)),
                role=role,
                decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
            )

    assert DeviationRepository(
        master.session, master.project_id
    ).get(deviation_id=escalation.deviation_id).is_open


def test_an_answered_deviation_cannot_be_answered_again(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    escalation = escalated()
    master.resolve_deviation(
        escalation.deviation_id,
        ReviseContract(terms=_revision(escalation)),
        role=AgentRole.MASTER,
        decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
    )

    with pytest.raises(ValueError, match="already answered"):
        master.resolve_deviation(
            escalation.deviation_id,
            Terminate(reason="Actually, stop."),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.TERMINATE_PROJECT),
        )


def test_a_resolution_with_the_wrong_decision_type_is_refused(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """The type is how a reader finds the answer, so it is checked."""
    escalation = escalated()

    with pytest.raises(ValueError, match="REVISE_EXECUTION_CONTRACT"):
        master.resolve_deviation(
            escalation.deviation_id,
            ReviseContract(terms=_revision(escalation)),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.CHANGE_ROUTE),
        )


def test_a_refused_resolution_leaves_no_decision_behind(
    master: MasterService,
    escalated: Escalate,
    a_decision: Draft,
) -> None:
    """The write order, asserted where it is observable.

    The decision is written before the change, so a change that is refused must
    leave no decision — otherwise the record that exists to make DAG changes
    attributable would contain one describing a change that never happened.
    """
    escalation = escalated()
    decisions = DecisionRepository(master.session, master.project_id)
    before = len(decisions.all())

    with pytest.raises(ValueError, match="does not permit"):
        master.resolve_deviation(
            escalation.deviation_id,
            ReviseContract(
                terms=_revision(escalation).model_copy(
                    update={"allowed_actions": ("run_measurement",)}
                )
            ),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.REVISE_EXECUTION_CONTRACT),
        )

    assert len(decisions.all()) == before


def test_an_unknown_deviation_cannot_be_answered(
    master: MasterService,
    a_decision: Draft,
) -> None:
    with pytest.raises(NotFound):
        master.resolve_deviation(
            "deviation-that-does-not-exist",
            Terminate(reason="Nothing to terminate."),
            role=AgentRole.MASTER,
            decision=a_decision(DecisionType.TERMINATE_PROJECT),
        )
