"""Master's window onto the project, and the checkpoint it writes for itself.

Every handler here reads or writes PostgreSQL. None of them reads the runtime
brief: the brief is a launch-time snapshot of what RAVEL believed when it
started the session, and a Master that made decisions from it would be deciding
from a memory of the project rather than from the project. `read_project_state`
exists precisely so the agent can ask the record of truth instead of trusting
its own recollection of the conversation.
"""

from __future__ import annotations

from typing import Any

from ravel.domain.contracts import BudgetLimits, ProjectSuccessContract, ResearchContract
from ravel.domain.decisions import ReviewRecord
from ravel.domain.enums import (
    DecisionType,
    JobState,
    NodeStatus,
    ProjectOutcome,
    ReviewOutcome,
)
from ravel.domain.identity import MasterCheckpoint
from ravel.domain.reconciliation import RunReconciliation
from ravel.domain.roles import AgentRole
from ravel.master.service import ENDING_DECISION, MasterService
from ravel.mcp.context import ToolContext, as_json, require_master
from ravel.mcp.registry import tools_for
from ravel.state.outbox import last_event_seq
from ravel.state.repositories.contracts import ResearchContractRepository
from ravel.state.repositories.identity import (
    AgentIdentityRepository,
    ApprovalRepository,
    CheckpointRepository,
)
from ravel.state.repositories.projects import ProjectRegistry, RoadmapRepository
from ravel.state.repositories.reconciliation import RunReconciliationRepository
from ravel.state.repositories.records import RecordRepositories
from ravel.state.services.dag import DagMutationService, DecisionDraft

#: The statuses counted separately in the state summary. Anything not listed is
#: reported under its own name, so a new status cannot be silently folded into
#: a bucket that would misdescribe it.
_AT_A_GLANCE = (
    NodeStatus.PLANNED,
    NodeStatus.READY,
    NodeStatus.RUNNING,
    NodeStatus.REVIEWING,
)


def whoami(context: ToolContext) -> Any:
    """Report this session's project, role, and available tools."""

    async def whoami() -> dict[str, Any]:
        """Report the project and role this session is serving, and its tools."""
        return {
            "project_id": context.project_id,
            "role": context.role.value,
            "role_name": context.role.display_name,
            "tools": list(tools_for(context.role)),
            "may_mutate_dag": context.scope.is_master,
        }

    return whoami


def _state_name(state: JobState | None) -> str | None:
    """A job state as it is reported, or nothing for a run that had no job.

    `None` rather than a word, because the two are different: a run that died
    before `start_job` recorded anything had no job, and "unknown" would read
    as a job nobody could identify. A reader who wants to know which ran out of
    the two looks at `job_id`, which is null in exactly the same case.
    """
    return state.value if state is not None else None


def read_project_state(context: ToolContext) -> Any:
    """Read the authoritative project state."""

    async def read_project_state() -> dict[str, Any]:
        """Read this project's authoritative state from the database.

        Returns the project's own record, its roadmap and where in it the
        project currently is, a summary of the DAG by status, and how many
        approvals are waiting on a human. Use this rather than any earlier
        description of the project, including your own.
        """
        with context.read() as session:
            project = ProjectRegistry(session).get(context.project_id)
            phases = RoadmapRepository(session, context.project_id).phases()
            service = DagMutationService(session, context.project_id)
            horizon = service.horizon()
            work = service.phase_work()
            nodes = service.dag.nodes()
            pending = ApprovalRepository(session, context.project_id).pending()
            checkpoint = CheckpointRepository(session, context.project_id).latest()
            contracts = ResearchContractRepository(session, context.project_id).all()
            # Read inside this transaction, like every other row here, and
            # reduced to the one record the read reports rather than kept as a
            # repository: a repository used after this block closes checks a
            # connection back out of the pool for a query nobody commits, and
            # that holds a read lock on everything it touched until the process
            # ends — which is a database no other test can truncate. The newest
            # version governs, and it is named as a version rather than taken
            # from the end of the list, because the list is ordered by when a
            # row was written.
            successes = MasterService(session, context.project_id).success.all()
            success = max(successes, key=lambda contract: contract.version, default=None)
            # Why the stopped nodes are stopped, which is the half of that
            # question the node's own status does not carry. A node waits on
            # Master for at least two different reasons — a Worker asked for
            # something its contract refused, or a Review withheld the
            # pre-flight clearance — and the answer Master has to give is
            # different for each. Without this, the one role that may change the
            # plan is asked to change it without being told what went wrong.
            # Latest per node, and the ordering is `all()`'s: oldest first.
            latest: dict[str, ReviewRecord] = {}
            for review in RecordRepositories(session, context.project_id).reviews.all():
                latest[review.node_id] = review
            # The third reason a node is waiting, and the one with no author:
            # a run of it was lost, and RAVEL parked it here rather than decide
            # what that meant. The record is what says so — without it the node
            # reads as a question with no question in it, and the one role that
            # may answer would be asked to answer nothing.
            #
            # Keyed by node rather than by run, because a node is what this read
            # reports about and a node can lose more than one run. The newest is
            # kept, which is `for_node`'s ordering: a reconciliation written
            # later is about a revision of the terms, and it is the one that
            # says where the node is now.
            lost: dict[str, RunReconciliation] = {}
            for reconciliation in RunReconciliationRepository(
                session, context.project_id
            ).all():
                lost[reconciliation.node_id] = reconciliation
            seq = last_event_seq(session, context.project_id)

        counts: dict[str, int] = {}
        for node in nodes:
            counts[node.status.value] = counts.get(node.status.value, 0) + 1

        return {
            "project_id": project.project_id,
            "display_id": project.display_id,
            "title": project.title,
            "objective": project.objective,
            "status": project.status.value,
            "created_at": project.created_at.isoformat(),
            "roadmap": [
                {
                    "name": phase.name,
                    "order": phase.order,
                    "intent": phase.intent,
                    "nodes": work[phase.name].nodes if phase.name in work else 0,
                    "unfinished": work[phase.name].unfinished if phase.name in work else 0,
                }
                for phase in phases
            ],
            "current_phase": horizon.current.name if horizon.current else None,
            "reachable_phases": list(horizon.reachable_names),
            "dag": {
                "nodes": len(nodes),
                "by_status": counts,
                "at_a_glance": {
                    status.value: counts.get(status.value, 0) for status in _AT_A_GLANCE
                },
                "ready_to_run": [n.display_id for n in nodes if n.status is NodeStatus.READY],
            },
            "approvals_pending": len(pending),
            # The nodes that are waiting on a decision, each with the verdict
            # that put it there. BLOCKED is included because it is the same
            # question from Master's side — nothing here may proceed until the
            # plan changes — and it has no verdict, which is itself the answer:
            # a node whose dependencies cannot all be satisfied was stopped by
            # the graph rather than by anyone's judgement.
            "stopped": [
                {
                    "node": node.display_id,
                    "node_type": node.node_type.value,
                    "status": node.status.value,
                    "objective": node.objective,
                    # Only a verdict that *withheld* something is a reason a
                    # node stopped. A PASS is the clearance a node needed to
                    # enter RUNNING, and a node that ran and was then parked —
                    # by a lost run, by a deviation — still has that PASS as its
                    # latest review, so reporting it here would answer "why did
                    # this stop" with a document saying it was allowed to go.
                    # Observed rather than theorised: the first version of this
                    # read did exactly that, and the acceptance case that reads
                    # it back caught it.
                    "verdict": (
                        {
                            "checkpoint": latest[node.node_id].checkpoint.value,
                            "outcome": latest[node.node_id].outcome.value,
                            "diagnosis": latest[node.node_id].diagnosis,
                            "recommendations": list(latest[node.node_id].recommendations),
                        }
                        if node.node_id in latest
                        and latest[node.node_id].outcome is not ReviewOutcome.PASS
                        else None
                    ),
                    # Why RAVEL stopped it, when RAVEL is the one that did.
                    # Every value is a report from somewhere else — Temporal's
                    # word for the run, the job's state, the class RAVEL read
                    # off both — so Master can see that a queue was lost without
                    # being told anything about the science.
                    "run_reconciliation": (
                        {
                            "reconciliation_id": lost[node.node_id].reconciliation_id,
                            "failure_class": lost[node.node_id].failure_class.value,
                            "observed": lost[node.node_id].observed.value,
                            "workflow_id": lost[node.node_id].workflow_id,
                            "execution_contract_version": (
                                lost[node.node_id].execution_contract_version
                            ),
                            "job_id": lost[node.node_id].job_id,
                            "job_state_before": _state_name(
                                lost[node.node_id].job_state_before
                            ),
                            "job_state_after": _state_name(
                                lost[node.node_id].job_state_after
                            ),
                            "detail": lost[node.node_id].detail,
                            "detected_by": lost[node.node_id].detected_by,
                            "created_at": lost[node.node_id].created_at.isoformat(),
                        }
                        if node.node_id in lost
                        else None
                    ),
                }
                for node in nodes
                if node.status in (NodeStatus.WAITING_DECISION, NodeStatus.BLOCKED)
            ],
            # Whether the project has stated its own question, reported here
            # because this is the read a session starts from: a contract that
            # is missing is the first thing to write, and a session that had to
            # discover that by having a research task refused would be
            # discovering it from the wrong end of the project.
            "research_contract": (
                {
                    "committed": True,
                    "contract_id": contracts[0].contract_id,
                    "scientific_problem": contracts[0].scientific_problem,
                }
                if contracts
                else {
                    "committed": False,
                    "what_is_missing": (
                        "this project has no research contract, so what it was asked "
                        "for has not been written down; commit_research_contract is "
                        "that act, and it comes before the plan"
                    ),
                }
            ),
            # The other contract the project has to state about itself, and
            # reported for the same reason: until it exists the project cannot
            # be concluded at all, and a session that learned that from a
            # refusal at the end of the project would be learning it too late
            # to do anything about it. `status` is here too because the two
            # move together — the first version of this contract is what takes
            # the project out of CREATED.
            "success_contract": (
                {
                    "committed": True,
                    "contract_id": success.contract_id,
                    "version": success.version,
                    "success_criteria": list(success.success_criteria),
                }
                if success is not None
                else {
                    "committed": False,
                    "what_is_missing": (
                        "this project has not said what would count as answering it, "
                        "so none of A20's endings can be measured; "
                        "commit_success_contract is that act, and it is what makes "
                        "the project able to end at all"
                    ),
                }
            ),
            "latest_checkpoint": as_json(checkpoint) if checkpoint is not None else None,
            "last_event_seq": seq,
        }

    return read_project_state


def commit_research_contract(context: ToolContext) -> Any:
    """Write down what this project was asked to do."""

    async def commit_research_contract(
        original_user_goal: str,
        scientific_problem: str,
        hypotheses: list[str] | None = None,
        target_metrics: list[str] | None = None,
        acceptance_strategy: str = "",
        known_constraints: list[str] | None = None,
        prohibited_actions: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write this project's research contract: what it is for, and what it is asking.

        **This is the project's own statement of its question, and it is the
        first thing a new project is missing.** `original_user_goal` is what was
        asked for, in the words it was asked in — the record that a later
        reader compares the work against. `scientific_problem` is that goal
        turned into something a study can answer; the two are separate fields
        because "find a better dopant" and "which dopant keeps conductivity
        above the threshold" are not the same statement, and only the second
        one can be planned against.

        It is written once and never edited. A change to what the user wants is
        a new project, not a second contract, so state it as the user's ask
        rather than as your current reading of it.

        Everything else is optional, and each part is used: `hypotheses` are
        what the project will test, `target_metrics` are what would count as
        answering it, `acceptance_strategy` is how a result is to be judged,
        `known_constraints` are limits that are already given, and
        `prohibited_actions` are things this project must not do. The Research
        seat reads all of it as the terms its tasks are answered under, so what
        is left out is a question nobody will be asked to answer.

        It changes no node and commits no stage, so it is not a DAG mutation:
        the plan that follows is still yours to write, with
        `commit_roadmap_phase` and `expand_dag_phase`.
        """
        require_master(context, "commit_research_contract")
        goal = original_user_goal.strip()
        if not goal:
            raise ValueError(
                "a research contract records the goal as the user stated it; that "
                "field is the record a later reader compares the work against"
            )
        problem = scientific_problem.strip()
        if not problem:
            raise ValueError(
                f"the goal {goal!r} has not been turned into a scientific problem. "
                "State what this project is trying to establish — a question that can "
                "be answered by evidence, not the goal restated"
            )
        contract = ResearchContract(
            project_id=context.project_id,
            original_user_goal=goal,
            scientific_problem=problem,
            research_hypotheses=_stated(hypotheses),
            target_metrics=_stated(target_metrics),
            acceptance_strategy=acceptance_strategy.strip(),
            known_constraints=_stated(known_constraints),
            prohibited_actions=_stated(prohibited_actions),
        )
        with context.write() as session:
            written = ResearchContractRepository(session, context.project_id).commit(
                contract, role=context.role
            )
        return {
            "contract": as_json(written),
            "what_happens_next": (
                "The project has a question. What it does about it is the plan: "
                "commit_roadmap_phase writes the stages, and expand_dag_phase commits "
                "the first stage's work."
            ),
        }

    return commit_research_contract


def _stated(items: list[str] | None) -> tuple[str, ...]:
    """A list of statements, with blanks dropped and nothing reworded.

    Blank entries are dropped rather than kept as empty strings: a contract
    whose `prohibited_actions` contains `""` reads as a restriction that was
    stated, and a reader counting them would count one that says nothing.
    """
    return tuple(stripped for item in items or () if (stripped := item.strip()))


def list_pending_approvals(context: ToolContext) -> Any:
    """List the questions waiting on a human."""

    async def list_pending_approvals() -> dict[str, Any]:
        """List approvals that no human has answered yet.

        Reaching for this is how you avoid asking twice, and how you tell
        waiting-on-a-human apart from waiting-on-yourself.
        """
        require_master(context, "list_pending_approvals")
        with context.read() as session:
            pending = ApprovalRepository(session, context.project_id).pending()
        return {"approvals": as_json(pending), "count": len(pending)}

    return list_pending_approvals


def write_master_checkpoint(context: ToolContext) -> Any:
    """Write the checkpoint a recovery would read."""

    async def write_master_checkpoint(
        current_focus: str,
        active_hypotheses: list[str] | None = None,
        pending_questions: list[str] | None = None,
        waiting_on: list[str] | None = None,
        recent_decision_refs: list[str] | None = None,
        important_context_refs: list[str] | None = None,
    ) -> dict[str, Any]:
        """Write down where your work stands, so a replacement session can continue it.

        Record what you are working on, what you are waiting for, and which
        decisions and context a successor should read. This is a navigational
        aid, not a record of truth: everything in it must be re-derivable from
        the project state, which is why the event sequence it was written at is
        stamped on it for the recovery to replay from.
        """
        require_master(context, "write_master_checkpoint")
        with context.write() as session:
            identity = AgentIdentityRepository(session, context.project_id).ensure(
                AgentRole.MASTER
            )
            checkpoint = MasterCheckpoint(
                project_id=context.project_id,
                master_identity_id=identity.identity_id,
                current_focus=current_focus,
                active_hypotheses=tuple(active_hypotheses or ()),
                pending_questions=tuple(pending_questions or ()),
                waiting_on=tuple(waiting_on or ()),
                recent_decision_refs=tuple(recent_decision_refs or ()),
                important_context_refs=tuple(important_context_refs or ()),
                last_event_seq=last_event_seq(session, context.project_id),
            )
            written = CheckpointRepository(session, context.project_id).write(
                checkpoint, actor_id=identity.identity_id
            )
        return as_json(written)

    return write_master_checkpoint


def read_master_checkpoint(context: ToolContext) -> Any:
    """Read the newest checkpoint, if there is one."""

    async def read_master_checkpoint() -> dict[str, Any]:
        """Read the most recent checkpoint written for this project.

        On recovery, read this first and then re-derive every claim in it from
        the project state before acting on it.
        """
        require_master(context, "read_master_checkpoint")
        with context.read() as session:
            checkpoint = CheckpointRepository(session, context.project_id).latest()
        return {
            "checkpoint": as_json(checkpoint) if checkpoint is not None else None,
            "found": checkpoint is not None,
        }

    return read_master_checkpoint


def commit_success_contract(context: ToolContext) -> Any:
    """Write down what would count as answering this project."""

    async def commit_success_contract(
        success_criteria: list[str],
        failure_criteria: list[str] | None = None,
        termination_criteria: list[str] | None = None,
        unresolved_uncertainty_policy: str = "",
        budget_time_limits: dict[str, Any] | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        """Freeze what this project would have to show for itself to have succeeded.

        **This is the definition every ending is measured against, and it is
        frozen before any work runs.** A20's first three endings — success,
        failure, and *inconclusive* — are all claims about results, and each is
        refused while the project has no frozen definition to make the claim
        against: a project that decides what success meant after seeing what it
        got has not concluded anything. So this is written early, and the
        project cannot be concluded without it.

        `success_criteria` are what would count as answering the question;
        `failure_criteria` are what would count as answering it the other way,
        and are worth stating rather than leaving to be inferred;
        `termination_criteria` are the conditions under which the project
        should be stopped rather than finished — a budget, a deadline, a
        result that would arrive too late to matter. `unresolved_uncertainty_
        policy` says what to do when the evidence runs out: this is what an
        INCONCLUSIVE ending is measured against, so a project whose policy is
        "keep going" cannot record one.

        It is a versioned record, not an editable one. The first version is
        written once and is what moves the project out of CREATED. A later
        version is a different act — the project was aiming somewhere and now
        aims somewhere else — so it requires `rationale`, and the change is
        recorded as a route change with that reasoning attached.

        It changes no node and commits no stage, so it is not a DAG mutation.
        """
        require_master(context, "commit_success_contract")
        criteria = _stated(success_criteria)
        if not criteria:
            raise ValueError(
                "a success contract's whole purpose is the list of things that would "
                "count as answering; stating it without any leaves every ending "
                "unmeasurable, which is the state this contract exists to end"
            )
        contract = ProjectSuccessContract(
            project_id=context.project_id,
            success_criteria=criteria,
            failure_criteria=_stated(failure_criteria),
            termination_criteria=_stated(termination_criteria),
            unresolved_uncertainty_policy=unresolved_uncertainty_policy.strip(),
            budget_time_limits=(
                BudgetLimits(**budget_time_limits) if budget_time_limits else BudgetLimits()
            ),
        )
        with context.write() as session:
            service = MasterService(session, context.project_id)
            version = service.success.next_version()
            contract = contract.model_copy(update={"version": version})
            revision = version > 1
            if revision and not rationale.strip():
                raise ValueError(
                    "this project already has a success contract; changing what it is "
                    "aiming at is a different act from defining it, and it is recorded "
                    "with the reasoning that made you change it"
                )
            written = service.define_success(
                contract,
                role=context.role,
                decision=(
                    DecisionDraft(
                        decision_type=DecisionType.CHANGE_ROUTE,
                        rationale=rationale,
                    )
                    if revision
                    else None
                ),
            )
        return {
            "contract": as_json(written),
            "revision": revision,
            "what_happens_next": (
                "Every ending is now measured against this. The plan comes next: "
                "commit_roadmap_phase writes the stages, and the project becomes "
                "EXECUTING when its first node starts running."
                if not revision
                else "The project is aiming somewhere else from here; the new version "
                "supersedes the one it replaced, and a reader finds both."
            ),
        }

    return commit_success_contract


def conclude_project(context: ToolContext) -> Any:
    """Record how the project ended."""

    async def conclude_project(
        outcome: str,
        rationale: str,
        reason: str = "",
    ) -> dict[str, Any]:
        """End this project, and record which of A20's four endings it is.

        This is the last act of a project and it is Master's alone. Call it
        when the loop reports that the project has stopped and only an ending
        is left — that report means no node can move and none of the three
        powers has anything to do, not that the project succeeded; whether what
        stopped was a success is the judgement this tool records.

        `outcome` is one of `SUCCESS`, `FAILED`, `INCONCLUSIVE` or
        `TERMINATED`, and the four are not interchangeable. The first three are
        *claims about results*, and each is refused while the answer is still
        outstanding: work that has not ended could still change it, a deviation
        nobody answered is a question the project left open, and a project with
        no frozen success contract has nothing to measure the claim against.
        `TERMINATED` is different — it is a statement about the work rather
        than about the answer, it is available at any time, and it is the one
        ending that cancels what is still running. It is what to record when
        the project is being stopped rather than finished, and `reason` is
        required for it so that a deliberate stop and a project that ran out of
        road do not read the same.

        `rationale` is the decision, and it is what a later reader uses to
        understand why the project ended where it did. A refusal here is not a
        formality: it names the nodes still unfinished, the deviations still
        unanswered, or the contract still missing, and resolving those is
        yours to do. If some of them can never finish, cancel them first and
        then conclude — a node nothing can execute is still a node that has not
        ended.
        """
        require_master(context, "conclude_project")
        try:
            ending = ProjectOutcome(outcome.strip().upper())
        except ValueError as error:
            raise ValueError(
                f"{outcome!r} is not an ending. A20 names four: "
                + ", ".join(member.value for member in ProjectOutcome)
            ) from error
        draft = DecisionDraft(
            decision_type=ENDING_DECISION[ending],
            rationale=rationale,
        )
        with context.write() as session:
            conclusion = MasterService(session, context.project_id).conclude(
                ending, role=context.role, decision=draft, reason=reason
            )
        return {
            "project": as_json(conclusion.project),
            "outcome": conclusion.outcome.value,
            "status": conclusion.status.value,
            "decision_id": conclusion.decision.decision_id,
            "cancelled_nodes": list(conclusion.cancelled),
        }

    return conclude_project


IMPLEMENTATIONS: dict[str, Any] = {
    "whoami": whoami,
    "read_project_state": read_project_state,
    "commit_research_contract": commit_research_contract,
    "commit_success_contract": commit_success_contract,
    "conclude_project": conclude_project,
    "list_pending_approvals": list_pending_approvals,
    "write_master_checkpoint": write_master_checkpoint,
    "read_master_checkpoint": read_master_checkpoint,
}
