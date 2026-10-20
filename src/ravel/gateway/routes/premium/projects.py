"""Projects, their consistent snapshot, and their read-only DAG.

The list and the DAG are paginated projections of the legacy reads, so the
records and their fields are the same ones the TUI has always drawn. The
snapshot is the new shape: an aggregate plus `anchor_seq`, read in one
`REPEATABLE READ` transaction so a client that replays events from that anchor
is replaying into a view the anchor actually describes.

**There is no DAG write here, and no route in this package that reaches one.**
The DAG is Master's, mutated through tools whose authority is a server-side
session binding. A user's token can read the graph and cannot add, cancel or
transition a node; `tests/premium/T13` asserts that against the generated
OpenAPI schema and against this package's source, as well as by reading the DAG
back after probing the routes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query, status

from ravel.gateway.deps import CallerDep, GatewayStateDep, PrincipalDep
from ravel.gateway.routes import members as legacy_members
from ravel.gateway.routes import projects as legacy_projects
from ravel.gateway.routes.members import NewProject
from ravel.gateway.routes.premium.support import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    Page,
    paginate,
    read_snapshot,
)
from ravel.gateway.routes.projects import NodeView, ProjectSummary

router = APIRouter(tags=["v1-projects"])


@router.get(
    "/projects",
    response_model=Page[ProjectSummary],
    summary="Projects this caller may open",
)
def list_projects(
    caller: CallerDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
) -> Page[ProjectSummary]:
    """Every project the caller is a member of, ordered by identifier.

    The ordering is by identifier rather than by the membership's grant time
    because the cursor has to name a key that cannot move: a grant time is
    stable too, but it is not in the legacy projection, and adding a field to
    that projection to make this page deterministic would change the shape the
    TUI reads. Project identifiers are opaque and unique, which is enough for a
    stable keyset.
    """
    found = legacy_projects.list_projects(caller, state)
    return paginate(
        found,
        key=lambda project: (project.project_id,),
        limit=limit,
        cursor=cursor,
    )


@router.post(
    "/projects",
    status_code=status.HTTP_201_CREATED,
    response_model=dict[str, str],
    summary="Open a project, owned by whoever opened it",
)
def create_project(
    caller: CallerDep, state: GatewayStateDep, request: NewProject
) -> dict[str, str]:
    """Create the project and its first membership, and nothing else.

    No DAG node is written. Opening a project gives Master an objective to plan
    against; the planning is Master's next turn, through the same tools a
    conversation reaches. A route that seeded a node here would be a user
    credential writing the Scientific DAG.
    """
    return legacy_members.create_project(caller, state, request)


@router.get(
    "/projects/{project_id}/snapshot",
    response_model=dict[str, Any],
    summary="The aggregate state and the event anchor it corresponds to",
)
def snapshot(standing: PrincipalDep, state: GatewayStateDep) -> dict[str, Any]:
    """The whole project view, with `anchor_seq` read from the same transaction.

    `anchor_seq` is the last event sequence the aggregate reflects. A client
    stores it and reconnects the event stream with `after_seq=anchor_seq`, so
    every event that arrived after the snapshot is delivered exactly once and
    no event already reflected in the snapshot is replayed into it.
    """
    return read_snapshot(standing, state)


@router.get(
    "/projects/{project_id}/dag",
    response_model=Page[NodeView],
    summary="The Scientific DAG, read-only",
)
def read_dag(
    standing: PrincipalDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=MAX_LIMIT)] = DEFAULT_LIMIT,
    cursor: Annotated[
        str | None, Query(description="The cursor returned by the previous page.")
    ] = None,
) -> Page[NodeView]:
    """The graph, in creation order. There is no method on this path but GET."""
    nodes = legacy_projects.read_dag(standing, state)
    return paginate(
        nodes,
        key=lambda node: (node.created_at, node.node_id),
        limit=limit,
        cursor=cursor,
    )


__all__ = ["router"]
