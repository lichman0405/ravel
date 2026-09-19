"""Master's decisions, each one bound to the record that justifies it.

`DagMutationService` owns what Master may change about the *plan*: which nodes
exist, and what they depend on. This module owns the two decisions that are
about the project rather than about one node — answering a Worker's escalation
(A12), and declaring the project over (A20).

**The two share a shape, and the shape is the point.** Read the state, decide
whether this decision is one the state admits, write the Decision Record, then
change things. Nothing is written before the refusal that would have stopped
it, so a refused decision leaves no trace; nothing is written after the change,
so a decision that exists describes a change that happened.

**What is checked here is the state of the world, not the caller's intent.**
Master may terminate a project at any moment, but may only *conclude* one whose
work has stopped, whose deviations are answered, and which has a frozen
definition of success to be measured against. "This project succeeded" is a
claim about evidence, and a claim made while results are still arriving is a
claim about nothing. Those refusals are the substance of the module; the rest
is bookkeeping.

**Termination is the one ending that is also an action.** A project stopped
mid-flight has live nodes, and leaving them PLANNED would leave a DAG whose
nodes a scheduler could still promote, in a project that has ended. So
termination cancels them under the same decision that terminated the project,
and the audit trail can say which work was stopped and why.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from ravel.domain.contracts import ExecutionContract
from ravel.domain.dag import DagNode
from ravel.domain.decisions import AffectedNodes, DecisionRecord
from ravel.domain.enums import (
    DecisionType,
    NodeStatus,
    ProjectOutcome,
    ProjectStatus,
)
from ravel.domain.execution import DeviationRecord
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.domain.state_machines import (
    PROJECT_TRANSITIONS,
    TERMINAL_NODE_STATUSES,
    TERMINAL_PROJECT_STATUSES,
)
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import (
    ExecutionContractRepository,
    SuccessContractRepository,
)
from ravel.state.repositories.dag import DagRepository, require_master
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.records import DecisionRepository, DeviationRepository
from ravel.state.services.dag import DecisionDraft

__all__ = [
    "ENDING_DECISION",
    "DeviationResolution",
    "MasterService",
    "ProjectConclusion",
    "ReplaceWork",
    "ResolvedDeviation",
    "ReviseContract",
    "Terminate",
]

#: The decision an ending is recorded as. A20's four outcomes, in the
#: vocabulary a reader searches the Decision Records with — so "why did this
#: project stop" is answered by a query rather than by reading the DAG and
#: reconstructing what Master must have meant.
ENDING_DECISION: dict[ProjectOutcome, DecisionType] = {
    ProjectOutcome.SUCCESS: DecisionType.ACCEPT_RESULT,
    ProjectOutcome.FAILED: DecisionType.REJECT_RESULT,
    ProjectOutcome.INCONCLUSIVE: DecisionType.CONCLUDE_INCONCLUSIVE,
    ProjectOutcome.TERMINATED: DecisionType.TERMINATE_PROJECT,
}


@dataclass(frozen=True, slots=True)
class ReviseContract:
    """Answer a deviation by permitting what the Worker asked for.

    The revised terms are for the *same* node. A node's
    `execution_contract_ref` is fixed when it is bound — that is what stops a
    result being measured against terms chosen afterwards — so the revision is
    a new version of the node's contract rather than a rewrite of the old one,
    and every reader that asks "what may this node do" reads the newest.
    """

    terms: ExecutionContract


@dataclass(frozen=True, slots=True)
class ReplaceWork:
    """Answer a deviation by doing something else instead."""

    replacements: tuple[DagNode, ...]


@dataclass(frozen=True, slots=True)
class Terminate:
    """Answer a deviation by ending the project rather than the question."""

    reason: str = ""


#: What Master may decide in response to a deviation.
DeviationResolution = ReviseContract | ReplaceWork | Terminate

#: The decision each kind of resolution is recorded as.
_RESOLUTION_DECISION: dict[type, DecisionType] = {
    ReviseContract: DecisionType.REVISE_EXECUTION_CONTRACT,
    ReplaceWork: DecisionType.REPLACE_NODE,
    Terminate: DecisionType.TERMINATE_PROJECT,
}


@dataclass(frozen=True, slots=True)
class ResolvedDeviation:
    """A deviation, the decision that answered it, and what changed."""

    deviation: DeviationRecord
    decision: DecisionRecord
    #: The node whose contract was revised, when that was the answer.
    revised: ExecutionContract | None = None
    #: The node as the revision left it — READY, so that the work goes on. The
    #: caller is shown the node rather than told it moved, because whether it
    #: moved depends on where it was: a revision answers the node that asked,
    #: and a node something else has already moved on from is left alone.
    resumed: DagNode | None = None
    created: tuple[DagNode, ...] = ()
    cancelled: tuple[str, ...] = ()
    #: The project this resolution ended, when it ended one.
    conclusion: ProjectConclusion | None = None


@dataclass(frozen=True, slots=True)
class ProjectConclusion:
    """The ending Master recorded, and the work it stopped."""

    project: Project
    outcome: ProjectOutcome
    decision: DecisionRecord
    cancelled: tuple[str, ...] = ()

    @property
    def status(self) -> ProjectStatus:
        """The project status this ending was written as."""
        return self.outcome.status


class MasterService:
    """The decisions Master makes about a project, as opposed to about a node."""

    def __init__(self, session: Session, project_id: str) -> None:
        self.session = session
        self.project_id = project_id
        self.dag = DagRepository(session, project_id)
        self.decisions = DecisionRepository(session, project_id)
        self.deviations = DeviationRepository(session, project_id)
        self.executions = ExecutionContractRepository(session, project_id)
        self.success = SuccessContractRepository(session, project_id)
        self.projects = ProjectRegistry(session)

    # ── Reading what needs an answer ────────────────────────────────────────

    def open_deviations(self) -> list[DeviationRecord]:
        """Every escalation no decision has answered yet.

        This is what Master's turn is *for*. A Worker that pauses has stopped
        its node and said why; until one of these is answered the node sits at
        `WAITING_DECISION` and the branch behind it does not move.
        """
        return self.deviations.open()

    def unfinished_nodes(self) -> list[DagNode]:
        """Every node whose ending has not been recorded."""
        return [
            node
            for node in self.dag.nodes()
            if node.status not in TERMINAL_NODE_STATUSES
        ]

    # ── A12: answering an escalation ────────────────────────────────────────

    def resolve_deviation(
        self,
        deviation_id: str,
        resolution: DeviationResolution,
        *,
        role: AgentRole,
        decision: DecisionDraft,
    ) -> ResolvedDeviation:
        """Answer a Worker's escalation, and record what the answer changed.

        The resolution is a closed union rather than a free-form instruction,
        because A12 names three answers — revise the contract, do different
        work, stop — and each one changes something different. A single
        "resolve" that took a prose instruction would leave the caller to
        perform the change itself, which is the mutation this method exists to
        be the only path for.

        Args:
            deviation_id: The escalation being answered.
            resolution: Which of the three answers this is.
            role: The caller's role, checked against Master.
            decision: The decision that justifies the answer.

        Raises:
            PermissionError: The actor is not Master.
            NotFound: This project has no such deviation.
            ValueError: The deviation is already answered, the decision is of
                the wrong type for this resolution, the revised terms do not
                permit what was asked for, or the replacement is empty.
        """
        require_master(role)
        deviation = self.deviations.get(deviation_id=deviation_id)
        if not deviation.is_open:
            raise ValueError(
                f"deviation {deviation.deviation_id} was already answered by "
                f"{deviation.resolved_by_decision_ref}; a second answer would "
                "leave two decisions each claiming to have settled it"
            )
        expected = _RESOLUTION_DECISION[type(resolution)]
        if decision.decision_type is not expected:
            raise ValueError(
                f"answering a deviation with {type(resolution).__name__} records a "
                f"{expected.value} decision, not {decision.decision_type.value}"
            )

        node = self.dag.node(deviation.node_id)
        match resolution:
            case ReviseContract(terms):
                return self._revise_contract(deviation, node, terms, role=role, decision=decision)
            case ReplaceWork(replacements):
                return self._replace_work(
                    deviation, node, replacements, role=role, decision=decision
                )
            case Terminate(reason):
                return self._terminate_for_deviation(
                    deviation, reason, role=role, decision=decision
                )

    def _revise_contract(
        self,
        deviation: DeviationRecord,
        node: DagNode,
        terms: ExecutionContract,
        *,
        role: AgentRole,
        decision: DecisionDraft,
    ) -> ResolvedDeviation:
        """Widen or narrow what a node may do, under a new version.

        The terms must permit the action the Worker asked about. Without that
        check a revision would *look* like an answer while leaving the Worker
        exactly as blocked as it was — the deviation would be closed, the
        decision would say the question was settled, and the next run would
        stop in the same place with nothing recording why.

        **And the node is returned to READY**, which is the other half of
        answering. A Worker that stopped is waiting at WAITING_DECISION, and
        nothing else in RAVEL moves a node out of it: the loop starts what is
        READY and touches nothing else, so a revision that left the node where
        it was would close the escalation while leaving the work stopped
        forever. It goes to READY rather than straight to RUNNING because
        starting a run is the scheduler's, and a node that is ready is one the
        scheduler will start — under the new version, which is what the run is
        identified by.
        """
        if terms.node_id != deviation.node_id:
            raise ValueError(
                f"these terms are for {terms.node_id}, and deviation "
                f"{deviation.deviation_id} was raised by {deviation.node_id}; a "
                "revision answers the node that asked"
            )
        if not terms.permits(deviation.requested_action):
            raise ValueError(
                f"the revised contract does not permit "
                f"{deviation.requested_action!r}, which is what "
                f"{deviation.deviation_id} asked for; a revision that leaves the "
                "Worker as blocked as it was has answered nothing"
            )
        existing = self.executions.for_node(node.node_id)
        if terms.version <= existing.version:
            raise ValueError(
                f"these terms are version {terms.version} and {node.display_id} "
                f"already has version {existing.version}; a revision comes after "
                "the contract it revises"
            )

        record = self._record(
            decision,
            role=role,
            affected=AffectedNodes(modified=(node.node_id,)),
        )
        written = self.executions.freeze(self.executions.add(terms).contract_id)
        resumed = (
            self.dag.transition_node(
                node.node_id,
                NodeStatus.READY,
                actor_id=role.value,
                decision_ref=record.decision_id,
            )
            if node.status is NodeStatus.WAITING_DECISION
            else node
        )
        resolved = self.deviations.resolve(
            deviation.deviation_id, decision_ref=record.decision_id
        )
        return ResolvedDeviation(
            deviation=resolved, decision=record, revised=written, resumed=resumed
        )

    def _replace_work(
        self,
        deviation: DeviationRecord,
        node: DagNode,
        replacements: tuple[DagNode, ...],
        *,
        role: AgentRole,
        decision: DecisionDraft,
    ) -> ResolvedDeviation:
        """Do something else instead, and stop what the substitution strands.

        The same computation as A09's replanning, for the same reason: a node
        that was waiting on the abandoned one can no longer be satisfied, and
        leaving it in the plan would leave work that can never run with nothing
        saying so. What differs is only what prompted it — a failure there, a
        Worker's escalation here — and a second implementation of "what is
        stranded" would be free to disagree with the first.
        """
        if not replacements:
            raise ValueError(
                f"replacing {node.display_id} commits no nodes; a deviation answered "
                "by doing nothing is answered by cancelling the node"
            )
        stranded = [
            descendant
            for descendant in self.dag.stranded_by(node.node_id)
            if descendant.node_id != node.node_id
        ]
        record = self._record(
            decision,
            role=role,
            affected=AffectedNodes(
                created=tuple(replacement.node_id for replacement in replacements),
                cancelled=(node.node_id, *(item.node_id for item in stranded)),
            ),
        )
        created = tuple(
            self.dag.add_nodes(
                [
                    replacement.model_copy(
                        update={"decision_ref": record.decision_id}
                    )
                    for replacement in replacements
                ],
                role=role,
                decision_ref=record.decision_id,
            )
        )
        for item in (node, *stranded):
            self.dag.cancel_node(
                item.node_id, role=role, decision_ref=record.decision_id
            )
        resolved = self.deviations.resolve(
            deviation.deviation_id, decision_ref=record.decision_id
        )
        return ResolvedDeviation(
            deviation=resolved,
            decision=record,
            created=created,
            cancelled=(node.node_id, *(item.node_id for item in stranded)),
        )

    def _terminate_for_deviation(
        self,
        deviation: DeviationRecord,
        reason: str,
        *,
        role: AgentRole,
        decision: DecisionDraft,
    ) -> ResolvedDeviation:
        """End the project, and close the deviation with the same decision.

        Termination reached this way and termination reached through
        `conclude` are the same act, so they are the same code: one
        implementation of "stop everything", reached from two places. The
        deviation is closed by the decision that stopped the project, which is
        how a reader sees that this escalation is what ended it.
        """
        conclusion = self.conclude(
            ProjectOutcome.TERMINATED,
            role=role,
            decision=decision,
            reason=reason,
        )
        resolved = self.deviations.resolve(
            deviation.deviation_id, decision_ref=conclusion.decision.decision_id
        )
        return ResolvedDeviation(
            deviation=resolved,
            decision=conclusion.decision,
            cancelled=conclusion.cancelled,
            conclusion=conclusion,
        )

    # ── A20: declaring the project over ─────────────────────────────────────

    def conclude(
        self,
        outcome: ProjectOutcome,
        *,
        role: AgentRole,
        decision: DecisionDraft,
        reason: str = "",
    ) -> ProjectConclusion:
        """Record how the project ended, and stop whatever is still running.

        Three of the four endings are statements about results, and each is
        refused while a result is still outstanding: work that has not ended
        could still change the answer, so "the project succeeded" said now is a
        claim the project has not finished making. The fourth, termination, is
        a statement about the work rather than about the answer, and is
        therefore available at any time — which is why it is the one ending
        that cancels nodes.

        Args:
            outcome: How the project ended.
            role: The caller's role, checked against Master.
            decision: The decision that justifies the ending.
            reason: Why, for the event stream. Required for termination, where
                the reason is the only thing distinguishing a deliberate stop
                from a project that ran out of road.

        Raises:
            PermissionError: The actor is not Master.
            ValueError: The project has already ended, the decision is of the
                wrong type, the project's own status cannot reach this ending,
                work is unfinished, a deviation is unanswered, the project has
                no frozen success contract, or a SUCCESS is claimed with
                nothing that passed.
        """
        require_master(role)
        expected = ENDING_DECISION[outcome]
        if decision.decision_type is not expected:
            raise ValueError(
                f"concluding a project as {outcome.value} records a "
                f"{expected.value} decision, not {decision.decision_type.value}; a "
                "reader finds the ending by type"
            )
        if outcome.is_termination and not reason.strip():
            raise ValueError(
                "terminating a project needs a reason; the ending is otherwise "
                "indistinguishable from a project that ran out of road"
            )

        project = self.projects.get(self.project_id)
        if project.status in TERMINAL_PROJECT_STATUSES:
            raise ValueError(
                f"project {project.display_id} already ended as "
                f"{project.status.value}; a later ending would contradict a "
                "recorded one"
            )
        allowed = PROJECT_TRANSITIONS[project.status]
        if outcome.status not in allowed:
            # Checked here rather than left to `ProjectRegistry.transition`,
            # which enforces it too — but *after* the decision is written. An
            # ending the project cannot reach would otherwise leave behind a
            # decision describing an ending that never happened, which is the
            # one thing this module's write order exists to prevent.
            reachable = ", ".join(sorted(status.value for status in allowed))
            raise ValueError(
                f"project {project.display_id} is {project.status.value}, which "
                f"cannot become {outcome.status.value}; {outcome.value} is an "
                f"ended project's status, and from {project.status.value} the "
                f"project can only become: {reachable}"
            )
        if not outcome.is_termination:
            self._refuse_while_work_remains(outcome)

        # Read first, write second, apply third. The doomed list is taken now,
        # so the decision names the nodes the cancellation will reach rather
        # than the nodes a later read happens to find.
        doomed = self._doomed_nodes() if outcome.is_termination else ()
        record = self._record(
            decision,
            role=role,
            affected=AffectedNodes(
                cancelled=tuple(node.node_id for node in doomed)
            ),
        )
        for node in doomed:
            self.dag.cancel_node(
                node.node_id, role=role, decision_ref=record.decision_id
            )
        moved = self.projects.transition(
            self.project_id,
            outcome.status,
            actor_id=role.value,
            reason=reason or None,
        )
        return ProjectConclusion(
            project=moved,
            outcome=outcome,
            decision=record,
            cancelled=tuple(node.node_id for node in doomed),
        )

    def _refuse_while_work_remains(self, outcome: ProjectOutcome) -> None:
        """Refuse an ending that is a claim about results, while results are due.

        Three checks, in the order a reader would ask them: is there a frozen
        definition of done, has the work stopped, and is anything still waiting
        on Master. A deviation is the one that is easy to miss — the project's
        nodes can all be terminal while a Worker's unanswered question sits
        beside a CANCELLED node, and concluding then would end the project with
        an escalation nobody answered.
        """
        try:
            contract = self.success.latest()
        except NotFound as error:
            raise ValueError(
                f"project {self.project_id} has no Project Success Contract, so "
                f"{outcome.value} is a claim with no frozen definition to measure "
                "it against; what success means is fixed before execution, not "
                "after it"
            ) from error

        unfinished = self.unfinished_nodes()
        if unfinished:
            names = ", ".join(node.display_id for node in unfinished[:5])
            raise ValueError(
                f"{len(unfinished)} node(s) have not ended ({names}); "
                f"{outcome.value} is a statement about results, and work still in "
                "flight could still change the answer"
            )

        open_deviations = self.open_deviations()
        if open_deviations:
            raise ValueError(
                f"{len(open_deviations)} deviation(s) are unanswered "
                f"({', '.join(item.deviation_id for item in open_deviations[:5])}); "
                "a project cannot conclude while a Worker is waiting on Master"
            )

        if outcome is ProjectOutcome.SUCCESS and not any(
            node.succeeded for node in self.dag.nodes()
        ):
            raise ValueError(
                f"no node in this project passed, so there is no result to accept "
                f"against {contract.contract_id} version {contract.version}; a "
                "project that answered nothing has not succeeded"
            )

    def _doomed_nodes(self) -> tuple[DagNode, ...]:
        """Every node a termination would stop.

        Taken before the decision is written, so the nodes `affected_nodes`
        names are the nodes the cancellation will actually reach — the record
        describes the change, and a list assembled from a second read of the
        DAG afterwards would be a description rather than an authorization.
        """
        return tuple(self.unfinished_nodes())

    # ── Internals ───────────────────────────────────────────────────────────

    def _record(
        self, decision: DecisionDraft, *, role: AgentRole, affected: AffectedNodes
    ) -> DecisionRecord:
        """Write the decision that authorizes a change, before the change."""
        return self.decisions.record(
            decision.record(project_id=self.project_id, actor=role, affected=affected),
            role=role,
        )
