"""What the person running the experiment needs, and the two things they can say.

`docs/08` lists the lab user's screen: the assigned task, the approved
procedure, the allowed ranges, the required deliverables, the status, a way to
report a problem, a way to upload, and the updated instruction they get back.
All but two of those are reads of records Master already wrote, and the whole
surface is arranged around one asymmetry.

**A lab user reads and reports; they do not decide.** Every route here needs
membership and nothing more, and the two writes are the two things a person
standing at a bench can contribute that nobody else can: the file they were
asked for, and the observation that reality did not match the plan. Reporting a
deviation *records* that the contract did not permit what was asked — it does
not grant the permission, does not stop the node, and does not decide that the
request was wrong. Master reads it on the next turn; the record is what makes
the question exist. Uploading a file *files* it against a named output — it does
not complete a run, does not satisfy a criterion, and does not decide that the
deliverable is acceptable. Whether the run is finished is decided by the backend
comparing what is recorded against what the handover owed, and whether the
delivery is *good* is Review's verdict against criteria frozen before the run.

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

**The package is served from the workspace the bench was handed, not
re-rendered.** Once a contract names a laboratory environment, preparation
builds a directory and hashes it into a manifest, and the handover names that
preparation. Serving the protocol by rendering the contract again would be a
second answer to "what was the bench given" — and the two would drift the first
time a materializer changed. So the documents route reads the file the manifest
lists, out of the directory the record names, and refuses any name the manifest
does not have. That last part is what makes it safe to build a path at all: the
caller's string is a key into the manifest and never a path component, so there
is no name a caller can send that reaches a file RAVEL did not write.

**An upload answers a name, and only a name the handover owed.** The directive
for this phase says an arbitrary file must not be able to complete a run, and
the shape of that rule here is that `output` is checked against the handover's
frozen `required_outputs` before a byte is stored. A file that answers nothing
is refused with the list of what is owed rather than filed under a name nobody
asked for — which is also why the door is per-output rather than a general
upload with a label attached. Everything the upload must record — the project,
the node, the contract version, who uploaded it, when, its media type and its
hash — is written by `record_upload`, which is the one place both this door and
any future one file bytes through.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from temporalio.exceptions import TemporalError

from ravel.domain.dag import DagNode
from ravel.domain.execution import DeviationRecord
from ravel.domain.lab import LabHandover
from ravel.domain.preparation import PreparationRecord
from ravel.domain.roles import AgentRole
from ravel.execution.temporal.contracts import ExternalResult
from ravel.gateway.deps import GatewayState, GatewayStateDep, PrincipalDep
from ravel.gateway.routes.artifacts import MAX_UPLOAD_BYTES, artifact_store
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.lab import (
    LabHandoverRepository,
    arrived_outputs,
    record_upload,
)
from ravel.state.repositories.preparations import PreparationRepository
from ravel.state.repositories.records import (
    BackendJobRepository,
    DeviationRepository,
    WorkerMessageRepository,
)
from ravel.state.store import ArtifactStoreError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/projects", tags=["lab"])

#: The role a task has to be executed by to appear on this screen. Named rather
#: than inlined because it is the whole definition of "a lab task": an
#: EXPERIMENT node is executed by the Experimental Worker, and `DagNode` refuses
#: to be built with any other executor for that type. That the two agree is
#: asserted in `tests/integration/gateway/test_lab.py` rather than here, so a
#: domain change fails a test that says what it broke instead of an import.
LAB_ROLE = AgentRole.EXPERIMENTAL_WORKER

#: What one package document may weigh when it is read back. The materializer
#: writes small text and JSON, so this is not a limit anybody should meet — it
#: is here because the route reads the file into memory to serve it, and a
#: prepared directory is a directory on a host that other things can write to.
#: Meeting it is answered with a refusal that names the size, not with a
#: truncated document that would look like a corrupt package.
MAX_DOCUMENT_BYTES = 8 * 1024 * 1024

#: What a document is served as, by its extension. A small closed table rather
#: than a guess: these are the files the laboratory materializer writes, and a
#: type the client has to sniff is a type a client will get wrong.
_MEDIA_TYPES = {
    ".md": "text/markdown; charset=utf-8",
    ".csv": "text/csv; charset=utf-8",
    ".json": "application/json",
    ".txt": "text/plain; charset=utf-8",
}

#: How a lab user says the plan and the bench disagree. Travelled in the view
#: rather than left to the client to know, because a screen that has to be told
#: out of band where to report a problem is a screen that reports it somewhere
#: else — and a report that lands somewhere else is not a deviation.
DEVIATION_MECHANISM: dict[str, str] = {
    "route": "POST /projects/{project_id}/lab/tasks/{task_id}/deviations",
    "body": (
        '{"requested_action": "<the action, named as the contract names it>", '
        '"description": "<what happened>"}'
    ),
    "effect": (
        "Records the observation against the contract version in force and "
        "tells the run, which stops and hands the question to Master. It "
        "permits nothing by itself."
    ),
}


class DeviationRequest(BaseModel):
    """What a lab user reports when the plan and the bench disagree."""

    #: The action that was refused, named as the contract names it, so Master
    #: can compare it against `allowed_actions` without guessing at synonyms.
    requested_action: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=10_000)


# ── Reading the task ────────────────────────────────────────────────────────


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


def _package(
    session: Session, project_id: str, handover: LabHandover | None
) -> PreparationRecord | None:
    """The prepared package this handover handed over, if it is still readable.

    Read by the identifier the handover carries rather than by searching for
    the newest preparation of the run: a handover names the package it gave
    somebody, and asking for "the latest" would answer with a package built
    later — after the bench had already started — which is precisely the
    confusion a handover's `preparation_id` exists to prevent.

    `None` when the handover is unknown or the preparation is not there, which
    a caller must render as "the package is not readable" rather than as an
    empty package: they are different facts, and only one of them is a fault.

    What comes back is always a *prepared* record and never a refusal: a
    handover is only ever made from a package that was built — the backend
    refuses to hand over anything else — so a refusal reached through a
    handover's own `preparation_id` is not a case that exists, and checking for
    it here would be a branch no test could honestly reach.
    """
    if handover is None or not handover.preparation_id:
        return None
    try:
        return PreparationRepository(session, project_id).get(
            preparation_id=handover.preparation_id
        )
    except NotFound:
        return None


def _documents(package: PreparationRecord | None) -> list[dict[str, Any]]:
    """What is in the package, as the manifest lists it.

    The manifest is the record of what was built and hashed, so it is the list
    — not a directory listing, which would also report whatever else had been
    put there, and not a table of names in this module, which would be a second
    account of what a materializer writes.
    """
    if package is None:
        return []
    listed = package.manifest.get("generated_files", [])
    if not isinstance(listed, list):
        return []
    return [dict(entry) for entry in listed if isinstance(entry, dict) and entry.get("name")]


def _handover_view(
    session: Session, project_id: str, handover: LabHandover
) -> dict[str, Any]:
    """The package a bench was given, what it has sent back, and what it still owes.

    Everything a lab user needs that is not the contract: the prepared
    documents they work from, the version of the package and the manifest that
    produced it, which outputs have been recorded and which are outstanding,
    and the identifier of each artifact that answered one. Names and
    identifiers rather than versions and hashes: *which bytes* answered an
    output is a fact about artifact versions, the artifact routes are where
    versions live, and repeating them here would be a second listing to keep
    in step with the first.
    """
    package = _package(session, project_id, handover)
    answered = arrived_outputs(session, handover)
    names = tuple(name for name, _artifact in answered)
    return {
        "handover": handover.model_dump(mode="json"),
        "package": None
        if package is None
        else {
            "preparation_id": package.preparation_id,
            "materializer": package.materializer,
            "materializer_version": package.materializer_version,
            #: Where the directory is, on the host that built it. Handed out
            #: because a bench on the same machine reads these files directly,
            #: and because a package that cannot be served is diagnosed from
            #: knowing which directory was not there.
            "workspace_path": package.workspace_path,
            "manifest": package.manifest,
            "documents": _documents(package),
        },
        "answered_outputs": list(names),
        "artifacts": {
            name: artifact.artifact_id for name, artifact in answered
        },
        "missing_outputs": list(handover.owed(names)),
    }


def _task(session: Session, project_id: str, node: DagNode) -> dict[str, Any]:
    """One row of the lab screen.

    Everything a reader needs about one task, gathered over a session the caller
    opened, so that listing twenty tasks is one connection rather than twenty.

    `handover` is `None` until work is actually handed to a bench. That is a
    different fact from a task with no contract, and a different fact again
    from a handover that has ended — the three states a lab user can be looking
    at, and each of them answered by what is here rather than by an empty
    object.
    """
    latest = BackendJobRepository(session, project_id).latest_for_node(node.node_id)
    handover = LabHandoverRepository(session, project_id).latest_for_node(node.node_id)
    return {
        "node": node.model_dump(mode="json"),
        "instruction": _instruction(session, project_id, node),
        "backend_job": None if latest is None else latest.model_dump(mode="json"),
        "handover": None
        if handover is None
        else _handover_view(session, project_id, handover),
        "deviation_mechanism": DEVIATION_MECHANISM,
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
    """The node, its contract, its package, its job, what it said, and what was reported."""
    with state.database.read_only() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        return _task(session, standing.project_id, node)


@router.get(
    "/{project_id}/lab/tasks/{task_id}/handover/documents/{document}",
    response_class=Response,
    summary="One file out of the package the bench was given",
)
def read_document(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    document: str,
) -> Response:
    """The protocol, the sample manifest, the reagent list, the measurement plan.

    Read from the prepared directory, under the name the manifest recorded, so
    that what a lab user reads here is the bytes RAVEL wrote rather than a
    document assembled at request time out of what the contract still says.

    Readable after the handover has ended as well as while it is open, because
    the protocol an experiment was performed under is history: the bench that
    worked from it, and whoever reads the result afterwards, need the same
    document rather than the one the contract would produce now.

    Raises:
        HTTPException: 404 if this project has no such task, if nothing was
            handed to a bench for it, or if the package has no document by that
            name — the last naming what the package does have. 410 if the
            directory the package was built in is no longer there, which is a
            workspace that was removed rather than a package that never
            existed. 413 if the file is larger than this route will read into
            memory.
    """
    with state.database.read_only() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        handover = LabHandoverRepository(
            session, standing.project_id
        ).latest_for_node(node.node_id)
        package = _package(session, standing.project_id, handover)
        listed = _documents(package)
        entry = next((item for item in listed if item["name"] == document), None)

    if package is None or entry is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"this task has no package document {document!r}; the package "
                f"holds {sorted(item['name'] for item in listed)}"
                if listed
                else "nothing was handed to a bench for this task, so there is "
                "no package to read a document out of"
            ),
        )
    # Built from the *manifest's* name, never from what the caller sent: the
    # caller's string was used as a key above and reaches no path component, so
    # no name can be sent that escapes the directory the manifest describes.
    path = Path(package.workspace_path) / str(entry["name"])
    try:
        body = path.read_bytes()
    except FileNotFoundError as gone:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=(
                f"the package directory {package.workspace_path!r} is no longer "
                "there, so this document cannot be read; what survives is the "
                "manifest, which records the file's name and hash"
            ),
        ) from gone
    except OSError as refused:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"the package document could not be read: {refused.strerror}",
        ) from refused

    if len(body) > MAX_DOCUMENT_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"{document!r} is {len(body)} bytes and this route reads at most "
                f"{MAX_DOCUMENT_BYTES}"
            ),
        )
    return Response(
        content=body,
        media_type=_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream"),
        headers={
            "X-RAVEL-Content-Hash": f"sha256:{entry.get('sha256', '')}",
            "Content-Disposition": f'inline; filename="{path.name}"',
        },
    )


# ── The two writes ──────────────────────────────────────────────────────────


@router.post(
    "/{project_id}/lab/tasks/{task_id}/uploads",
    status_code=status.HTTP_201_CREATED,
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
            description="The required output this file answers, named as the contract names it.",
        ),
    ],
    filename: Annotated[str, Query(max_length=255)] = "",
    media_type: Annotated[str, Query(max_length=200)] = "",
) -> dict[str, Any]:
    """File the body as the artifact that answers one output, and tell the run.

    **A file that answers nothing is refused.** `output` has to be one of the
    outputs this handover owed, checked before a byte is stored, so there is no
    way to upload something the contract did not ask for and no way for one to
    count towards a delivery. That is the mechanism, not a policy: the delivery
    the run completes on is computed from the *recorded* names compared against
    the handover's frozen list, so an arbitrary file could not complete a run
    even if it were accepted.

    Any member may upload, which is the point of this door: the person who ran
    the experiment is the person holding the file, and requiring the owner to
    relay it would mean the owner uploading files they have never seen. The
    record says who uploaded it — `created_by` on the version — so the route
    does not need to.

    Uploading does not complete anything. What it does is make the file a fact
    and tell the run, which then decides whether it has everything; and it is
    idempotent in the bytes, because the same file sent twice is one version
    with one hash rather than two versions of one output.

    Raises:
        HTTPException: 404 if this project has no such task. 409 if nothing is
            in anybody's hands for it, or if the package owes no such output —
            either way the message says what is owed instead. 413 if the body
            is over the cap. 502 if the store refused the bytes. 503 if the
            deployment has no store at all.
    """
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {MAX_UPLOAD_BYTES} bytes",
        )
    body = await request.body()
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {MAX_UPLOAD_BYTES} bytes",
        )

    store = artifact_store(state)
    try:
        with state.database.transaction() as session:
            node = _experiment_node(session, standing.project_id, task_id)
            handover = _owed_handover(session, standing.project_id, node)
            if not handover.answers(output):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"this handover owes no output called {output!r}; it owes "
                        f"{list(handover.required_outputs)}. An upload answers a "
                        "name the contract required, and a file that answers "
                        "nothing is not part of a delivery."
                    ),
                )
            artifact, version = record_upload(
                session,
                store,
                handover,
                output=output,
                chunks=[body],
                uploaded_by=standing.user_id,
                filename=filename,
                media_type=media_type or _media_type(filename),
            )
            answered = arrived_outputs(session, handover)
    except ArtifactStoreError as refused:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"the artifact store refused the upload: {refused}",
        ) from refused

    names = tuple(name for name, _artifact in answered)
    delivered, note = await _tell_the_run(
        state,
        handover,
        ExternalResult(
            summary=f"{standing.username} uploaded {output!r} against this handover",
            # Carried for the history and not for the decision: the backend
            # recomputes what arrived from the recorded artifacts, and a
            # delivery that claimed to bring everything could not complete a
            # run even if one did.
            delivered_outputs=(output,),
            payload={"handover_id": handover.handover_id},
        ),
    )
    return {
        "artifact": artifact.model_dump(mode="json"),
        "version": version.model_dump(mode="json"),
        "output": output,
        "answered_outputs": list(names),
        "missing_outputs": list(handover.owed(names)),
        "delivered_to_run": delivered,
        "delivery_note": note,
    }


@router.post(
    "/{project_id}/lab/tasks/{task_id}/deviations",
    status_code=status.HTTP_201_CREATED,
    summary="Report that the plan and the bench disagree",
)
async def report_deviation(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: DeviationRequest,
) -> dict[str, Any]:
    """Record something the contract did not permit, and tell the run.

    The record is written against the node's frozen contract, and it is written
    whether or not the requested action later turns out to be permitted — a
    fact this route deliberately does not decide. A lab user saying "I was asked
    for something the contract does not allow" is reporting an observation, and
    the observation is worth having even when Master then rules that the action
    was in fact permitted. `permitted` stays false because the *reporter* did
    not permit it, which is what makes this a report rather than an execution;
    what Master decides is written as a decision, and resolving the deviation
    points at it.

    **The report then goes to the run.** A run waiting on a bench is blocked in
    a durable wait and is never polled, so nothing RAVEL learns by looking will
    ever reach it — the report travels on the same signal a delivery does, and
    the Worker adjudicates it against the frozen contract it holds. What the
    Worker does with it is not this route's decision either: a report the
    contract turns out to permit leaves the run working, and one it does not
    stops it and hands the question to Master.

    A report that the run could not be told about is still recorded, and the
    response says so. That is the deliberate ordering: the record is what makes
    the question exist, and Master reads open deviations on the next turn
    whatever the scheduler is doing.

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
        waiting = LabHandoverRepository(session, standing.project_id).waiting_for(task_id)

    delivered, note = (False, _NOBODY_WAITING)
    if waiting is not None:
        delivered, note = await _tell_the_run(
            state,
            waiting,
            ExternalResult(
                summary=(
                    f"{standing.username} reported that the contract did not "
                    f"permit {request.requested_action!r}"
                ),
                detail=request.description,
                # The identifier, not the words: the record is already written
                # and attributed to the person who wrote it, and the Worker
                # stops the run *against that record* rather than raising a
                # second one for the same sentence.
                payload={"deviation_id": deviation.deviation_id},
            ),
        )

    return {
        **deviation.model_dump(mode="json"),
        "delivered_to_run": delivered,
        "delivery_note": note,
    }


# ── Reaching the run ────────────────────────────────────────────────────────


#: What the response says when there is no run to tell. Not a failure: a report
#: against a task that is not in anybody's hands is still a report, and Master
#: reads it on the next turn like any other.
_NOBODY_WAITING = (
    "no run of this task is waiting on anything, so there was nothing to tell; "
    "the record stands and Master reads it on the next turn"
)


def _owed_handover(session: Session, project_id: str, node: DagNode) -> LabHandover:
    """The handover this node is in somebody's hands under, or a refusal.

    Three refusals rather than one, because they are three different situations
    for the person reading them: nothing was ever prepared for this task,
    something was handed over and has ended, or there is a node here at all.
    Each is answered with what is true rather than with "conflict".

    Raises:
        HTTPException: 409.
    """
    repository = LabHandoverRepository(session, project_id)
    waiting = repository.waiting_for(node.node_id)
    if waiting is not None:
        return waiting
    latest = repository.latest_for_node(node.node_id)
    if latest is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"task {node.display_id} has not been handed to a bench, so "
                "there is nothing for an upload to answer; a handover exists "
                "once a run has prepared a package and given it to somebody"
            ),
        )
    raise HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=(
            f"the handover for task {node.display_id} ended as "
            f"{latest.state.value} and cannot be answered; what was already "
            "recorded against it stands, and a new run is a new handover"
        ),
    )


async def _tell_the_run(
    state: GatewayState, handover: LabHandover, result: ExternalResult
) -> tuple[bool, str]:
    """Send the signal that ends a waiting run's wait, or say that it did not go.

    Returns:
        Whether the run was told, and a sentence to show the caller when it was
        not. **A failure to deliver is not raised**, and that is the considered
        answer rather than a swallowed error: everything this route records has
        already been recorded by the time it is called, and by then the
        question "did my file arrive" is answered by the artifact — so a 5xx
        would tell a lab user their upload failed when it did not. What the
        caller is told instead is exactly what is true: it is on the record,
        and the run has not been told.

        The distinction matters most in the case that is not rare: a run whose
        worker died is a workflow that is no longer there to signal, and the
        delivery is not the thing that was wrong.
    """
    try:
        await state.deliver_external.deliver(
            node_id=handover.node_id,
            execution_contract_version=handover.execution_contract_version,
            result=result,
        )
    except (TemporalError, OSError) as unreachable:
        # Logged in full on the server, where a hostname and a status code are
        # for the operator; reported as a kind to the caller, who needs to know
        # that the run did not hear and not which socket refused it.
        logger.warning(
            "could not deliver to node %s under contract version %s: %s",
            handover.node_id,
            handover.execution_contract_version,
            unreachable,
        )
        return False, (
            "the run was not told: the workflow service could not be reached or "
            "refused the delivery. Nothing recorded here is lost, and re-sending "
            "the same upload is safe — the same bytes file once — but a wait that "
            "is never answered ends by timing out rather than by delivering."
        )
    return True, ""


def _media_type(filename: str) -> str:
    """What a file is, from its name, when the uploader did not say.

    An empty string when the name says nothing, which is the honest answer and
    not a missing one: `application/octet-stream` would be RAVEL asserting that
    a file is opaque bytes, and RAVEL does not sniff. A lab file is whatever the
    person who wrote it says it is, and a name with an extension this table does
    not hold is a name that told us nothing — recorded as nothing rather than
    guessed at.
    """
    return _MEDIA_TYPES.get(Path(filename).suffix.lower(), "")


__all__ = [
    "DEVIATION_MECHANISM",
    "LAB_ROLE",
    "MAX_DOCUMENT_BYTES",
    "DeviationRequest",
    "router",
]
