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

**One panel is an exception, and it is an exception because there is no
second source.** `service_health` reports what a long-running process last said
about itself, and nothing else can: a supervisor that died leaves a project
that has stopped moving, which is what a project with nothing left to do looks
like too. What is not taken on trust there is the *judgement* — the age is
computed from this process's clock against a cadence the service promised — and
the panel says `reporting: false` rather than "down" for a service that has
never reported, because a deployment that never started one has not lost one.

**What this module will not do is reach a cluster.** The Slurm settings reach
the backend process and nothing else, so `backend_health` reports *whether* a
credential is configured — by naming the setting, never by reading the value —
and says in as many words that reachability is not probed from here. A Gateway
that opened an SSH session to answer a health question would be a Gateway
holding the cluster password, which is the arrangement the whole `slurm_` rule
exists to prevent.

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

from ravel.config import Settings
from ravel.domain.execution import BackendJob
from ravel.domain.services import (
    SERVICE_NAMES,
    ServiceReport,
    heartbeat_is_stale,
)
from ravel.domain.state_machines import JOB_TRANSITIONS
from ravel.gateway.deps import AdministratorDep, GatewayStateDep, PrincipalDep
from ravel.gateway.runtime import harness_home
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.reconciliation import RunReconciliationRepository
from ravel.state.repositories.records import BackendJobRepository
from ravel.state.repositories.services import ServiceRepository

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

#: How many beats a service may miss before this screen calls it stale.
#:
#: Three, so that one late tick is not a fault — a supervisor held up by a slow
#: reconciliation sweep is alive and a screen that said otherwise would train
#: its reader to ignore it. Multiplied by the cadence the service reports.
MISSED_BEATS = 3

#: The floor under that, for a service that reported without saying how often
#: it beats. Deliberately short: the conservative reading of an unknown cadence
#: is the one that reports a silence soonest, and a false "stale" sends an
#: operator to look at a process — which is a cheap mistake — where a false
#: "alive" leaves a dead one running.
MINIMUM_SILENCE_SECONDS = 30.0

#: How many recoveries the reconciliation panel shows. The count is the fact;
#: this is a page, and an operator who needs the twenty-first oldest recovery
#: is reading a record rather than a screen.
RECONCILIATIONS_SHOWN = 20


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


@router.get(
    "/{project_id}/runtime/services",
    summary="Which long-running processes are still reporting",
)
def service_health(standing: PrincipalDep, state: GatewayStateDep) -> dict[str, Any]:
    """Every named service, whether it has reported, and how long ago.

    **This is the one panel that reads a process's account of itself**, and it
    is the exception to this module's rule for a reason: there is nothing else
    to read. Whether the supervisor is running is not derivable from any
    record — a project it stopped driving and a project with nothing left to
    do are the same rows — so the alternative to trusting the report is not
    verifying it, it is having no answer. What is *not* taken on trust is the
    service's word for its own health: the age is computed here, from a clock
    this process owns, against a cadence the service promised.

    The cadence comes out of the report's own `detail`, so a supervisor that
    polls slowly is not reported as dead for polling at the rate it was
    configured to. A service that reported without one gets the floor, which is
    the conservative reading: the shorter the budget, the sooner a silence is
    called a fault.

    **The detail is filtered to this project.** A supervisor's report names
    every project it holds, which is other people's research, and the standing
    check that admits a caller here is a membership in *this* project. What a
    caller learns about the others is that they exist — `projects_held` is a
    count — which is the same shape `project_runtime` already uses for the
    runtime pool's scopes.

    Needs membership rather than the ADMIN role, like the Temporal probe and
    for the same reason: "the supervisor is down" is a fact about the
    deployment, and a member waiting on a node that has not moved is one of the
    people most entitled to it.
    """
    with state.database.read_only() as session:
        reports = ServiceRepository(session).named(SERVICE_NAMES)

    services: list[dict[str, Any]] = []
    for name in SERVICE_NAMES:
        report = reports.get(name)
        budget = _silence_budget(report)
        held = set() if report is None else _held_projects(report)
        services.append(
            {
                "service": name,
                "reporting": report is not None,
                "stale": heartbeat_is_stale(report, budget_seconds=budget),
                "silence_budget_seconds": budget,
                "instance": None if report is None else report.instance,
                "started_at": None if report is None else report.started_at.isoformat(),
                "heartbeat_at": None if report is None else report.heartbeat_at.isoformat(),
                "silent_for_seconds": None if report is None else round(report.age_seconds(), 3),
                "holds_this_project": standing.project_id in held,
                "projects_held": len(held),
            }
        )
    return {"project_id": standing.project_id, "services": services}


@router.get(
    "/{project_id}/runtime/backends",
    summary="What this deployment can run work on, and the cluster it was given",
)
def backend_health(standing: AdministratorDep, state: GatewayStateDep) -> dict[str, Any]:
    """Which backends have actually run this project's work, and the Slurm setup.

    **Which backends are *registered* is not answerable from here, and this
    route does not pretend otherwise.** The registry is built by the Temporal
    worker at start-up from its own launch arguments — `--compute-backend`,
    `--lab-backend` — and that is a different process, on a different command
    line, which the Gateway has no way to ask. So the first half of this answer
    is the backends this project's jobs have *actually been handed to*, read
    from `backend_jobs`, which is a fact in the authoritative state rather than
    a reading of somebody's configuration.

    The second half is the cluster. `slurm_` settings are the one group in
    `Settings` with a rule attached: the credential reaches the backend process
    and nothing else, so this route reports **whether** a secret is configured
    by naming the setting that holds it and never reads its value. What it also
    does not do is test reachability, and that is the same rule rather than a
    gap: opening an SSH session from here would mean this process holding the
    password, which is exactly what `_LAUNCHER_ONLY` exists to prevent. An
    operator who wants to know whether the cluster answers runs a job on it,
    and what that job says is in the jobs route below.
    """
    with state.database.read_only() as session:
        jobs = BackendJobRepository(session, standing.project_id).all()
        # Read once for the page rather than per job: a backend's node types are
        # what it was handed work for, and asking the DAG per job would be one
        # query per row to answer the same question about the same node twice.
        kinds = {
            node.node_id: node.node_type.value
            for node in DagRepository(session, standing.project_id).nodes()
        }

    return {
        "project_id": standing.project_id,
        "used": [
            {
                "backend": backend,
                "jobs": len(named_jobs),
                "states": _counts(job.state.value for job in named_jobs),
                "node_types": sorted(
                    {kinds[job.node_id] for job in named_jobs if job.node_id in kinds}
                ),
            }
            for backend, named_jobs in _by_backend(jobs).items()
        ],
        "slurm": _slurm_configuration(state.settings),
    }


def _slurm_configuration(settings: Settings) -> dict[str, Any]:
    """What the deployment was told about the cluster, minus the secret.

    Every field here is either not a secret or a *name* rather than a value.
    `authentication` says which method is configured, because "the key is
    missing" and "the password is missing" are different repairs, and it says
    it by naming the setting — `RAVEL_SLURM_KEY_FILENAME`, the file's *path* —
    rather than by quoting anything out of `slurm_password`.
    """
    host = settings.slurm_host
    if settings.slurm_key_filename is not None:
        authentication = f"private key, {settings.slurm_key_filename}"
    elif settings.slurm_password is not None:
        authentication = "password (RAVEL_SLURM_PASSWORD)"
    else:
        authentication = "none configured"
    return {
        "configured": bool(host),
        "host": host,
        "port": settings.slurm_port,
        "username": settings.slurm_username,
        "authentication": authentication,
        "jobs_root": settings.slurm_jobs_root,
        # False is the safe answer and the default: an unknown host key is
        # refused. Reported because a deployment that turned it on is a
        # deployment whose transport is encrypted to whoever answered the
        # address, which an operator should be able to see without reading a
        # config file on another host.
        "trust_unknown_host": settings.slurm_trust_unknown_host,
        "connect_timeout_seconds": settings.slurm_connect_timeout_seconds,
        "command_timeout_seconds": settings.slurm_command_timeout_seconds,
        "reachability": "not probed from the Gateway",
        "why": (
            "the credential reaches the backend process and nothing else, so "
            "whether the cluster answers is reported by the run that was sent "
            "there rather than tested from here"
        ),
    }


@router.get(
    "/{project_id}/runtime/jobs",
    summary="The backend jobs this project has, running and failed",
)
def project_jobs(
    standing: AdministratorDep,
    state: GatewayStateDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> dict[str, Any]:
    """Every job, newest first, split by whether it is still somebody's problem.

    Ordered by `updated_at` rather than by submission, because the question
    this answers is "what is happening", and a job submitted an hour ago and
    polled a second ago is happening now. `open` and `ended` are split by the
    domain's transition table rather than by a list written here: a job that
    has a state to move to is unfinished, and a state added to the machine
    would otherwise silently join neither half.

    The node's `display_id` travels with each job — read once for the page
    rather than per row — because a job names a node by identifier and an
    operator reading a failure wants the name Master gave it.
    """
    with state.database.read_only() as session:
        jobs = BackendJobRepository(session, standing.project_id).all()
        named = {
            node.node_id: node.display_id
            for node in DagRepository(session, standing.project_id).nodes()
        }

    ordered = sorted(jobs, key=lambda job: job.updated_at, reverse=True)[:limit]
    return {
        "project_id": standing.project_id,
        "total": len(jobs),
        "open": [
            _job_view(job, named) for job in ordered if job.state in UNFINISHED
        ],
        "ended": [
            _job_view(job, named) for job in ordered if job.state not in UNFINISHED
        ],
        "failed_by_class": _counts(
            job.failure_class.value for job in jobs if job.failure_class is not None
        ),
    }


def _job_view(job: BackendJob, named: dict[str, str]) -> dict[str, Any]:
    """One job as a row of the screen: which node, which backend, how it went."""
    return {
        "job_id": job.job_id,
        "node_id": job.node_id,
        "display_id": named.get(job.node_id, ""),
        "backend": job.backend,
        "attempt": job.attempt,
        "state": job.state.value,
        "backend_state": job.backend_state,
        "failure_class": None if job.failure_class is None else job.failure_class.value,
        "detail": job.detail,
        "submitted_at": job.submitted_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "ended_at": None if job.ended_at is None else job.ended_at.isoformat(),
    }


@router.get(
    "/{project_id}/runtime/reconciliations",
    summary="Runs RAVEL found dead, and what it did about them",
)
def project_reconciliations(
    standing: AdministratorDep, state: GatewayStateDep
) -> dict[str, Any]:
    """Every recovery this project has needed, newest first.

    The record an operator looks for after an outage: a node that was RUNNING
    when the worker died was put back in play by a reconciliation, and this is
    where that is written down. Absent records are the healthy answer and the
    route says so with a count rather than an empty list, because "RAVEL has
    never had to recover a run here" is a fact worth being able to read.

    `observed` is on each row and not only in the detail: `UNKNOWN` — a probe
    that could not reach Temporal — is the difference between a run that was
    found dead and a run RAVEL could not ask about, and a screen that showed
    only the recovery would report the second as the first.
    """
    with state.database.read_only() as session:
        records = RunReconciliationRepository(session, standing.project_id).all()
    ordered = sorted(records, key=lambda record: record.created_at, reverse=True)
    return {
        "project_id": standing.project_id,
        "total": len(records),
        "by_class": _counts(record.failure_class.value for record in records),
        "by_observation": _counts(record.observed.value for record in records),
        "recent": [
            {
                "reconciliation_id": record.reconciliation_id,
                "node_id": record.node_id,
                "execution_contract_version": record.execution_contract_version,
                "workflow_id": record.workflow_id,
                "observed": record.observed.value,
                "failure_class": record.failure_class.value,
                "node_status_before": record.node_status_before.value,
                "node_status_after": record.node_status_after.value,
                "detail": record.detail,
                "detected_by": record.detected_by,
                "created_at": record.created_at.isoformat(),
            }
            for record in ordered[:RECONCILIATIONS_SHOWN]
        ],
    }


def _held_projects(report: ServiceReport) -> set[str]:
    """The projects a service's report says it is holding, defensively.

    Read out of JSONB, which is to say out of whatever was stored: a report
    written by an older build, or by a process whose detail has a different
    shape, must not take the route down. Anything that is not a list of strings
    is read as "holding nothing", which under-reports rather than raising.
    """
    held = report.detail.get("projects")
    if not isinstance(held, list):
        return set()
    return {str(project) for project in held}


def _silence_budget(report: ServiceReport | None) -> float:
    """How long a service may go quiet before it is called stale.

    Three beats, so that one missed tick is not a fault — a supervisor that is
    half a second late because a reconciliation sweep was slow must not be
    reported as dead — and the floor is for a service whose report does not say
    how often it beats. The report's own `poll_seconds` is what makes this a
    judgement about *that* service rather than a constant that has to be right
    for all of them.
    """
    if report is None:
        return MINIMUM_SILENCE_SECONDS
    poll = report.detail.get("poll_seconds")
    if not isinstance(poll, int | float) or poll <= 0:
        return MINIMUM_SILENCE_SECONDS
    return max(MINIMUM_SILENCE_SECONDS, MISSED_BEATS * float(poll))


def _by_backend(jobs: list[BackendJob]) -> dict[str, list[BackendJob]]:
    """The jobs grouped by the backend that held them, by name."""
    grouped: dict[str, list[BackendJob]] = {}
    for job in jobs:
        grouped.setdefault(job.backend, []).append(job)
    return dict(sorted(grouped.items()))


def _counts(values: Iterable[str]) -> dict[str, int]:
    """How many of each value, with the keys sorted so two responses compare."""
    counted: dict[str, int] = {}
    for value in values:
        counted[value] = counted.get(value, 0) + 1
    return dict(sorted(counted.items()))


__all__ = [
    "MINIMUM_SILENCE_SECONDS",
    "MISSED_BEATS",
    "PROBE_TIMEOUT_SECONDS",
    "RECONCILIATIONS_SHOWN",
    "UNFINISHED",
    "router",
]
