"""The lab bench on the versioned surface.

Every route here is a call into the legacy laboratory handlers, and that is the
whole design. A handover, an upload and a deviation are records with rules
about who may write them and what they may answer; restating those rules under
a new prefix would be a second set of rules. The only thing `/v1` changes is
the list route's pagination, because a bench that has accumulated thousands of
tasks needs a stable cursor like every other collection.

**Sign-off has no service behind it, and says so.** `CONTRACTS.md` §8 names
sign-off among the laboratory actions to adapt, but V0 records the end of a
handover in the run that owned it, not through a human route, and there is no
service that could accept one without inventing an authority that does not
exist. The route answers 501 `UNSUPPORTED` with that explanation rather than
pretending to accept a signature nothing reads. When the service exists, this
is the one function that changes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import Response

from ravel.gateway.deps import GatewayStateDep, PrincipalDep
from ravel.gateway.routes import lab as legacy
from ravel.gateway.routes.lab import DeviationRequest
from ravel.gateway.routes.premium.errors import problem
from ravel.gateway.routes.premium.support import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    paginate,
)

router = APIRouter(tags=["v1-lab"])


@router.get(
    "/projects/{project_id}/lab/tasks",
    response_model=Page[dict[str, Any]],
    summary="The experiment tasks this project has",
)
def list_tasks(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
    node_id: Annotated[
        str | None,
        Query(description="Only this node, for a lab user following one task."),
    ] = None,
) -> Page[dict[str, Any]]:
    """The tasks the legacy screen shows, in the order the DAG created them.

    The pagination is applied to the same list the legacy route returns, so a
    task has exactly the fields the bench has always seen — the node, its
    instruction, the job, the handover, the deviations and the mechanism for
    reporting one.
    """
    found = legacy.list_tasks(standing, state, node_id=node_id)
    return paginate(
        found,
        key=lambda task: (
            task["node"]["created_at"],
            task["node"]["node_id"],
        ),
        limit=limit,
        cursor=cursor,
    )


@router.get(
    "/projects/{project_id}/lab/tasks/{task_id}",
    response_model=dict[str, Any],
    summary="One task, in full",
)
def read_task(
    standing: PrincipalDep, state: GatewayStateDep, task_id: str
) -> dict[str, Any]:
    """The same record the bench reads, including what is still owed."""
    return legacy.read_task(standing, state, task_id)


@router.get(
    "/projects/{project_id}/lab/tasks/{task_id}/handover/documents/{document}",
    response_class=Response,
    summary="One file out of the package the bench was given",
)
def read_document(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    document: str,
) -> Response:
    """The bytes RAVEL wrote, under the name the manifest recorded."""
    return legacy.read_document(standing, state, task_id, document)


@router.post(
    "/projects/{project_id}/lab/tasks/{task_id}/uploads",
    status_code=status.HTTP_201_CREATED,
    response_model=dict[str, Any],
    summary="Upload the file that answers one required output",
)
async def upload_output(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: Request,
    output: Annotated[
        str,
        Query(
            min_length=1,
            max_length=200,
            description="The required output this file answers.",
        ),
    ],
    filename: Annotated[str, Query(max_length=255)] = "",
    media_type: Annotated[str, Query(max_length=200)] = "",
) -> dict[str, Any]:
    """File the body against an output the handover actually owed."""
    return await legacy.upload_output(
        standing, state, task_id, request, output, filename, media_type
    )


@router.post(
    "/projects/{project_id}/lab/tasks/{task_id}/deviations",
    status_code=status.HTTP_201_CREATED,
    response_model=dict[str, Any],
    summary="Report that the plan and the bench disagree",
)
async def report_deviation(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: DeviationRequest,
) -> dict[str, Any]:
    """Record the observation and tell the run, exactly as the legacy route does."""
    return await legacy.report_deviation(standing, state, task_id, request)


@router.post(
    "/projects/{project_id}/lab/tasks/{task_id}/sign-off",
    response_model=dict[str, Any],
    summary="Acknowledge a handover (not available in V0)",
)
def sign_off(
    standing: PrincipalDep, state: GatewayStateDep, task_id: str
) -> dict[str, Any]:
    """Refuse, because there is no sign-off record to write.

    The membership check runs first, so a caller who may not see this project
    learns only that it has no such task — the same 404 a missing one gives.
    A member is told the truth: the action is not supported in this deployment
    and no record was created.
    """
    # Reading the task is the authorization check; it also proves the task
    # exists, so the refusal cannot be used to probe for project membership.
    legacy.read_task(standing, state, task_id)
    raise problem(
        status.HTTP_501_NOT_IMPLEMENTED,
        "UNSUPPORTED",
        "V0 has no handover sign-off record; a handover ends in the run that owns it",
        details={"task_id": task_id, "created": False},
    )


__all__ = ["router"]
