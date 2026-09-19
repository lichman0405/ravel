"""The Workers' surface: what they act under, and what they may say.

`docs/02` §5 gives the Experimental Worker one rule — *is this action
explicitly allowed by the Execution Contract?* — and states that it does not
classify execution questions from science questions using judgement. That rule
is executable: `ravel.execution.worker_rules` answers it by lookup, and these
tools are how a Worker session gets to ask.

Which makes the shape of this module deliberate and narrow. A Worker holds:

- the **terms** it acts under, so that "execute the frozen contract exactly"
  is something it can read rather than something it is told to remember;
- the **state** of its task, so that it can report what happened rather than
  what it believes happened;
- and exactly two writing tools, both of which record *what the Worker said*
  rather than what the project should do.

There is deliberately no tool here that changes a scientific parameter, writes
an Execution Record, or moves work on. The invocation, the waiting, the
collection and the completeness check are the durable layer's — they are
deterministic, they must survive a worker being killed, and `docs/14`'s rule
about not handing a deterministic problem to a model is the same rule as
`docs/02`'s about a Worker not making scientific decisions. What is left for a
Worker session is the part that is genuinely its own: observing something the
backend did not report, asking the contract about it, and saying what it found.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from ravel.domain.contracts import ExecutionContract
from ravel.domain.dag import DagNode
from ravel.domain.enums import NodeStatus, WorkerMessageKind
from ravel.domain.execution import DeviationRecord, WorkerMessage
from ravel.domain.state_machines import can_transition_node
from ravel.execution.backends import DeviationReport
from ravel.execution.worker_rules import adjudicate
from ravel.mcp.context import ToolContext, as_json, require_worker
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import RecordRepositories

#: The statuses a Worker may stop. A task is live while it is waiting to run or
#: running; everything else is either finished or somebody else's to move, and a
#: Worker that stopped work that was not going would be recording a question
#: about nothing.
LIVE_STATUSES = (NodeStatus.READY, NodeStatus.RUNNING)

#: The kinds a Worker may say without naming something in the contract.
#: ESCALATE is the one: it is how a Worker asks for authority it does not have,
#: and refusing it would be refusing the request for permission — which is the
#: only route by which a contract is ever widened.
UNCHECKED_KINDS = (WorkerMessageKind.ESCALATE,)


def read_execution_contract(context: ToolContext) -> Any:
    """The frozen terms this Worker acts under."""

    async def read_execution_contract(node_id: str) -> dict[str, Any]:
        """Read the frozen Execution Contract for one node.

        This is the whole of a Worker's authority. `allowed_actions` is a
        closed list: an action absent from it is not "probably fine", it is
        forbidden. `allowed_ranges` gives the interval each parameter may be
        set to, `allowed_substitutions` the exact replacement pairs permitted,
        and `required_outputs` what has to be delivered before the work counts
        as delivered at all.

        Read it before acting, and read it again rather than recalling it: a
        contract Master revised while you worked is a new version, and the one
        that governs your run is the one bound to this node now.
        """
        require_worker(context, "read_execution_contract")
        with context.read() as session:
            node = DagRepository(session, context.project_id).node(node_id)
            contract = ExecutionContractRepository(session, context.project_id).for_node(
                node_id
            )
        return {
            "node": {
                "node_id": node.node_id,
                "display_id": node.display_id,
                "node_type": node.node_type.value,
                "objective": node.objective,
                "status": node.status.value,
            },
            "contract": _contract(contract),
            "what_this_session_may_do": [
                "read the contract and the record of what happened",
                "ask the contract whether an action is permitted",
                "say one of the four permitted things to the lab",
            ],
            "what_this_session_may_not_do": [
                "choose a method, model or parameter the contract does not name",
                "decide whether a result passes; Review does that",
                "move the node on, or change the plan; Master does that",
            ],
        }

    return read_execution_contract


def _contract(contract: ExecutionContract) -> dict[str, Any]:
    """The terms, with the parts a Worker acts on spelled out."""
    return {
        "contract_id": contract.contract_id,
        "version": contract.version,
        "objective": contract.objective,
        "procedure": contract.procedure,
        "inputs": list(contract.inputs),
        "allowed_actions": list(contract.allowed_actions),
        "parameter_targets": dict(contract.parameter_targets),
        "allowed_ranges": dict(contract.allowed_ranges),
        "allowed_substitutions": list(contract.allowed_substitutions),
        "required_outputs": list(contract.required_outputs),
        "allowed_retries": contract.allowed_retries,
        "stop_conditions": list(contract.stop_conditions),
        "escalation_conditions": list(contract.escalation_conditions),
        "resource_limits": dict(contract.resource_limits),
        "acceptance_contract_ref": contract.acceptance_contract_ref,
        "is_frozen": contract.is_frozen,
    }


def read_execution_status(context: ToolContext) -> Any:
    """What has happened to this task so far."""

    async def read_execution_status(node_id: str) -> dict[str, Any]:
        """Read the record of what actually happened on this node.

        `job` is the work in flight: which backend holds it, which attempt it
        is, what the backend says its own state is, and whether it has reported
        something it may not be permitted to do. `execution` is the immutable
        record of a run that has ended, including every attempt and what was
        delivered against what was required — read `completeness` before
        claiming the work is done, because a delivered set that is missing a
        required output is not a delivery.

        `deviations` are escalations raised on this node, open ones first, and
        `messages` is what this task has said to the lab. Together they are the
        honest answer to "what is this task waiting on", which is a different
        question from "what is it doing".
        """
        require_worker(context, "read_execution_status")
        with context.read() as session:
            node = DagRepository(session, context.project_id).node(node_id)
            records = RecordRepositories(session, context.project_id)
            job = records.jobs.latest_for_node(node_id)
            execution = records.latest_execution(node_id)
            deviations = [
                deviation
                for deviation in records.deviations.all()
                if deviation.node_id == node_id
            ]
            messages = records.messages.for_node(node_id)
            contract = ExecutionContractRepository(session, context.project_id).for_node(
                node_id
            )
        return {
            "node_status": node.status.value,
            "contract_version": contract.version,
            "job": as_json(job) if job is not None else None,
            "execution": as_json(execution) if execution is not None else None,
            "deviations": as_json(deviations),
            "open_deviations": [
                deviation.deviation_id for deviation in deviations if deviation.is_open
            ],
            "messages": as_json(messages),
            "required_outputs": list(contract.required_outputs),
        }

    return read_execution_status


def request_action(context: ToolContext) -> Any:
    """Ask the contract about an action, and let RAVEL answer."""

    async def request_action(
        node_id: str,
        requested_action: str,
        description: str = "",
        parameter: str = "",
        value: float | None = None,
        substitute: list[str] | None = None,
    ) -> dict[str, Any]:
        """Ask whether the Execution Contract permits something.

        **RAVEL answers, not you.** This is the Worker's one rule, executed:
        the contract is consulted and the verdict returned with the reason it
        reached. When the answer is yes, the work carries on and a confirmation
        is recorded. When the answer is no, a deviation is raised for Master,
        an escalation is recorded, and this task stops — because the only role
        that may widen a contract is the one that wrote it.

        You are never asked whether a change is scientifically reasonable, and
        this tool does not take that as input. Say what you have been asked
        for; RAVEL says whether the contract names it.

        `requested_action` is what the backend or the lab asked for, in the
        contract's own vocabulary. Fill in `parameter` and `value` when the
        question is about a value the backend can actually reach — the
        reachable value is the one checked, because it is what would happen —
        and `substitute` with a `[given, instead]` pair when it is about a
        replacement. Anything else is checked against `allowed_actions`.

        Silence is refusal: a contract that does not mention a parameter has
        not permitted every value of it.
        """
        require_worker(context, "request_action")
        report = DeviationReport(
            requested_action=requested_action,
            description=description,
            parameter=parameter,
            value=value,
            substitution=_substitution(substitute),
        )
        with context.write() as session:
            node = DagRepository(session, context.project_id).node(node_id)
            contracts = ExecutionContractRepository(session, context.project_id)
            contract = contracts.for_node(node_id)
            verdict = adjudicate(report, contract)
            records = RecordRepositories(session, context.project_id)
            if verdict.permitted:
                records.messages.record(
                    WorkerMessage(
                        project_id=context.project_id,
                        node_id=node_id,
                        kind=verdict.statement.kind,
                        body=verdict.statement.body,
                    )
                )
                return {
                    "permitted": True,
                    "requested_action": verdict.requested_action,
                    "reason": verdict.reason,
                    "contract_version": contract.version,
                    "deviation_id": None,
                    "node_status": node.status.value,
                }
            deviation = _raise(
                context,
                node,
                contract,
                verdict.requested_action,
                verdict.reason,
                records,
            )
        status, ending = _stop_the_task(context, node_id)
        return {
            "permitted": False,
            "requested_action": verdict.requested_action,
            "reason": verdict.reason,
            "contract_version": contract.version,
            "deviation_id": deviation.deviation_id,
            "node_status": status,
            "task_stopped": ending is _Ending.STOPPED,
            "what_happens_next": _next_step(ending),
        }

    return request_action


def _substitution(substitute: list[str] | None) -> tuple[str, str] | None:
    """The replacement pair, checked for shape before it is checked against
    anything.

    Raises:
        ValueError: It is not exactly two names. A one-name substitution is a
            request to replace something with nothing, which is a different
            question and not one the contract can answer.
    """
    if substitute is None:
        return None
    if len(substitute) != 2:
        raise ValueError(
            f"a substitution is a pair, [given, instead], and this has "
            f"{len(substitute)} element(s); a replacement of one name for nothing "
            "is not a substitution"
        )
    return (substitute[0], substitute[1])


def _raise(
    context: ToolContext,
    node: DagNode,
    contract: ExecutionContract,
    requested_action: str,
    reason: str,
    records: RecordRepositories,
) -> DeviationRecord:
    """Record the refusal and the escalation, in the caller's transaction.

    Both rows or neither: a deviation that exists always has the message that
    says the Worker did not answer the question itself, which is the whole of
    what the separation of powers asks of this act.
    """
    deviation = records.deviations.raise_(
        DeviationRecord(
            project_id=context.project_id,
            node_id=node.node_id,
            execution_contract_ref=contract.contract_id,
            requested_action=requested_action,
            description=reason,
            permitted=False,
            raised_by=context.role.value,
        )
    )
    records.messages.record(
        WorkerMessage(
            project_id=context.project_id,
            node_id=node.node_id,
            kind=WorkerMessageKind.ESCALATE,
            body=(
                f"the execution contract does not permit {requested_action}: {reason}. "
                "This requires Master's decision; the Worker has not acted and has "
                "not answered the question."
            ),
        )
    )
    return deviation


class _Ending(StrEnum):
    """Where a refused request leaves the task.

    Four cases, kept apart because the model is told what happened and a reply
    that said "stopped" in all of them would be a claim about the DAG that the
    DAG does not support — which is the one thing a Worker's report may not be.
    """

    #: This call moved the node to WAITING_DECISION.
    STOPPED = "STOPPED"
    #: A run is in flight, and where it ends is the run's own ending to report.
    IN_FLIGHT = "IN_FLIGHT"
    #: It was already waiting on a decision, and it still is.
    ALREADY = "ALREADY"
    #: The task was not going anywhere, so there was nothing to stop.
    NOT_LIVE = "NOT_LIVE"


def _stop_the_task(context: ToolContext, node_id: str) -> tuple[str, _Ending]:
    """Stop the task at WAITING_DECISION, and say honestly what that took.

    A second transaction on purpose. Stopping is a status change on the DAG,
    and the deviation has to be committed before it: a node waiting on a
    question that is not in the record is a node nobody can unblock, which is
    worse than a recorded question on a node still shown as live.

    Nothing else in RAVEL moves a node out of WAITING_DECISION — Master's
    answer is the only path — so a Worker that raised a deviation and did not
    stop the task would leave the question answerable and the work stopped
    forever. Which is why the cases where this declines to move the node are
    the cases where something else owns the ending: a run that is in flight
    will report its own, and a task that was never live has nothing to stop.
    """
    with context.write() as session:
        node = DagRepository(session, context.project_id).node(node_id)
        if node.status not in LIVE_STATUSES:
            # Not going anywhere: either it already ended, or the ending is
            # somebody else's to decide. Either way this call moves nothing.
            already_waiting = node.status is NodeStatus.WAITING_DECISION
            return node.status.value, (
                _Ending.ALREADY if already_waiting else _Ending.NOT_LIVE
            )
        job = RecordRepositories(session, context.project_id).jobs.latest_for_node(node_id)
        if job is not None and not job.is_terminal:
            return node.status.value, _Ending.IN_FLIGHT
        # Asked of the state machine rather than assumed: this tool is
        # reachable from a session whose node may have moved while it thought.
        if not can_transition_node(node.status, NodeStatus.WAITING_DECISION).allowed:
            return node.status.value, _Ending.NOT_LIVE
        stopped = DagRepository(session, context.project_id).transition_node(
            node_id, NodeStatus.WAITING_DECISION, actor_id=context.role.value
        )
        return stopped.status.value, _Ending.STOPPED


def _next_step(ending: _Ending) -> str:
    """What a refused request means for the task, said as what it is.

    The first sentence is the same in every case and is the whole of the
    separation of powers: the Worker asked, RAVEL answered, and the answer is a
    question for Master rather than a decision by the Worker. The second says
    what happened to the work, which is the part that differs.
    """
    asked = (
        "Master has been asked, and this task has not answered the question "
        "itself. Nothing about the work changes until a Decision Record answers "
        "the deviation."
    )
    return {
        _Ending.STOPPED: f"{asked} The task is now stopped at WAITING_DECISION.",
        _Ending.IN_FLIGHT: (
            f"{asked} The node is left where it is: a run is in flight for it, and "
            "where that run ends is the run's own ending to report."
        ),
        _Ending.ALREADY: f"{asked} The task was already waiting on a decision.",
        _Ending.NOT_LIVE: f"{asked} The task was not running, so nothing was stopped.",
    }[ending]


def send_message(context: ToolContext) -> Any:
    """Say one of the four permitted things, about something in the contract."""

    async def send_message(
        node_id: str,
        kind: str,
        body: str,
        about: str = "",
    ) -> dict[str, Any]:
        """Send one message to the lab, inside the four permitted kinds.

        CONFIRM, INFORM and REQUEST_MISSING_INFORMATION are things a Worker
        says *from the contract*, so each has to name what it is about — an
        action the contract lists, an output it requires, or a stop condition
        it declares — and a message about anything else is refused. That is the
        "INFORM from contract" rule made checkable: the alternative is a Worker
        negotiating terms in prose that nobody can audit.

        ESCALATE needs nothing named. It is how a Worker asks for authority it
        does not have, and refusing it would close the only route by which a
        contract is ever widened.

        A question from an operator is not answered here, however obvious the
        answer looks. Answering would be the Worker making a scientific
        decision, and that belongs to Master.
        """
        require_worker(context, "send_message")
        parsed = _kind(kind)
        with context.write() as session:
            node = DagRepository(session, context.project_id).node(node_id)
            contract = ExecutionContractRepository(session, context.project_id).for_node(
                node_id
            )
            approved = _is_about_the_contract(contract, about)
            if parsed not in UNCHECKED_KINDS and not approved:
                raise ValueError(
                    f"a {parsed.value} message has to be about something the contract "
                    f"names, and {about!r} is not one of them. The contract lists the "
                    f"actions {list(contract.allowed_actions)}, the outputs "
                    f"{list(contract.required_outputs)} and the stop conditions "
                    f"{list(contract.stop_conditions)}. If what you need to say is "
                    "not in that list, ESCALATE it to Master."
                )
            message = RecordRepositories(session, context.project_id).messages.record(
                WorkerMessage(
                    project_id=context.project_id,
                    node_id=node_id,
                    kind=parsed,
                    body=body,
                    approved_by_contract=approved or parsed in UNCHECKED_KINDS,
                )
            )
        return {**as_json(message), "node_status": node.status.value}

    return send_message


def _kind(value: str) -> WorkerMessageKind:
    """The kind the model named.

    Raises:
        ValueError: It is not one of the four. The list is closed because a
            Worker that can say anything can negotiate outside its contract.
    """
    try:
        return WorkerMessageKind(value)
    except ValueError as exc:
        known = ", ".join(kind.value for kind in WorkerMessageKind)
        raise ValueError(f"kind must be one of {known}; got {value!r}") from exc


def _is_about_the_contract(contract: ExecutionContract, about: str) -> bool:
    """Whether what a message is about is something the contract names."""
    if not about.strip():
        return False
    return about in (
        *contract.allowed_actions,
        *contract.required_outputs,
        *contract.stop_conditions,
        *contract.escalation_conditions,
    )


IMPLEMENTATIONS: dict[str, Any] = {
    "read_execution_contract": read_execution_contract,
    "read_execution_status": read_execution_status,
    "request_action": request_action,
    "send_message": send_message,
}
