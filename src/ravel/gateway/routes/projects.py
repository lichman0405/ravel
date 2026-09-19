"""Everything a member may read about their project.

This is the read surface, and it is the whole of what every role has in
common — an owner, a lab user and an administrator all see the same project
here, and what separates them is what they may *do*, which lives in `control`,
`lab` and `admin`.

**The DAG is served and cannot be written.** `docs/08` lists a read-only DAG
under the owner's view, and there is no route in this module — or anywhere in
this package — that adds, cancels or transitions a node. That is checked
mechanically rather than trusted: `tests/e2e/test_tui.py` reads the source of
`ravel.gateway` for an import of the DAG mutation service, and probes every
route with an owner's token to confirm the DAG is unchanged afterwards.

**Records are serialized, not re-modelled.** The responses carry the domain
records' own fields rather than a set of parallel response models, for the
reason `ravel.state.mapping` gives about rows: a second description of the
same thing is a second place for it to be wrong. `model_dump(mode="json")` is
what the outbox uses for the event stream, so a reader of the REST surface and
a reader of the stream see the same shapes.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ravel.domain.base import Record
from ravel.gateway.deps import CallerDep, GatewayStateDep, PrincipalDep
from ravel.master.audit import ProjectAudit
from ravel.state.outbox import events_since
from ravel.state.repositories.identity import memberships_of
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceRepository,
    EvidenceSourceRepository,
)

router = APIRouter(prefix="/projects", tags=["projects"])

#: How many events one read of the stream returns when the caller does not say.
#: A default rather than a limit: a caller may ask for more, up to `MAX_EVENTS`.
DEFAULT_EVENTS = 200
MAX_EVENTS = 2000


class ProjectSummary(BaseModel):
    """One project, as it appears in a list."""

    project_id: str
    display_id: str
    title: str
    objective: str
    status: str
    role: str


class NodeView(BaseModel):
    """One node of the DAG, as a reader sees it.

    A subset of `DagNode` rather than the whole record, and that is the one
    place this module narrows. A node carries its contract references, its
    join policy and its failure policy — all of which are how the node is
    *executed* — and a DAG view is for reading what the project is doing, not
    for re-deriving how it would be run. What is kept is what a person needs in
    order to follow the graph: what the node is for, who executes it, what it
    depends on, and where it has got to.
    """

    node_id: str
    display_id: str
    objective: str
    node_type: str
    status: str
    executor_role: str
    dependencies: list[str]
    roadmap_phase: str | None
    decision_ref: str | None
    created_at: str
    started_at: str | None
    completed_at: str | None


@router.get("", summary="The projects this caller is a member of")
def list_projects(caller: CallerDep, state: GatewayStateDep) -> list[ProjectSummary]:
    """Every project the caller may see, oldest first.

    Enumerated from the caller's own memberships, so a project they are not in
    is not merely filtered out of the answer — it is never fetched. There is no
    route that lists all projects, because none of the three V0 roles has a
    reason to see one they are not a member of.
    """
    with state.database.read_only() as session:
        held = memberships_of(session, caller.user_id)
        registry = ProjectRegistry(session)
        found = [(membership, registry.find(membership.project_id)) for membership in held]

    return [
        ProjectSummary(
            project_id=project.project_id,
            display_id=project.display_id,
            title=project.title,
            objective=project.objective,
            status=project.status.value,
            role=membership.role.value,
        )
        for membership, project in found
        if project is not None
    ]


@router.get("/{project_id}", summary="One project's authoritative state")
def project_projection(standing: PrincipalDep, state: GatewayStateDep) -> dict[str, Any]:
    """The projection the TUI opens on.

    Assembled by `ProjectAudit`, which is the same reader Master's recovery
    uses. That is the point: a person looking at a project and Master resuming
    one are looking at the same thing, so a session that died mid-turn cannot
    leave the two disagreeing about what had happened.
    """
    with state.database.read_only() as session:
        audit = ProjectAudit.assemble(session, standing.project_id)
    return {
        "project": audit.project.model_dump(mode="json"),
        "role": standing.role.value,
        "success_contract": _optional(audit.success_contract),
        "roadmap": [phase.model_dump(mode="json") for phase in audit.roadmap],
        "is_finished": audit.is_finished,
        "is_concludable": audit.is_concludable,
        "unfinished": list(audit.unfinished),
        "open_deviations": list(audit.open_deviations),
        "last_event_seq": audit.events[-1].seq if audit.events else 0,
    }


@router.get("/{project_id}/dag", summary="The Scientific DAG, read-only")
def read_dag(standing: PrincipalDep, state: GatewayStateDep) -> list[NodeView]:
    """Every node, in creation order.

    Read-only is the whole specification of this route. The Scientific DAG is
    mutated by Master alone, through tools whose authority comes from a
    server-side session binding; a user credential reaching a mutation here
    would be the thing `docs/09` forbids when it says an owner cannot
    direct-mutate the DAG.
    """
    with state.database.read_only() as session:
        audit = ProjectAudit.assemble(session, standing.project_id)
    return [_node_view(node) for node in audit.nodes]


@router.get("/{project_id}/decisions", summary="Master's decisions")
def read_decisions(standing: PrincipalDep, state: GatewayStateDep) -> list[dict[str, Any]]:
    """Every decision, in the order it was made.

    A decision is immutable and is what every DAG mutation cites, so this is
    the record of *why* the graph is the shape it is.
    """
    with state.database.read_only() as session:
        audit = ProjectAudit.assemble(session, standing.project_id)
    return [decision.model_dump(mode="json") for decision in audit.decisions]


@router.get("/{project_id}/reviews", summary="Review outcomes")
def read_reviews(standing: PrincipalDep, state: GatewayStateDep) -> list[dict[str, Any]]:
    """Every review, newest last.

    A review may accept, reject, or send work back, and it may recommend — but
    it cannot change a contract or a node. Reading one here does not change
    that; the route is a read, and the reviewer's authority is bounded where
    the record is written.
    """
    with state.database.read_only() as session:
        audit = ProjectAudit.assemble(session, standing.project_id)
    return [review.model_dump(mode="json") for review in audit.reviews]


@router.get("/{project_id}/evidence", summary="What the research found, and where")
def read_evidence(
    standing: PrincipalDep,
    state: GatewayStateDep,
    node_id: Annotated[
        str | None, Query(description="Only evidence recorded against this research task.")
    ] = None,
) -> list[dict[str, Any]]:
    """Evidence summaries, each with the sources it cites.

    The sources are joined in rather than left as identifiers because the one
    thing a reader must be able to do with a claim is check it, and a citation
    that cannot be followed is not a citation. `EvidenceSource` carries the URL
    that answered and the tier it was judged to be, which is what makes the
    claim checkable without a second request per reference.

    Filtering is by `research_task_ref`, which is the node a claim was recorded
    against. That is not the same as "evidence this node produced" — a research
    node gathers evidence for its own objective — because the model does not
    record that relationship, and inventing one here would be a second
    description of the same thing.
    """
    with state.database.read_only() as session:
        evidence = EvidenceRepository(session, standing.project_id).all()
        if node_id is not None:
            evidence = [item for item in evidence if item.research_task_ref == node_id]
        sources = _sources_for(session, standing.project_id, evidence)
    return [
        {
            "evidence": item.model_dump(mode="json"),
            "sources": [
                sources[reference].model_dump(mode="json")
                for reference in item.source_refs
                if reference in sources
            ],
        }
        for item in evidence
    ]


@router.get("/{project_id}/artifacts", summary="Artifacts registered against this project")
def read_artifacts(standing: PrincipalDep, state: GatewayStateDep) -> list[dict[str, Any]]:
    """Artifact metadata. The bytes are a separate request, authorized the same way."""
    with state.database.read_only() as session:
        artifacts = ArtifactRepository(session, standing.project_id).all()
    return [artifact.model_dump(mode="json") for artifact in artifacts]


@router.get("/{project_id}/executions", summary="Compute and experiment state")
def read_executions(standing: PrincipalDep, state: GatewayStateDep) -> list[dict[str, Any]]:
    """Every execution, which is where "is anything running?" is answered."""
    with state.database.read_only() as session:
        audit = ProjectAudit.assemble(session, standing.project_id)
    return [execution.model_dump(mode="json") for execution in audit.executions]


@router.get("/{project_id}/deviations", summary="What workers reported going wrong")
def read_deviations(standing: PrincipalDep, state: GatewayStateDep) -> list[dict[str, Any]]:
    """Every deviation, open ones included.

    A deviation is a worker saying that what it was asked to do is not what it
    could do. It is a record rather than an action: nothing here resolves one,
    because resolving it is a scientific judgement.
    """
    with state.database.read_only() as session:
        audit = ProjectAudit.assemble(session, standing.project_id)
    return [deviation.model_dump(mode="json") for deviation in audit.deviations]


@router.get("/{project_id}/events", summary="The project event stream, after a sequence")
def read_events(
    standing: PrincipalDep,
    state: GatewayStateDep,
    after_seq: Annotated[int, Query(ge=0, description="Return only events after this one.")] = 0,
    limit: Annotated[int, Query(ge=1, le=MAX_EVENTS)] = DEFAULT_EVENTS,
) -> dict[str, Any]:
    """Events after `after_seq`, oldest first.

    `after_seq` is what makes a reconnect cheap and correct: a client that
    recorded the last sequence it saw asks for what it missed, and the sequence
    being assigned by the database per project is what makes that a total
    order rather than a guess. The response reports whether there is more, so a
    client that is far behind knows to ask again rather than assume it is
    current.
    """
    with state.database.read_only() as session:
        found = events_since(session, standing.project_id, after_seq=after_seq, limit=limit + 1)

    more = len(found) > limit
    return {
        "events": [event.model_dump(mode="json") for event in found[:limit]],
        "more": more,
    }


def _node_view(node: Any) -> NodeView:
    """One node, narrowed to what a reader of the graph needs."""
    return NodeView(
        node_id=node.node_id,
        display_id=node.display_id,
        objective=node.objective,
        node_type=node.node_type.value,
        status=node.status.value,
        executor_role=node.executor_role.value,
        dependencies=list(node.dependencies),
        roadmap_phase=node.roadmap_phase,
        decision_ref=node.decision_ref,
        created_at=node.created_at.isoformat(),
        started_at=node.started_at.isoformat() if node.started_at else None,
        completed_at=node.completed_at.isoformat() if node.completed_at else None,
    )


def _sources_for(session: Session, project_id: str, evidence: list[Any]) -> dict[str, Any]:
    """The sources this evidence cites, by identifier.

    Fetched in one pass rather than per item, and only the ones cited: a
    project's source table grows with every search ever run, and a reader
    looking at one node's evidence has no use for the rest.
    """
    wanted = {reference for item in evidence for reference in item.source_refs}
    if not wanted:
        return {}
    repository = EvidenceSourceRepository(session, project_id)
    return {
        source.source_id: source
        for source in repository.all()
        if source.source_id in wanted
    }


def _optional(record: Record | None) -> dict[str, Any] | None:
    """A record as JSON, or `None` if the project has not got one yet."""
    return record.model_dump(mode="json") if record is not None else None


__all__ = ["ProjectSummary", "router"]
