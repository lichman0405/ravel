"""What a person may change about a running project, and nothing else.

`docs/08` gives the owner three controls: pause, resume, and "modify Authority
Envelope through controlled command". An approval is answered by whoever the
request names as its resolver. Those four are the whole of this module, and the
list is short on purpose — `docs/09`'s separation of powers is mostly a
statement about what a user credential does *not* reach, and the DAG is the
thing it does not reach. An owner who wants the graph changed says so to
Master, in words, and Master decides.

**Pause and resume are transitions, not flags.** Both go through
`ProjectRegistry.transition`, which consults the domain's transition table and
lets the database's own constraint refuse what the table forbids. Nothing here
re-implements the lifecycle, so there is no second copy of it to fall out of
step, and an illegal move — pausing a project that has not been planned yet —
is answered by the rule rather than by a check somebody remembered to write.

**The envelope gains versions.** It is never edited, because
`AuthorityEnvelope` is a record of what Master was permitted at a moment and an
edited one would describe a permission that was never actually granted. The
route appends the next version; `version`, `envelope_id` and `project_id` come
from the server, and a caller that sends them is ignored rather than obeyed.

**An approval's answer is checked where the record is written.** This module
does not decide who may resolve one: `ApprovalRepository.resolve` does, against
the request's own `required_role`. A route that repeated the check would be a
second place for it to be wrong, and the interesting case — an approval whose
resolver is a lab user — is one only the record knows about.
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ravel.domain.contracts import ApprovalRequirement, AuthorityEnvelope, BudgetLimits
from ravel.domain.enums import ApprovalStatus, ProjectStatus, UserRole
from ravel.domain.events import ActorType
from ravel.gateway.deps import DirectorDep, GatewayStateDep, PrincipalDep
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import AuthorityEnvelopeRepository
from ravel.state.repositories.identity import ApprovalRepository
from ravel.state.repositories.projects import ProjectRegistry

router = APIRouter(prefix="/projects", tags=["control"])

#: The two answers a person gives. `PENDING` is not one — it is the absence of
#: an answer — and `EXPIRED` is the system's, not a human's.
Resolution = Literal[ApprovalStatus.APPROVED, ApprovalStatus.REJECTED]


class PauseRequest(BaseModel):
    """Why the project is being stopped. Recorded, and may be empty."""

    reason: str = Field(default="", max_length=2000)


class EnvelopeRequirement(BaseModel):
    """One class of action a human must approve before Master may take it."""

    action: str = Field(min_length=1, max_length=200)
    required_role: UserRole
    rationale: str = Field(default="", max_length=2000)


class EnvelopeLimits(BaseModel):
    """The envelope's budget. Every field absent means unbounded."""

    max_wall_clock_hours: float | None = Field(default=None, gt=0)
    max_compute_hours: float | None = Field(default=None, gt=0)
    max_iterations: int | None = Field(default=None, gt=0)
    max_experiment_runs: int | None = Field(default=None, gt=0)
    max_cost_units: float | None = Field(default=None, gt=0)


class EnvelopeRequest(BaseModel):
    """A new version of the envelope.

    There is no `version` field, and that is the point: which version this is
    is a fact about the record, not a claim the caller gets to make. A body
    that carried one would let two clients both write version 4 and one of them
    silently lose.
    """

    requires_approval: list[EnvelopeRequirement] = Field(default_factory=list)
    max_dag_nodes_without_approval: int | None = Field(default=None, gt=0)
    max_parallel_branches_without_approval: int | None = Field(default=None, gt=0)
    budget_time_limits: EnvelopeLimits = Field(default_factory=EnvelopeLimits)


class ApprovalResolution(BaseModel):
    """A person's answer to a request for authority."""

    status: Resolution
    note: str = Field(default="", max_length=2000)


# ── The project's life cycle ────────────────────────────────────────────────


@router.post("/{project_id}/pause", summary="Stop the project where it stands")
def pause(
    standing: DirectorDep, state: GatewayStateDep, request: PauseRequest | None = None
) -> dict[str, str]:
    """Move the project to `PAUSED`, recording why.

    Pausing does not stop a running turn or cancel a node: the nodes in flight
    are already someone's work, and a pause that reached into them would be a
    cancellation wearing a different word. What it stops is the project moving
    on — the loop's own reading of the status is what holds it.

    Raises:
        HTTPException: 409, through the domain's transition table, if the
            project is not in a state that can be paused.
    """
    return _move(
        standing,
        state,
        ProjectStatus.PAUSED,
        reason=(request.reason if request is not None else ""),
    )


@router.post("/{project_id}/resume", summary="Let the project move on again")
def resume(standing: DirectorDep, state: GatewayStateDep) -> dict[str, str]:
    """Move the project back to `EXECUTING`.

    Raises:
        HTTPException: 409 if the project is not paused.
    """
    return _move(standing, state, ProjectStatus.EXECUTING, reason=None)


# ── The Authority Envelope ──────────────────────────────────────────────────


@router.get("/{project_id}/envelope", summary="What Master may decide alone")
def read_envelope(standing: PrincipalDep, state: GatewayStateDep) -> dict[str, object] | None:
    """The envelope in force, or `null` if none has been set.

    `null` is not a 404. The project exists and the caller may see it; it
    simply has no envelope yet, which is the state every project starts in and
    in which Master's authority is whatever the deployment's defaults say. A
    reader that could not tell that apart from a missing project would draw the
    wrong conclusion about both.
    """
    with state.database.read_only() as session:
        envelope = _latest_envelope(session, standing.project_id)
    return envelope.model_dump(mode="json") if envelope is not None else None


@router.post("/{project_id}/envelope", summary="Set a new Authority Envelope")
def write_envelope(
    standing: DirectorDep, state: GatewayStateDep, request: EnvelopeRequest
) -> dict[str, object]:
    """Append the next version of the envelope, and return it.

    The whole envelope is sent, not a patch. A partial update would need a
    merge rule, and the only merge rule that means anything here is "the last
    one silently won" — which is how a permission that was never granted ends
    up being the one in force. Sending the whole thing makes every version a
    complete statement of what Master may do.
    """
    with state.database.transaction() as session:
        repository = AuthorityEnvelopeRepository(session, standing.project_id)
        envelope = repository.add_version(
            AuthorityEnvelope(
                project_id=standing.project_id,
                version=repository.next_version(),
                requires_approval=tuple(
                    ApprovalRequirement(
                        action=item.action,
                        required_role=item.required_role,
                        rationale=item.rationale,
                    )
                    for item in request.requires_approval
                ),
                max_dag_nodes_without_approval=request.max_dag_nodes_without_approval,
                max_parallel_branches_without_approval=(
                    request.max_parallel_branches_without_approval
                ),
                budget_time_limits=BudgetLimits(**request.budget_time_limits.model_dump()),
            )
        )
    return envelope.model_dump(mode="json")


# ── Approvals ───────────────────────────────────────────────────────────────


@router.get("/{project_id}/approvals", summary="Questions waiting on a human")
def read_approvals(
    standing: PrincipalDep,
    state: GatewayStateDep,
    pending_only: Annotated[
        bool, Query(description="Answer only with the requests still unanswered.")
    ] = True,
) -> list[dict[str, object]]:
    """Approval requests, oldest first.

    Pending by default, because a list of answered questions is a history and
    the reason to open this screen is that something is waiting. Asking for the
    whole history is a query parameter rather than a second route.
    """
    with state.database.read_only() as session:
        repository = ApprovalRepository(session, standing.project_id)
        found = repository.pending() if pending_only else repository.all()
    return [approval.model_dump(mode="json") for approval in found]


@router.post("/{project_id}/approvals/{approval_id}/resolve", summary="Answer one")
def resolve_approval(
    standing: PrincipalDep,
    state: GatewayStateDep,
    approval_id: str,
    request: ApprovalResolution,
) -> dict[str, object]:
    """Record this caller's answer.

    The resolver is the authenticated caller and is not a parameter: a body
    that named one would let a caller answer as somebody else, which is the
    separation failing in the one place it exists to hold. The answer is
    checked against the request's own `required_role`, so a lab user may answer
    a question addressed to lab users and nothing else.

    Raises:
        HTTPException: 403 if the caller does not hold the authority the
            request names; 404 if there is no such request in this project.
    """
    with state.database.transaction() as session:
        resolved = ApprovalRepository(session, standing.project_id).resolve(
            approval_id,
            ApprovalStatus(request.status),
            resolved_by=standing.user_id,
            note=request.note,
        )
    return resolved.model_dump(mode="json")


# ── Shared ──────────────────────────────────────────────────────────────────


def _move(
    standing: DirectorDep, state: GatewayStateDep, target: ProjectStatus, *, reason: str | None
) -> dict[str, str]:
    """Move a project and report where it ended up.

    A no-op when it is already there — `transition` returns the project
    unchanged rather than faulting — so a client that retries a pause it is not
    sure landed gets the same answer as the client that got there first, and no
    duplicate event.
    """
    with state.database.transaction() as session:
        project = ProjectRegistry(session).transition(
            standing.project_id,
            target,
            actor_id=standing.user_id,
            actor_type=ActorType.USER,
            reason=reason or None,
        )
    return {"project_id": project.project_id, "status": project.status.value}


def _latest_envelope(session: Session, project_id: str) -> AuthorityEnvelope | None:
    """The envelope in force, or `None` when the project has not got one.

    `latest()` raises `NotFound` for an empty family, which is the repository
    being precise about contracts. Here the absence is an ordinary state, so it
    is turned into `None` at the one place that knows the difference.
    """
    try:
        envelope: AuthorityEnvelope = AuthorityEnvelopeRepository(
            session, project_id
        ).latest()
    except NotFound:
        return None
    return envelope


__all__ = [
    "ApprovalResolution",
    "EnvelopeLimits",
    "EnvelopeRequest",
    "EnvelopeRequirement",
    "PauseRequest",
    "router",
]
