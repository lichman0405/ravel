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

import hashlib
import logging
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, status
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session
from temporalio.exceptions import TemporalError

from ravel.domain.artifacts import unusable_filename_reason
from ravel.domain.dag import DagNode
from ravel.domain.enums import JobState, LabEventType, LabUploadOutcome, UserRole
from ravel.domain.execution import DeviationRecord
from ravel.domain.lab import (
    LabEvent,
    LabHandover,
    LabLeadConfirmation,
    LabUploadReceipt,
)
from ravel.domain.preparation import PreparationRecord
from ravel.domain.roles import AgentRole
from ravel.execution.temporal.contracts import ExternalResult
from ravel.gateway.deps import (
    GatewayState,
    GatewayStateDep,
    Principal,
    PrincipalDep,
)
from ravel.gateway.routes.artifacts import (
    CHUNK_BYTES,
    UploadTooLarge,
    artifact_store,
)
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.contracts import ExecutionContractRepository
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.lab import (
    LabEventRepository,
    LabHandoverRepository,
    LabLeadConfirmationRepository,
    LabPackageRepository,
    LabUploadReceiptRepository,
    arrived_outputs,
    confirmation_for,
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


def _file_chunks(path: Path) -> Iterator[bytes]:
    """Read a spooled upload in bounded slices, so the store never sees it whole."""
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            yield chunk

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
    #: Which run the report is about. Omitted means the handover currently in
    #: somebody's hands, or the newest one if nothing is waiting.
    protocol_version: int | None = Field(default=None, ge=1)
    #: The sample the observation is about, labelled as the package labelled
    #: it. Checked against the package binding when one exists.
    sample_id: str | None = Field(default=None, max_length=128)
    #: The file the observation is about, by the artifact identifier the read
    #: routes return, and its SHA-256 as the person computed or was given it.
    raw_output_ref: str | None = Field(default=None, max_length=1024)
    raw_output_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )


class HumanReportRequest(BaseModel):
    """The version a person believes they are reporting under.

    Optional because the current handover is the ordinary answer; when it is
    supplied it has to agree with the handover the report lands on, so a report
    about version one cannot be recorded as an observation about version two.
    """

    protocol_version: int | None = Field(default=None, ge=1)


class LeadConfirmationRequest(BaseModel):
    """A lab lead's statement that SOP, site and device suit one package."""

    protocol_version: int = Field(ge=1)
    #: The exact prepared package. Required when several preparations exist for
    #: a node, because a confirmation is about one package's files and terms;
    #: omitted means the newest prepared package for `protocol_version`.
    preparation_id: str | None = Field(default=None, max_length=32)
    sop_ref: str = Field(min_length=1, max_length=256)
    site_ref: str = Field(min_length=1, max_length=256)
    device_identity: str = Field(min_length=1, max_length=256)
    statement: str = Field(min_length=1, max_length=4_000)


#: Real human-lab acceptance is T26 and external. T11 proves the software
#: handover with simulated stand-ins, and every surface that reports a
#: handover says which side of that line it is on.
REAL_LAB_ACCEPTANCE: dict[str, str] = {
    "status": "BLOCKED_EXTERNAL",
    "task": "T26",
    "reason": (
        "Real human-lab acceptance needs a real laboratory, a real operator and "
        "a real instrument. T11's automated people prove software handover only; "
        "a SIMULATED handover is not instrument evidence."
    ),
}


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


def _readiness(
    session: Session, project_id: str, preparation_id: str
) -> dict[str, Any] | None:
    """What one prepared package is bound to, and whether it may open for real.

    `ready_for_real_execution` is derived from a covering lead confirmation,
    not from a stored flag: the binding names the protocol version, samples,
    required raw files, device and SOP references, and a confirmation is
    covering only when its protocol version and device agree and its SOP is one
    the package names. A package with no confirmation — or with one about a
    different instrument — is not ready, however complete its file list is.

    Reading never creates the binding. A package nobody has used or confirmed
    has no binding yet, and a GET that wrote one would turn a read into a fact.
    """
    if not preparation_id:
        return None
    try:
        package = PreparationRepository(session, project_id).get(
            preparation_id=preparation_id
        )
    except NotFound:
        return None
    binding = LabPackageRepository(session, project_id).binding_for(preparation_id)
    recorded = LabLeadConfirmationRepository(
        session, project_id
    ).for_package(preparation_id)
    covering = confirmation_for(session, binding) if binding is not None else None
    return {
        "preparation_id": preparation_id,
        "execution_contract_ref": package.execution_contract_ref,
        "execution_contract_version": package.execution_contract_version,
        "workspace_path": package.workspace_path,
        "manifest": package.manifest,
        "binding": None if binding is None else binding.model_dump(mode="json"),
        "lead_confirmation": (
            None if recorded is None else recorded.model_dump(mode="json")
        ),
        "confirmation_covers_package": (
            recorded is not None
            and binding is not None
            and recorded.covers(binding)
        ),
        "ready_for_real_execution": covering is not None,
    }


def _newest_prepared(
    session: Session, project_id: str, node_id: str
) -> PreparationRecord | None:
    """The newest package RAVEL built for this node, skipping refusals."""
    prepared = [
        record
        for record in PreparationRepository(session, project_id).for_node(node_id)
        if record.is_prepared
    ]
    return prepared[-1] if prepared else None


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

    The person-reported events and the upload receipts are listed in full
    because they are the audit trail T11 is about: who signed, who started,
    what deviation was said verbatim, and which late upload was refused. They
    sit beside — not instead of — the artifact records.
    """
    package = _package(session, project_id, handover)
    answered = arrived_outputs(session, handover)
    names = tuple(name for name, _artifact in answered)
    events = LabEventRepository(session, project_id).for_handover(
        handover.handover_id
    )
    receipts = LabUploadReceiptRepository(session, project_id).for_handover(
        handover.handover_id
    )
    readiness = _readiness(session, project_id, handover.preparation_id)
    stopped = any(
        event.event_type is LabEventType.STOP_CONFIRMED for event in events
    )
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
        "execution_mode": handover.execution_mode.value,
        "evidence_execution_type": handover.evidence_execution_type.value,
        "package_readiness": readiness,
        "ready_for_real_execution": bool(
            readiness and readiness["ready_for_real_execution"]
        ),
        "awaiting_manual_stop_confirmation": (
            handover.state is JobState.CANCELLED and not stopped
        ),
        "person_reported_events": [
            event.model_dump(mode="json") for event in events
        ],
        "upload_receipts": [
            receipt.model_dump(mode="json") for receipt in receipts
        ],
        "instrument_state": None,
        "scientific_verdict": None,
        "real_lab_acceptance": REAL_LAB_ACCEPTANCE,
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

    `package_readiness` is present before a handover too, because a lab lead
    confirms a package *before* it is opened at a bench. Without it the
    confirmation would have no preparation to name.
    """
    latest = BackendJobRepository(session, project_id).latest_for_node(node.node_id)
    handover = LabHandoverRepository(session, project_id).latest_for_node(node.node_id)
    prepared = _newest_prepared(session, project_id, node.node_id)
    return {
        "node": node.model_dump(mode="json"),
        "instruction": _instruction(session, project_id, node),
        "backend_job": None if latest is None else latest.model_dump(mode="json"),
        "handover": None
        if handover is None
        else _handover_view(session, project_id, handover),
        "package_readiness": None
        if prepared is None
        else _readiness(session, project_id, prepared.preparation_id),
        "real_lab_acceptance": REAL_LAB_ACCEPTANCE,
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


def _human_operator(standing: Principal) -> None:
    """Refuse a bench report from anybody who is not a lab user.

    Reads are open to every member; *reporting what happened at a bench* is
    not. A project owner or administrator may intervene, approve and read, but
    cannot sign for an operator or upload as one — that would make the
    person-reported record no longer person-reported. The check is here rather
    than in each route so the several human doors cannot disagree.

    Raises:
        HTTPException: 403.
    """
    if standing.role is not UserRole.LAB_USER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "only a LAB_USER reports bench events; this account holds "
                f"{standing.role.value}, and a sign-off, start report, upload, "
                "deviation or stop confirmation must be the person's own"
            ),
        )


def _lab_lead(standing: Principal) -> None:
    """Refuse a lead confirmation from anybody but the project owner.

    V0 has no separate lab-lead account, and the project owner is the human
    authority that can bind the project to a laboratory's SOP, site and
    device. A lab user cannot confirm their own laboratory's suitability, and
    an administrator who is not the owner cannot either.

    Raises:
        HTTPException: 403.
    """
    if standing.role is not UserRole.PROJECT_OWNER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "only the project owner may confirm that a laboratory's SOP, "
                "site and device suit a prepared package; this account holds "
                f"{standing.role.value}"
            ),
        )


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
    protocol_version: Annotated[
        int | None,
        Query(
            ge=1,
            description=(
                "The contract version this file answers; omitted means the run "
                "now waiting."
            ),
        ),
    ] = None,
    sample_id: Annotated[
        str | None,
        Query(
            max_length=128,
            description="The sample label from the package this file belongs to.",
        ),
    ] = None,
) -> dict[str, Any]:
    """File the body as the artifact that answers one output, and tell the run.

    **A file that answers nothing is refused.** `output` has to be one of the
    outputs this handover owed, checked before a byte is stored, so there is no
    way to upload something the contract did not ask for and no way for one to
    count towards a delivery. That is the mechanism, not a policy: the delivery
    the run completes on is computed from the *recorded* names compared against
    the handover's frozen list, so an arbitrary file could not complete a run
    even if it were accepted.

    **Only a lab user uploads.** The person who ran the experiment is the
    person holding the file, and the record has to be their own report; an
    owner or administrator relaying a file would be uploading something they
    never saw and signing for somebody else.

    Uploading does not complete anything. What it does is make the file a fact
    and tell the run, which then decides whether it has everything; and it is
    idempotent in the bytes, because the same file sent twice is one version
    with one hash rather than two versions of one output.

    **Every attempt leaves a receipt, including the refused ones.** A late file
    after a withdrawal, a file for the wrong contract version, a sample the
    package never named and an output the contract never required are all
    recorded with their digest and size before the route refuses them. The
    bytes of a refused upload are not stored as an artifact, and the receipt is
    the trace that says what was sent and why it could not count.

    Raises:
        HTTPException: 403 if the caller is not a LAB_USER. 404 if this project
            has no such task. 409 if nothing is in anybody's hands, if the
            handover has ended, if the version or sample is not this run's, or
            if the package owes no such output — the message says which and
            what is owed instead. 413 if the body is over the cap. 502 if the
            store refused the bytes. 503 if the deployment has no store at all.
    """
    _human_operator(standing)
    if filename:
        reason = unusable_filename_reason(filename)
        if reason is not None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"this filename cannot name an artifact: {reason}",
            )

    limit = state.settings.gateway_upload_limit_bytes
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {limit} bytes",
        )

    store = artifact_store(state)
    temporary = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w+b", prefix="ravel-lab-upload-", suffix=".body", delete=False
    )
    temporary_path = Path(temporary.name)
    digest = hashlib.sha256()
    size = 0
    try:
        stream = request.stream()
        try:
            async for chunk in stream:
                if not chunk:
                    continue
                if temporary.tell() + len(chunk) > limit:
                    raise UploadTooLarge(limit)
                temporary.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        finally:
            await stream.aclose()
        temporary.flush()
        temporary.close()

        accepted = False
        rejection = ""
        receipt: LabUploadReceipt
        event: LabEvent | None = None
        artifact = version = None
        answered: tuple[tuple[str, Any], ...] = ()
        try:
            with state.database.transaction() as session:
                node = _experiment_node(session, standing.project_id, task_id)
                handover, versions = _upload_target(
                    session, standing.project_id, node, protocol_version
                )
                if handover is None:
                    raise HTTPException(
                        status_code=status.HTTP_409_CONFLICT,
                        detail=(
                            f"task {node.display_id} has not been handed to a "
                            "bench, so there is nothing for an upload to answer; "
                            "a handover exists once a run has prepared a package "
                            "and given it to somebody"
                        ),
                    )
                binding = (
                    LabPackageRepository(
                        session, standing.project_id
                    ).binding_for(handover.preparation_id)
                    if handover.preparation_id
                    else None
                )
                if (
                    protocol_version is not None
                    and handover.execution_contract_version != protocol_version
                ):
                    rejection = (
                        f"this file declares contract version {protocol_version}, "
                        f"but the handover it reached is version "
                        f"{handover.execution_contract_version}; this task's "
                        f"handovers are versions {list(versions)}. A file for "
                        "another version keeps its provenance but cannot answer "
                        "this run."
                    )
                    receipt = _record_receipt(
                        session,
                        standing,
                        handover,
                        output=output,
                        filename=filename,
                        media_type=media_type or _media_type(filename),
                        digest=digest.hexdigest(),
                        size=size,
                        outcome=LabUploadOutcome.REJECTED_WRONG_CONTRACT_VERSION,
                        detail=rejection,
                    )
                elif handover.is_terminal:
                    rejection = (
                        f"the handover for task {node.display_id} ended as "
                        f"{handover.state.value} and cannot be answered; what was "
                        "already recorded against it stands, the late upload is "
                        "recorded as a receipt, and a new run is a new handover."
                    )
                    receipt = _record_receipt(
                        session,
                        standing,
                        handover,
                        output=output,
                        filename=filename,
                        media_type=media_type or _media_type(filename),
                        digest=digest.hexdigest(),
                        size=size,
                        outcome=LabUploadOutcome.REJECTED_LATE,
                        detail=rejection,
                    )
                elif not handover.answers(output):
                    rejection = (
                        f"this handover owes no output called {output!r}; it owes "
                        f"{list(handover.required_outputs)}. An upload answers a "
                        "name the contract required, and a file that answers "
                        "nothing is not part of a delivery."
                    )
                    receipt = _record_receipt(
                        session,
                        standing,
                        handover,
                        output=output,
                        filename=filename,
                        media_type=media_type or _media_type(filename),
                        digest=digest.hexdigest(),
                        size=size,
                        outcome=LabUploadOutcome.REJECTED_UNKNOWN_OUTPUT,
                        detail=rejection,
                    )
                elif sample_id is not None and (
                    binding is None or sample_id not in binding.sample_ids
                ):
                    labels = [] if binding is None else list(binding.sample_ids)
                    rejection = (
                        f"sample {sample_id!r} is not one the package bound for "
                        f"this handover; the package names {labels}. A sample "
                        "label that is not in the binding cannot be traced from "
                        "the upload back to what the bench was given."
                    )
                    receipt = _record_receipt(
                        session,
                        standing,
                        handover,
                        output=output,
                        filename=filename,
                        media_type=media_type or _media_type(filename),
                        digest=digest.hexdigest(),
                        size=size,
                        outcome=LabUploadOutcome.REJECTED_WRONG_SAMPLE,
                        detail=rejection,
                    )
                else:
                    artifact, version = record_upload(
                        session,
                        store,
                        handover,
                        output=output,
                        chunks=_file_chunks(temporary_path),
                        uploaded_by=standing.user_id,
                        filename=filename,
                        media_type=media_type or _media_type(filename),
                        sample_id=sample_id,
                    )
                    receipt = _record_receipt(
                        session,
                        standing,
                        handover,
                        output=output,
                        filename=filename,
                        media_type=media_type or _media_type(filename),
                        digest=digest.hexdigest(),
                        size=size,
                        outcome=LabUploadOutcome.ACCEPTED,
                        detail="",
                        artifact_id=artifact.artifact_id,
                        artifact_version=version.version,
                    )
                    event = LabEventRepository(
                        session, standing.project_id
                    ).upload_event_for(
                        handover.handover_id,
                        artifact.artifact_id,
                        version.sha256,
                    )
                    answered = arrived_outputs(session, handover)
                    accepted = True
        except ArtifactStoreError as refused:
            logger.warning(
                "lab upload to project %s failed in the store: %s",
                standing.project_id,
                type(refused).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="the artifact store refused the upload",
            ) from refused
    except UploadTooLarge as too_big:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"this deployment accepts uploads up to {too_big.limit} bytes",
        ) from too_big
    finally:
        temporary.close()
        temporary_path.unlink(missing_ok=True)

    if not accepted:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"{rejection} This attempt is recorded as upload receipt "
                f"{receipt.receipt_id} with sha256 {receipt.content_sha256}."
            ),
        )
    assert artifact is not None and version is not None, (
        "the accepted path recorded no artifact"
    )

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
        "sample_id": sample_id,
        "answered_outputs": list(names),
        "missing_outputs": list(handover.owed(names)),
        "receipt": receipt.model_dump(mode="json"),
        "person_reported_event": (
            None if event is None else event.model_dump(mode="json")
        ),
        "execution_mode": handover.execution_mode.value,
        "evidence_execution_type": handover.evidence_execution_type.value,
        "scientific_verdict": None,
        "delivered_to_run": delivered,
        "delivery_note": note,
    }


def _event_response(
    event: LabEvent,
    handover: LabHandover,
    *,
    awaiting_manual_stop_confirmation: bool,
) -> dict[str, Any]:
    """What a person is told after a bench report.

    The handover state is repeated unchanged, because no bench report moves it:
    acknowledging, starting and stopping are statements about the bench, and
    the run is ended by a delivery or by Master through the backend. A waiting
    bench is therefore still `WAITING_EXTERNAL` here, never `RUNNING`.
    """
    return {
        "person_reported_event": event.model_dump(mode="json"),
        "handover_id": handover.handover_id,
        "handover_state": handover.state.value,
        "execution_mode": handover.execution_mode.value,
        "evidence_execution_type": handover.evidence_execution_type.value,
        "awaiting_manual_stop_confirmation": awaiting_manual_stop_confirmation,
        "instrument_state": None,
        "scientific_verdict": None,
        "real_lab_acceptance": REAL_LAB_ACCEPTANCE,
    }


def _refuse_a_version_mismatch(
    handover: LabHandover, protocol_version: int | None
) -> None:
    """Refuse a report that declares a version other than the handover's.

    Raises:
        HTTPException: 409, naming both versions.
    """
    if (
        protocol_version is not None
        and protocol_version != handover.execution_contract_version
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"this report is about contract version {protocol_version}, but "
                f"the handover it reached is version "
                f"{handover.execution_contract_version}; a report about one "
                "version cannot be recorded against another."
            ),
        )


@router.post(
    "/{project_id}/lab/tasks/{task_id}/sign-off",
    status_code=status.HTTP_201_CREATED,
    summary="Sign for the package a bench was given",
)
def sign_off(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: HumanReportRequest | None = None,
) -> dict[str, Any]:
    """Record that the person at the bench accepted the package.

    **This is the person's signature, not RAVEL's inference.** Nothing in
    software can tell that a human read the SOP or stood at the instrument, so
    the only thing that can create this event is the LAB_USER saying so. The
    event is person-reported by construction and the handler keeps that true by
    refusing every other role.

    A sign-off is idempotent: sending it twice returns the event already
    recorded, because a retried request is not a second signature. It does not
    start the run and does not move the handover — a signed package is still
    `WAITING_EXTERNAL`, because nothing RAVEL can see is running while a person
    works.
    """
    _human_operator(standing)
    with state.database.transaction() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        handover = _owed_handover(session, standing.project_id, node)
        if request is not None:
            _refuse_a_version_mismatch(handover, request.protocol_version)
        event = LabEventRepository(session, standing.project_id).record_milestone(
            LabEvent(
                project_id=standing.project_id,
                handover_id=handover.handover_id,
                actor_user_id=standing.user_id,
                protocol_version=handover.execution_contract_version,
                event_type=LabEventType.ACKNOWLEDGED,
                lead_confirmation_ref=handover.lead_confirmation_ref or None,
            )
        )
    return _event_response(
        event, handover, awaiting_manual_stop_confirmation=False
    )


@router.post(
    "/{project_id}/lab/tasks/{task_id}/start",
    status_code=status.HTTP_201_CREATED,
    summary="Report that work at the bench has started",
)
def report_start(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: HumanReportRequest | None = None,
) -> dict[str, Any]:
    """Record that a person says they have started the protocol.

    **A start report does not make the run `RUNNING`.** RAVEL cannot see the
    bench; what it can record is that a person said they began. The handover
    therefore stays `WAITING_EXTERNAL`, and the start is an event beside it
    rather than a state transition that would claim observability RAVEL does
    not have.

    The sign-off has to exist first, because "started" is a statement about a
    package the person already accepted; without that ordering the record
    could show work started under terms nobody signed for. Reporting a start
    twice returns the first event, so a retry is not a second start.
    """
    _human_operator(standing)
    with state.database.transaction() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        handover = _owed_handover(session, standing.project_id, node)
        if request is not None:
            _refuse_a_version_mismatch(handover, request.protocol_version)
        events = LabEventRepository(session, standing.project_id)
        if events.of_type(handover.handover_id, LabEventType.ACKNOWLEDGED) is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "nobody has signed for this handover, so work cannot be "
                    "reported as started; the lab user signs for the package "
                    "first, and a start is a statement about work under terms "
                    "they accepted."
                ),
            )
        event = events.record_milestone(
            LabEvent(
                project_id=standing.project_id,
                handover_id=handover.handover_id,
                actor_user_id=standing.user_id,
                protocol_version=handover.execution_contract_version,
                event_type=LabEventType.START_REPORTED,
                lead_confirmation_ref=handover.lead_confirmation_ref or None,
            )
        )
    return _event_response(
        event, handover, awaiting_manual_stop_confirmation=False
    )


@router.post(
    "/{project_id}/lab/tasks/{task_id}/stop-confirmation",
    status_code=status.HTTP_201_CREATED,
    summary="Confirm that the instrument was stopped by a person",
)
def confirm_stop(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: HumanReportRequest | None = None,
) -> dict[str, Any]:
    """Record the person's confirmation that the instrument was stopped.

    **Only a person can produce this event.** A system cancellation revokes
    RAVEL's handover and receiving eligibility and then says it is awaiting
    manual stop confirmation; it never writes this event, because whether the
    bench stopped is not something RAVEL can see. This route is the other side
    of that: the operator says the instrument is stopped, and the statement is
    recorded as theirs.

    The confirmation never changes the handover state. It is valid after a
    withdrawal as well as during work — a late confirmation that the bench
    stopped is exactly what the awaiting flag was waiting for — and it is
    idempotent, since a second confirmation is a retry rather than a second
    stop.
    """
    _human_operator(standing)
    with state.database.transaction() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        handover, _versions = _upload_target(
            session,
            standing.project_id,
            node,
            None if request is None else request.protocol_version,
        )
        if handover is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"task {node.display_id} has not been handed to a bench, so "
                    "there is no instrument whose stop a person could confirm"
                ),
            )
        if request is not None:
            _refuse_a_version_mismatch(handover, request.protocol_version)
        event = LabEventRepository(session, standing.project_id).record_milestone(
            LabEvent(
                project_id=standing.project_id,
                handover_id=handover.handover_id,
                actor_user_id=standing.user_id,
                protocol_version=handover.execution_contract_version,
                event_type=LabEventType.STOP_CONFIRMED,
                lead_confirmation_ref=handover.lead_confirmation_ref or None,
            )
        )
    return _event_response(
        event,
        handover,
        awaiting_manual_stop_confirmation=False,
    )


@router.post(
    "/{project_id}/lab/tasks/{task_id}/lead-confirmation",
    status_code=status.HTTP_201_CREATED,
    summary="A lab lead confirms SOP, site and device suit the package",
)
def confirm_lab_lead(
    standing: PrincipalDep,
    state: GatewayStateDep,
    task_id: str,
    request: LeadConfirmationRequest,
) -> dict[str, Any]:
    """Bind a prepared package to what the lab lead says about the laboratory.

    **Without this, a package is not ready for real execution.** The binding is
    derived from the package's own manifest — protocol version, sample labels,
    required raw files, device identity and SOP references — and readiness is
    derived from a confirmation that actually covers it. There is no mutable
    "ready" flag: a lead who confirms protocol 2 on instrument A says nothing
    about protocol 3 on instrument B, and `LabLeadConfirmation.covers` is where
    that is decided.

    **RAVEL can verify linkage, not reality.** It can check that the SOP named
    is one the package carries and the device is the device the contract asked
    for. It cannot check that the laboratory is what the statement says, which
    is why the confirmation is a signed human record and why real acceptance
    remains T26 and external.

    Raises:
        HTTPException: 403 if the caller is not the project owner. 404 if the
            task or the named preparation is not in this project. 409 if the
            package is not prepared, is for another node or version, or if the
            SOP, site or device do not agree with its binding.
    """
    _lab_lead(standing)
    with state.database.transaction() as session:
        node = _experiment_node(session, standing.project_id, task_id)
        preparations = PreparationRepository(
            session, standing.project_id
        ).for_node(node.node_id)
        if request.preparation_id is not None:
            try:
                package = PreparationRepository(
                    session, standing.project_id
                ).get(preparation_id=request.preparation_id)
            except NotFound as missing:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=(
                        f"no preparation {request.preparation_id!r} in this "
                        "project"
                    ),
                ) from missing
        else:
            package = next(
                (
                    record
                    for record in reversed(preparations)
                    if record.is_prepared
                    and record.execution_contract_version
                    == request.protocol_version
                ),
                None,
            )
            if package is None:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(
                        f"no prepared package for contract version "
                        f"{request.protocol_version} exists for task "
                        f"{node.display_id}, so there is nothing to confirm"
                    ),
                )
        if package.node_id != node.node_id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"preparation {package.preparation_id} belongs to node "
                    f"{package.node_id}, not task {node.display_id}"
                ),
            )
        if not package.is_prepared:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"preparation {package.preparation_id} was not prepared "
                    "successfully, so it is not a package a lead can confirm"
                ),
            )
        if package.execution_contract_version != request.protocol_version:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"preparation {package.preparation_id} is contract version "
                    f"{package.execution_contract_version}, but the confirmation "
                    f"declares {request.protocol_version}"
                ),
            )
        try:
            binding = LabPackageRepository(
                session, standing.project_id
            ).ensure_bound(package)
        except ValueError as unusable:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"this package cannot be bound: {unusable}",
            ) from unusable
        if request.device_identity != binding.device_identity:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"the package was bound to device "
                    f"{binding.device_identity!r}, but the confirmation names "
                    f"{request.device_identity!r}; a lead confirms the device "
                    "the package was actually built for"
                ),
            )
        if request.sop_ref not in binding.sop_refs:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"SOP {request.sop_ref!r} is not one the package references; "
                    f"it references {list(binding.sop_refs)}. A confirmation "
                    "about an SOP the bench was not given does not cover this "
                    "package."
                ),
            )
        confirmation = LabLeadConfirmationRepository(
            session, standing.project_id
        ).record_once(
            LabLeadConfirmation(
                project_id=standing.project_id,
                node_id=node.node_id,
                preparation_id=package.preparation_id,
                execution_contract_ref=package.execution_contract_ref,
                execution_contract_version=package.execution_contract_version,
                protocol_version=request.protocol_version,
                sop_ref=request.sop_ref,
                site_ref=request.site_ref,
                device_identity=request.device_identity,
                statement=request.statement,
                confirmed_by=standing.user_id,
            )
        )
        readiness = _readiness(session, standing.project_id, package.preparation_id)
    return {
        "confirmation": confirmation.model_dump(mode="json"),
        "covers_package": confirmation.covers(binding),
        "package_readiness": readiness,
        "ready_for_real_execution": bool(
            readiness and readiness["ready_for_real_execution"]
        ),
        "real_lab_acceptance": REAL_LAB_ACCEPTANCE,
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
    _human_operator(standing)
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
        target, _versions = _upload_target(
            session,
            standing.project_id,
            node,
            request.protocol_version,
        )
        if target is not None:
            _refuse_a_version_mismatch(target, request.protocol_version)
        binding = (
            LabPackageRepository(
                session, standing.project_id
            ).binding_for(target.preparation_id)
            if target is not None and target.preparation_id
            else None
        )
        if (
            request.sample_id is not None
            and binding is not None
            and request.sample_id not in binding.sample_ids
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"sample {request.sample_id!r} is not one the package bound "
                    f"for this handover; the package names "
                    f"{list(binding.sample_ids)}."
                ),
            )
        if target is not None and len(request.description) > 4000:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "a deviation against a handed-over run is recorded as a "
                    "person-reported lab event, whose verbatim note holds up to "
                    "4000 characters; this report is longer. Shorten it or split "
                    "the observations, so the event keeps the text verbatim "
                    "rather than truncating it."
                ),
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
        event: LabEvent | None = None
        if target is not None:
            event = LabEventRepository(session, standing.project_id).record(
                LabEvent(
                    project_id=standing.project_id,
                    handover_id=target.handover_id,
                    actor_user_id=standing.user_id,
                    protocol_version=target.execution_contract_version,
                    event_type=LabEventType.DEVIATION_REPORTED,
                    sample_id=request.sample_id,
                    lead_confirmation_ref=target.lead_confirmation_ref or None,
                    raw_output_ref=request.raw_output_ref,
                    raw_output_sha256=request.raw_output_sha256,
                    deviation_note=request.description,
                )
            )
        waiting = target if target is not None and target.is_waiting else None

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
        "person_reported_event": (
            None if event is None else event.model_dump(mode="json")
        ),
        "handover_id": None if target is None else target.handover_id,
        "execution_mode": (
            None if target is None else target.execution_mode.value
        ),
        "evidence_execution_type": (
            None if target is None else target.evidence_execution_type.value
        ),
        "instrument_state": None,
        "scientific_verdict": None,
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


def _upload_target(
    session: Session,
    project_id: str,
    node: DagNode,
    protocol_version: int | None,
) -> tuple[LabHandover | None, tuple[int, ...]]:
    """The handover an upload reaches, and the versions this node has had.

    A declared version wins when it names a handover this node really had, so
    a late file for an ended version lands on *that* handover and is recorded
    there as late rather than being refused against the version now waiting.
    Without a declaration the live handover is the target; if nothing is live,
    the newest one is, which is what makes a late upload a recorded rejection
    instead of a 409 that leaves no trace.

    `None` means this node was never handed to a bench at all. That is not a
    late upload and it has no handover to attach a receipt to.
    """
    repository = LabHandoverRepository(session, project_id)
    handovers = repository.for_node(node.node_id)
    if not handovers:
        return None, ()
    versions = tuple(
        sorted({handover.execution_contract_version for handover in handovers})
    )
    if protocol_version is not None:
        matching = [
            handover
            for handover in handovers
            if handover.execution_contract_version == protocol_version
        ]
        if matching:
            return matching[-1], versions
    waiting = repository.waiting_for(node.node_id)
    if waiting is not None:
        return waiting, versions
    return handovers[-1], versions


def _record_receipt(
    session: Session,
    standing: Principal,
    handover: LabHandover,
    *,
    output: str,
    filename: str,
    media_type: str,
    digest: str,
    size: int,
    outcome: LabUploadOutcome,
    detail: str,
    artifact_id: str | None = None,
    artifact_version: int | None = None,
) -> LabUploadReceipt:
    """Record one upload attempt against the handover it reached.

    Written for every outcome, so a refusal is a record rather than silence.
    The artifact identifiers are present only for `ACCEPTED`, because the
    bytes of a refused upload are not in the artifact store at all.
    """
    return LabUploadReceiptRepository(session, standing.project_id).record(
        LabUploadReceipt(
            project_id=standing.project_id,
            node_id=handover.node_id,
            handover_id=handover.handover_id,
            output=output,
            filename=filename,
            media_type=media_type,
            content_sha256=digest,
            size_bytes=size,
            uploaded_by=standing.user_id,
            outcome=outcome,
            detail=detail,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            protocol_version=handover.execution_contract_version,
        )
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
