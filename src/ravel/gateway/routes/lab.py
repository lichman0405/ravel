"""What the person running the experiment needs, and the one thing they can say.

`docs/08` lists the lab user's screen: the assigned task, the approved
procedure, the allowed ranges, the required deliverables, the status, a way to
report a problem, a way to upload, and the updated instruction they get back.
All but one of those are reads of records Master already wrote, and the whole
surface is arranged around one asymmetry.

**A lab user reads and reports; they do not decide.** Every route here needs
membership and nothing more, and the single write is the one thing a person
standing at a bench can contribute that nobody else can: the observation that
reality did not match the plan. Reporting a deviation *records* that the
contract did not permit what was asked — it does not grant the permission, does
not stop the node, and does not decide that the request was wrong. Master reads
it on the next turn; the record is what makes the question exist.

**The task is the node, and the instruction is the contract.** There is no
separate "lab instruction" table, because inventing one would be a second
source of truth about what a worker is supposed to do. The approved procedure is
`ExecutionContract.procedure`, the allowed ranges are
`ExecutionContract.allowed_ranges`, and the required deliverables are
`ExecutionContract.required_outputs` — read here, not restated. An *updated*
instruction is therefore a new version of that contract, which is why every
response carries the version and the freeze time: a lab user who has been told
something different needs to be able to see when it changed, and a contract is
never edited in place.

**Who a task is assigned to is a role, not a person.** A node carries
`executor_role`, and every member sees the experiment nodes. A per-person
assignment column would be a claim about who was at the bench that RAVEL has no
way to keep true, and a stale assignment is worse than none. What actually says
who did the work is the artifact's `created_by` and the deviation's `raised_by`,
both of which are written by the person themselves.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ravel.domain.dag import DagNode
from ravel.domain.execution import DeviationRecord
from ravel.domain.roles import AgentRole
from ravel.gateway.deps import GatewayStateDep, PrincipalDep
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import (
    BackendJobRepository,
    DeviationRepository,
    WorkerMessageRepository,
)

router = APIRouter(prefix="/projects", tags=["lab"])

#: The role a task has to be executed by to appear on this screen. Named rather
#: than inlined because it is the whole definition of "a lab task": an
#: EXPERIMENT node is executed by the Experimental Worker, and `DagNode` refuses
#: to be built with any other executor for that type. That the two agree is
#: asserted in `tests/integration/gateway/test_lab.py` rather than here, so a
#: domain change fails a test that says what it broke instead of an import.
LAB_ROLE = AgentRole.EXPERIMENTAL_WORKER


class DeviationRequest(BaseModel):
    """What a lab user reports when the plan and the bench disagree."""

    #: The action that was refused, named as the contract names it, so Master
    #: can compare it against `allowed_actions` without guessing at synonyms.
    requested_action: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)


def _instruction(session: Session, project_id: str, node: DagNode) -> dict[str, Any] | None:
    """The contract a lab user is working under, or `None` if none is bound.

    `None` is a real answer and an important one: a node that has been planned
    but whose contract has not been written is a task nobody should start, and
    a screen that rendered an empty procedure instead would look like a task
    with nothing to do.
    """
    if not node.execution_contract_ref:
        return None
    try:
        contract = ExecutionContractRepository(session, project_id).get(
            contract_id=node.execution_contract_ref
        )
    except NotFound:
        return None
    return contract.model_dump(mode="json")


def _task(session: Session, project_id: str, node: DagNode) -> dict[str, Any]:
    """One row of the lab screen.

    Everything a reader needs about one task, gathered over a session the caller
    opened, so that listing twenty tasks is one connection rather than twenty.
    """
    latest = BackendJobRepository(session, project_id).latest_for_node(node.node_id)
    return {
        "node": node.model_dump(mode="json"),
        "instruction": _instruction(session, project_id, node),
        "backend_job": None if latest is None else latest.model_dump(mode="json"),
        "messages": [
            message.model_dump(mode="json")
            for message in WorkerMessageRepository(session, project_id).for_node(node.node_id)
        ],
        "deviations": [
            deviation.model_dump(mode="json")
            for deviation in DeviationRepository(session, project_id).all(node_id=node.node_id)
        ],
    }


def _experiment_node(session: Session, project_id: str, task_id: str) -> DagNode:
    """The node, or a refusal.

    Raises:
        HTTPException: 404 if this project has no such node, or if it is not an
            experiment. The second is not hidden behind the first — a node that
            exists and is not a lab task is not a secret from a project member,
            and answering "no such task" for it would send them looking for a
            typo in an identifier they got right.
    """
    try:
        node = DagRepository(session, project_id).get(node_id=task_id)
    except NotFound as missing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no task {task_id!r} in this project",
        ) from missing
    if node.executor_role is not LAB_ROLE:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"node {task_id!r} is not an experiment task",
        )
    return node


@router.get("/{project_id}/lab/tasks", summary="The experiment tasks this project has")
def list_tasks(
    standing: PrincipalDep,
    state: GatewayStateDep,
    node_id: Annotated[
        str | None, Query(description="Only this node, for a lab user following one task.")
    ] = None,
) -> list[dict[str, Any]]:
    """Every experiment node, with the contract it runs under.

    Visible to every member rather than to lab users only, and deliberately: an
    owner who cannot see what the bench was told cannot tell whether an
    experiment went wrong or was asked to do the wrong thing. What the *lab
    user's screen* shows is its own choice; what the Gateway enforces is
    membership, as everywhere else.
    """
    with state.database.read_only() as session:
        nodes = DagRepository(session, standing.project_id).nodes()
        return [
            _task(session, standing.project_id, node)
            for node in nodes
            if node.executor_role is LAB_ROLE
            and (node_id is None or node.node_id == node_id)
        ]


@router.get("/{project_id}/lab/tasks/{task_id}", summary="One task, in full")
def read_task(
    standing: PrincipalDep, state: GatewayStateDep, task_id: str
) -> dict[str, Any]:
    """The node, its contract, its backend job, what it said, and what was reported."""
    with state.database.read_only() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        return _task(session, standing.project_id, node)


@router.post(
    "/{project_id}/lab/tasks/{task_id}/deviations",
    status_code=status.HTTP_201_CREATED,
    summary="Report that the plan and the bench disagree",
)
def report_deviation(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: DeviationRequest,
) -> dict[str, Any]:
    """Record something the contract did not permit.

    The record is written against the node's frozen contract, and it is written
    whether or not the requested action later turns out to be permitted — a
    fact this route deliberately does not decide. A lab user saying "I was asked
    for something the contract does not allow" is reporting an observation, and
    the observation is worth having even when Master then rules that the action
    was in fact permitted. `permitted` stays false because the *reporter* did
    not permit it, which is what makes this a report rather than an execution;
    what Master decides is written as a decision, and resolving the deviation
    points at it.

    Raises:
        HTTPException: 404 if this project has no such task. 409 if the node has
            no contract, because there is then nothing to have deviated from —
            a report against no contract would record a refusal that no
            document ever made.
    """
    with state.database.transaction() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        if not node.execution_contract_ref:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"node {node.display_id} has no execution contract, so there "
                    "is no permitted list for this to be a deviation from"
                ),
            )
        contract = ExecutionContractRepository(session, standing.project_id).get(
            contract_id=node.execution_contract_ref
        )
        deviation = DeviationRepository(session, standing.project_id).raise_(
            DeviationRecord(
                project_id=standing.project_id,
                node_id=task_id,
                execution_contract_ref=contract.contract_id,
                requested_action=request.requested_action,
                description=request.description,
                permitted=False,
                raised_by=standing.user_id,
            )
        )
    return deviation.model_dump(mode="json")


__all__ = ["LAB_ROLE", "DeviationRequest", "router"]
