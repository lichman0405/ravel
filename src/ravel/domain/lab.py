"""What RAVEL handed to a bench, and what the bench gave back.

A laboratory run has no scheduler to ask and no remote directory to leave a note
in. The `SlurmComputeBackend` finds a run again by reading the `submission.json`
it uploaded next to the work; a bench has nowhere to upload one to, and the
person standing at it is not a machine that can be queried. So the handover is
recorded here, in RAVEL's own state, and it is the same kind of fact: **which
work was handed over, under which terms, and what it owed.**

That is the whole reason this record exists rather than being derived. The
running job row, the frozen contract, the prepared package and the uploaded
artifacts are each somewhere in PostgreSQL already, and a reader could in
principle assemble the handover out of them — but the port hands a backend a
reference and nothing else, so a backend that had to re-derive its own work from
four tables would be re-deriving it from records that can move under it. A
contract revised while the bench was working would silently change what the run
owed. The handover is therefore written once, at the moment of the handover, and
carries the terms frozen as they were: `required_outputs` here is a copy of the
contract's list, and the copy is the point — see `JobRequest.task_spec`, which
is duplicated into the backend's hands for the same reason.

**What the lab user uploaded is not recorded here.** It is an artifact version,
because that is what it is: bytes with an author, a time, a media type and a
hash, filed under the name of the output they satisfy. `ArtifactVersionRow`
already carries every one of those columns, and `node_id` and `created_by` are
already how RAVEL says which run a file belongs to and who produced it. A second
table of uploads would be a second answer to "what did the bench deliver".

**A handover's state is one of four, and it starts where the mock's does.** It
is `WAITING_EXTERNAL` from the moment it is written until something ends it,
which is what the directive says a laboratory wait is: not a long `RUNNING`, and
not a state a person has to be polled to leave. The moves out of it are the
job state machine's own — `can_transition_job` is the check — so a handover
cannot reach a state a job could not, and `is_terminal` on a handover means what
it means on a job.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from pydantic import Field, model_validator

from ravel.domain.base import Record
from ravel.domain.clock import utcnow
from ravel.domain.enums import (
    EvidenceExecutionType,
    JobState,
    LabEventType,
    LabExecutionMode,
    LabUploadOutcome,
)
from ravel.domain.ids import new_id
from ravel.domain.state_machines import can_transition_job

#: The states a handover can be in: waiting, or one of the four endings. Named
#: as a tuple so the database constraint and the record's validator are derived
#: from one list rather than written twice — a state added to `JobState`
#: tomorrow is not silently a state a handover may be in.
HANDOVER_STATES: tuple[JobState, ...] = (
    JobState.WAITING_EXTERNAL,
    JobState.COMPLETED,
    JobState.CANCELLED,
    JobState.TIMED_OUT,
)

#: The prefix an uploaded file's `provenance` carries, so that "which handover
#: does this answer" is a checkable field rather than a sentence. The same role
#: `slurm:{job_id}` plays for collected output, and the same reason.
UPLOAD_PROVENANCE = "lab-upload"


class LabHandover(Record):
    """One piece of work RAVEL handed to a person, while it is in their hands.

    Mutable in exactly the way `BackendJob` is: the state changes while the
    bench works, and nothing else about it does. Which attempt it is, which
    contract it was handed over under, which package the bench was given and
    what that package owed are fixed at the handover and enforced as such in the
    database — a handover whose `required_outputs` could be edited after the
    bench delivered would let the thing that is checked be made to agree with
    whatever arrived.
    """

    handover_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    #: Which try at this work was handed over. Part of the identity with the
    #: node, for the reason `BackendJob.attempt` is: it is what makes handing
    #: the same work over twice return the first handover rather than opening a
    #: second one with the same bench.
    attempt: int = Field(ge=1)
    #: How the backend is named in `ExecutionRecord.backend`, and the string a
    #: `backend_job_ref` carries so that a reference says which kind of backend
    #: it belongs to.
    backend: str = Field(min_length=1)
    execution_contract_ref: str = Field(min_length=1)
    execution_contract_version: int = Field(ge=1)
    #: The prepared package the bench was given. A laboratory run is handed
    #: files rather than a command, so this is what a `workspace_path` is for a
    #: cluster job — and the two are recorded the same way rather than one of
    #: them being inferred from "we must have prepared something".
    preparation_id: str = ""
    workspace_path: str = ""
    #: The file in that workspace a person starts from. Named rather than
    #: assumed, because a materializer decides what a package's entry point is
    #: and a backend that guessed `experimental_protocol.md` would break the day
    #: a second kind of bench exists.
    protocol: str = ""
    #: What this handover owed, frozen at the handover. An upload satisfies a
    #: name here or it is an extra file: the check is against this list, not
    #: against what a delivery claims to have brought.
    required_outputs: tuple[str, ...] = ()
    #: Set at the handover and immutable. A handover that is not authorized for
    #: a live laboratory is `SIMULATED`, and uploads against it are filed with
    #: the artifact kind `simulated`, so the evidence they produce is visibly a
    #: software proof rather than a measurement.
    execution_mode: LabExecutionMode = LabExecutionMode.SIMULATED
    #: The lab lead's confirmation this handover was made under, if one was
    #: recorded against the package before the handover. Empty means none did.
    lead_confirmation_ref: str = ""
    state: JobState = JobState.WAITING_EXTERNAL
    detail: str = ""
    handed_over_at: datetime = Field(default_factory=utcnow)
    closed_at: datetime | None = None

    @model_validator(mode="after")
    def _an_ending_is_all_or_nothing(self) -> LabHandover:
        """A terminal state and a closing time are the same statement.

        The same rule `BackendJob` enforces, for the same reason: a handover
        that ended with no record of when, or one still open with a time it
        closed, is read as fact by whatever comes next.
        """
        if self.state.is_terminal and self.closed_at is None:
            raise ValueError(
                f"handover {self.handover_id} is {self.state.value} but records no "
                "closing time; a handover that ended ended at some point"
            )
        if not self.state.is_terminal and self.closed_at is not None:
            raise ValueError(
                f"handover {self.handover_id} is {self.state.value} but records a "
                "closing time; only a terminal state ends a handover"
            )
        return self

    @model_validator(mode="after")
    def _a_handover_is_only_ever_waiting_or_over(self) -> LabHandover:
        """Refuse the states a handover cannot be in.

        A handover is written at the moment somebody is asked, so `SUBMITTED` —
        "given to a backend that has not started it" — is a state it is never
        in. `RUNNING` is the one this check exists for: a bench is worked by a
        person, RAVEL cannot see them working, and a `RUNNING` that meant "we
        assume somebody is at the bench" is exactly the long-running lie the
        directive forbids.
        """
        if self.state not in HANDOVER_STATES:
            raise ValueError(
                f"handover {self.handover_id} is {self.state.value}; a handover is "
                "WAITING_EXTERNAL until it ends, because nothing RAVEL can see is "
                "running while a person works"
            )
        return self

    @property
    def is_terminal(self) -> bool:
        """Whether the handover has ended."""
        return self.state.is_terminal

    @property
    def is_waiting(self) -> bool:
        """Whether the bench still has this work."""
        return self.state is JobState.WAITING_EXTERNAL

    @property
    def upload_provenance(self) -> str:
        """The provenance a file uploaded against this handover carries.

        What makes "did this arrive for *this* handover" answerable without
        reading a timestamp or guessing from filenames. The Slurm backend asks
        the same question of `slurm:{job_id}`; here the identity is the
        handover rather than the cluster's job, because a node handed over
        twice has two handovers and the second must not inherit the first's
        deliverables.

        The provenance identifies the handover in both modes. What tells a
        reader whether the bytes are instrument evidence or a software proof
        is the artifact's `kind`: uploads against a `SIMULATED` handover are
        filed as `simulated`, so the report classifier types them SIMULATED
        even though the provenance names a real handover row. Relabelling the
        provenance would not change that.
        """
        return f"{UPLOAD_PROVENANCE}:{self.handover_id}"

    @property
    def evidence_execution_type(self) -> EvidenceExecutionType:
        """How a report must type this handover's delivery.

        Derived from the handover's own recorded mode rather than from a caller
        label, for the same reason report classification is derived from
        provenance: a simulated run cannot become real by relabelling a claim.
        """
        if self.execution_mode is LabExecutionMode.SIMULATED:
            return EvidenceExecutionType.SIMULATED
        return EvidenceExecutionType.HUMAN_LAB

    @property
    def execution_ref(self) -> str:
        """The run an uploaded file belongs to, as `ArtifactVersion` records it.

        `(node_id, contract_version)` is what identifies a run, and `node_id` is
        already a column on the version. What is left to record is the version,
        and it is recorded with the contract it is a version *of*: a bare `2`
        would be a number whose meaning depends on which contract a reader
        happens to be looking at.
        """
        return f"{self.execution_contract_ref}.v{self.execution_contract_version}"

    def arrived(self, names: Iterable[str]) -> tuple[str, ...]:
        """Of `names`, the ones this handover owed and that have arrived.

        Order is the contract's, and duplicates are collapsed: what a caller
        asks is "which of the things I asked for are here", and a list that
        answered in delivery order would make two deliveries that differ only
        in the order they arrived look like different results.
        """
        present = set(names)
        return tuple(name for name in _unique(self.required_outputs) if name in present)

    def owed(self, names: Iterable[str]) -> tuple[str, ...]:
        """Of `names`, the ones this handover owed and that have not arrived.

        The list that decides whether a delivery completes the run, and
        deliberately computed from what is *recorded* rather than from what a
        delivery says it brought: a caller that could complete a run by
        claiming to have delivered something would make the check advisory.
        """
        present = set(names)
        return tuple(name for name in _unique(self.required_outputs) if name not in present)

    def answers(self, name: str) -> bool:
        """Whether `name` is one of the outputs this handover owed.

        The question the upload path asks before filing anything: a file that
        claims an output this handover never owed is not a deliverable, and
        filing it under a name nothing checks would put a document in the
        project that no verdict, no completeness check and no review would
        ever look at.
        """
        return name in self.required_outputs

    def moved_to(
        self, state: JobState, *, at: datetime | None = None, detail: str = ""
    ) -> LabHandover:
        """Return this handover in a new state, validated.

        Raises:
            TransitionError: The move is not one the job state machine allows,
                or the handover has already ended.
        """
        can_transition_job(self.state, state).raise_if_denied()
        if state is self.state:
            return self
        return LabHandover.model_validate(
            {
                **self.model_dump(),
                "state": state,
                "detail": detail or self.detail,
                "closed_at": (at or utcnow()) if state.is_terminal else None,
            }
        )


class LabEvent(Record):
    """One thing an authorized person at a bench reported.

    The shape is the frozen `schemas/premium/lab_event.schema.json`. Every
    event is person-reported and the field is a `Literal[True]` rather than a
    boolean default, so a caller cannot construct a system-reported event even
    if a future route tried to. The database enforces the same constant.

    `protocol_version` is the execution-contract version the person was working
    under, and it is checked against the handover before the event is written:
    a report about version one cannot land on the handover for version two.
    """

    event_id: str = Field(default_factory=new_id)
    project_id: str
    handover_id: str = Field(min_length=1, max_length=64)
    actor_user_id: str = Field(min_length=1, max_length=32)
    protocol_version: int = Field(ge=1)
    event_type: LabEventType
    reported_at: datetime = Field(default_factory=utcnow)
    person_reported: Literal[True] = True
    sample_id: str | None = Field(default=None, max_length=128)
    sop_ref: str | None = Field(default=None, max_length=256)
    lead_confirmation_ref: str | None = Field(default=None, max_length=256)
    raw_output_ref: str | None = Field(default=None, max_length=1024)
    raw_output_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    deviation_note: str | None = Field(default=None, max_length=4000)


class LabDeviationLink(Record):
    """The written join between a deviation and the person-reported event.

    A deviation record is Master's question; a `DEVIATION_REPORTED` lab event
    is the bench's answer. They are separate records because they have
    different authors and different lifetimes, and matching them by node and
    timestamp would be an inference. This link makes the join an identifier:
    given either record, the other is a lookup, not a guess.

    There is one link per deviation and one per event. A report either happened
    at a handover or it did not, and a second link for the same pair would say
    two things about one occurrence.
    """

    link_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    handover_id: str = Field(min_length=1, max_length=64)
    deviation_id: str = Field(min_length=1, max_length=64)
    event_id: str = Field(min_length=1, max_length=64)
    linked_at: datetime = Field(default_factory=utcnow)


class LabPackageBinding(Record):
    """What one prepared package is bound to, written once when it is used.

    The terms are copied from the preparation's own manifest rather than
    re-read from the contract at delivery time: the contract may be revised
    while the bench works, and a package whose sample list could change under
    a result is a package that cannot be traced to what was actually done.

    `required_raw_files` is the manifest's `required_outputs` verbatim. RAVEL
    does not decide which of a contract's required deliverables count as "raw";
    the contract's required list is the set a delivery must answer, and this
    binding copies it rather than classifying it.
    """

    binding_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    preparation_id: str = Field(min_length=1)
    execution_contract_ref: str = Field(min_length=1)
    execution_contract_version: int = Field(ge=1)
    protocol_version: int = Field(ge=1)
    sample_ids: tuple[str, ...] = ()
    required_raw_files: tuple[str, ...] = ()
    device_identity: str = ""
    sop_refs: tuple[str, ...] = ()
    bound_at: datetime = Field(default_factory=utcnow)


class LabLeadConfirmation(Record):
    """A lab lead's confirmation that SOP, site and device suit this package.

    This is the only thing that can make `LabPackageBinding` ready for real
    execution. It is a human record with an author and a statement; RAVEL can
    check that it exists and that its references agree with the package, and
    cannot check that the laboratory is what the statement says it is. That
    last judgement belongs to the real acceptance task, not to software.
    """

    confirmation_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    preparation_id: str = Field(min_length=1)
    execution_contract_ref: str = Field(min_length=1)
    execution_contract_version: int = Field(ge=1)
    protocol_version: int = Field(ge=1)
    sop_ref: str = Field(min_length=1, max_length=256)
    site_ref: str = Field(min_length=1, max_length=256)
    device_identity: str = Field(min_length=1, max_length=256)
    statement: str = Field(min_length=1, max_length=4000)
    confirmed_by: str = Field(min_length=1)
    confirmed_at: datetime = Field(default_factory=utcnow)

    def covers(self, binding: LabPackageBinding) -> bool:
        """Whether this confirmation is about the package as it was built.

        Four things have to agree: the protocol version, the device, one of
        the SOP references the contract named, and a non-empty site. A lead
        who confirmed a different instrument or a superseded protocol has not
        confirmed this package.
        """
        return (
            self.protocol_version == binding.protocol_version
            and self.device_identity == binding.device_identity
            and self.sop_ref in binding.sop_refs
            and bool(self.site_ref.strip())
        )


class LabUploadReceipt(Record):
    """One upload attempt, accepted or refused.

    The artifact version records accepted bytes; this records every attempt,
    including the ones the door refused. A late upload after a withdrawal and
    a corrected upload that becomes version two both leave a receipt, which is
    what makes "the bench kept sending after RAVEL stopped waiting" and "the
    bench supplied a correction" different, traceable facts.
    """

    receipt_id: str = Field(default_factory=new_id)
    project_id: str
    node_id: str
    handover_id: str
    output: str = Field(min_length=1, max_length=200)
    filename: str = ""
    media_type: str = ""
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    uploaded_by: str = Field(min_length=1)
    uploaded_at: datetime = Field(default_factory=utcnow)
    outcome: LabUploadOutcome
    detail: str = ""
    artifact_id: str | None = None
    artifact_version: int | None = None
    #: The contract version the handover was opened under. This is the version
    #: the file has to answer, whichever version the uploader declared.
    protocol_version: int = Field(ge=1)
    #: The sample label the uploader declared, when they declared one. Kept
    #: structured rather than only inside `detail`, so a rejected upload's
    #: sample can be queried without parsing prose.
    sample_id: str | None = Field(default=None, max_length=128)
    #: The contract version the uploader declared, when they declared one.
    #: Null means they named the live handover by omission. A declaration that
    #: disagrees with `protocol_version` is itself the reason for a
    #: `REJECTED_WRONG_CONTRACT_VERSION` receipt, so both values are kept.
    declared_protocol_version: int | None = Field(default=None, ge=1)


def _unique(names: Iterable[str]) -> tuple[str, ...]:
    """`names`, each kept once, in the order first seen."""
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return tuple(seen)


__all__ = [
    "HANDOVER_STATES",
    "UPLOAD_PROVENANCE",
    "LabDeviationLink",
    "LabEvent",
    "LabHandover",
    "LabLeadConfirmation",
    "LabPackageBinding",
    "LabUploadReceipt",
]
