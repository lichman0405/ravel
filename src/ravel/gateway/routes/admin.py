"""Runtime health, for the person who keeps the machine running.

`docs/08` gives the administrator "DSH health, Temporal health, runtime status,
backend status, project runtime diagnostics, session state, recovery tools/log
refs", and closes the section with the sentence that shapes this module: *Admin
does not make scientific decisions.* So everything here is a read, and the
interesting question is not what the routes return but what they refuse to
believe.

**Nothing here trusts a process to describe itself.** "Is Temporal up" is
answered by connecting to it, not by reading a configuration value and
reporting that a hostname is set. "Is a runtime live" is answered by the pool's
own snapshot of runtimes it is holding, not by counting rows that say a session
was started. The distinction is the whole value of the screen: a diagnostic that
reports what was configured rather than what is happening tells an operator
nothing they did not already know, and tells it to them most confidently when
the deployment is broken.

**Health is bounded, and a slow answer is an answer.** The Temporal probe is
capped by a timeout and reports `reachable: false` when it expires, because a
health endpoint that hangs is indistinguishable from a service that is down —
and worse, it takes the operator's terminal with it. Every field is either
already in memory or behind a timeout.

**An administrator may read a project's runtime and not its research.** The
project-scoped route below requires membership like every other project route
and additionally requires the ADMIN role. That is the `may_direct_project`
split: an administrator's authority is over the runtime, and it does not come
with the right to read a project they were never added to. An administrator who
needs to see one is granted a membership, which is a fact about them and is
recorded.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status

from ravel.domain.state_machines import JOB_TRANSITIONS
from ravel.gateway.deps import AdministratorDep, GatewayStateDep, PrincipalDep
from ravel.gateway.runtime import harness_home
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import BackendJobRepository

router = APIRouter(prefix="/projects", tags=["admin"])

#: How long the Gateway will wait for Temporal to answer. Short on purpose: this
#: is a person looking at a screen, and "did not answer within two seconds" is
#: the information they need. A longer wait would make the Gateway's health
#: endpoint fail exactly when the estate is degraded, which is when it is being
#: read.
PROBE_TIMEOUT_SECONDS = 2.0

#: The states a backend job can be in and still be somebody's problem: the ones
#: with somewhere left to go. Derived from the transition table rather than
#: listed here, because "has this job ended" is a question the domain already
#: answers — a terminal state is a state with no way out of it — and a second
#: hand-written list of "the unfinished ones" is a list that goes stale the
#: first time a state is added.
UNFINISHED = frozenset(
    state for state, onward in JOB_TRANSITIONS.items() if onward
)


async def _temporal_reachable(host: str, namespace: str) -> dict[str, Any]:
    """Whether a Temporal frontend answers at `host`, within the timeout.

    Failure is reported and not raised: an estate with a stopped Temporal is
    exactly the estate whose Gateway must keep answering, and a health route
    that returns 500 when the thing it reports on is down is a route that is
    only readable when it has nothing to say.
    """
    try:
        from temporalio.client import Client
    except ImportError as missing:  # pragma: no cover - the dependency is required
        return {"reachable": False, "detail": f"the Temporal client is unavailable: {missing}"}

    try:
        async with asyncio.timeout(PROBE_TIMEOUT_SECONDS):
            await Client.connect(host, namespace=namespace)
    except TimeoutError:
        return {
            "reachable": False,
            "detail": f"no answer within {PROBE_TIMEOUT_SECONDS}s",
        }
    except Exception as refused:
        # The client raises several types depending on how the connection
        # failed — refused, TLS, unknown namespace — and the operator's next
        # step is the same for all of them, which is to look at the server.
        return {"reachable": False, "detail": f"{type(refused).__name__}: {refused}"}
    return {"reachable": True, "detail": "connected"}


@router.get("/{project_id}/runtime", summary="This project's runtime, as the Gateway sees it")
def project_runtime(
    standing: AdministratorDep, state: GatewayStateDep
) -> dict[str, Any]:
    """Everything the Gateway knows about this project's live execution.

    Scoped to a project and gated on both membership and the ADMIN role,
    because the useful diagnostic is per-project: "which runtimes are live" is
    a global question whose answer is mostly other people's projects, and an
    administrator reading the estate is not the same as an administrator
    reading somebody's research.
    """
    pool = state.runtime.stats()
    live_scopes = (
        []
        if pool is None
        else [
            {"project_id": project_id, "role": role}
            for project_id, role in pool.scopes
            if project_id == standing.project_id
        ]
    )

    with state.database.read_only() as session:
        nodes = DagRepository(session, standing.project_id).nodes()
        jobs = BackendJobRepository(session, standing.project_id).all()

    return {
        "project_id": standing.project_id,
        "harness": {
            # `None` before anything has needed a runtime, which is a different
            # fact from a pool with nothing in it: the first says the Gateway
            # has not reached the harness at all, the second says it has and
            # every runtime has been reaped.
            "pool_started": pool is not None,
            "live_runtimes": None if pool is None else pool.live_runtimes,
            "live_sessions": None if pool is None else pool.live_sessions,
            "total_turns": None if pool is None else pool.total_turns,
            "scopes": live_scopes,
        },
        "nodes": {
            "total": len(nodes),
            "by_status": _counts(node.status.value for node in nodes),
        },
        "backend_jobs": {
            "total": len(jobs),
            "unfinished": sum(1 for job in jobs if job.state in UNFINISHED),
            "by_state": _counts(job.state.value for job in jobs),
        },
        # Where to look when the numbers above say something is wrong. Paths
        # rather than contents: the Gateway has no business reading a harness
        # log into a response, and the pinned harness already keeps its sessions
        # and profiles under this root.
        "logs": {
            "dsh_home": str(harness_home(state.settings)),
            "runtime_directory": str(state.settings.runtime_dir),
            "session_logs": str(harness_home(state.settings) / "sessions"),
        },
    }


@router.get("/{project_id}/runtime/jobs/{node_id}", summary="A node's backend attempts")
def node_jobs(
    standing: AdministratorDep,
    state: GatewayStateDep,
    node_id: str,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Every backend attempt this node has made, newest last.

    The retry history, which is the thing an operator needs and the DAG's node
    record does not keep: a node that has been submitted to a backend four times
    looks from the outside exactly like a node that is slow.

    Raises:
        HTTPException: 404 if this project has no such node.
    """
    with state.database.read_only() as session:
        try:
            DagRepository(session, standing.project_id).get(node_id=node_id)
        except NotFound as missing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no node {node_id!r} in this project",
            ) from missing
        jobs = BackendJobRepository(session, standing.project_id).for_node(node_id)
    return [job.model_dump(mode="json") for job in jobs[-limit:]]


@router.get("/{project_id}/runtime/temporal", summary="Whether Temporal answers")
async def temporal_health(
    standing: PrincipalDep, state: GatewayStateDep
) -> dict[str, Any]:
    """Reach the Temporal frontend and report what happened.

    Needs membership rather than the ADMIN role, and that is the same split as
    the rest of the Gateway read surface: whether the estate's queue is up is a
    fact about the deployment, and a project member waiting on a node that has
    not moved is one of the people most entitled to it. What it does not reveal
    is anything about another project — the probe names no project and reads no
    record.

    `standing` is unused beyond the dependency that runs it. It is named rather
    than omitted because the check *is* the dependency, and a route that
    dropped the parameter would be unauthenticated.
    """
    probe = await _temporal_reachable(
        state.settings.temporal_host, state.settings.temporal_namespace
    )
    return {
        "host": state.settings.temporal_host,
        "namespace": state.settings.temporal_namespace,
        "task_queue": state.settings.temporal_task_queue,
        **probe,
    }


@router.get("/{project_id}/runtime/harness", summary="The harness the Gateway is pinned to")
def harness_health(standing: PrincipalDep, state: GatewayStateDep) -> dict[str, Any]:
    """What the pin file says, and whether the home it names is there.

    The pin is read from `vendor/DSH_PIN.json` rather than repeated here, so
    that this screen and the test suite cannot disagree about which harness is
    running. `patches_required` is on the screen because it is the one field
    whose value being `true` means the deployment is not the one that was
    verified.
    """
    health: dict[str, Any] = state.runtime.health()
    return health["harness"]


def _counts(values: Iterable[str]) -> dict[str, int]:
    """How many of each value, with the keys sorted so two responses compare."""
    counted: dict[str, int] = {}
    for value in values:
        counted[value] = counted.get(value, 0) + 1
    return dict(sorted(counted.items()))

__all__ = ["PROBE_TIMEOUT_SECONDS", "UNFINISHED", "router"]
