"""Where the work RAVEL handed to a bench is recorded.

One repository and one table, and two of its methods are load-bearing rather
than convenient.

**`hand_over` is idempotent in `(project, node, attempt)`**, and that is the
port's requirement of every backend rather than a nicety of this one: a worker
killed between the backend accepting work and the job reference being recorded
is retried, and the retry has to find the handover it already made instead of
asking the same bench to run the same experiment twice. The deduplication is
PostgreSQL's — an insert that conflicts on the unique key returns the row that
is already there — so two activities racing produce one handover and one
conflict rather than two.

**`close` is the only write that is not a handover**, and it can only move a
handover to an ending. `LabHandover.moved_to` runs the job state machine's own
check, so the endings reachable from `WAITING_EXTERNAL` are the ones a job
could reach from it, and a closed handover cannot be reopened: the four
terminal states have no outgoing edges.

Nothing here deduplicates reads. A node may be handed to a bench under more than
one contract version, and what happened in order is history rather than
duplication, so `for_node` returns all of it and the callers that want one row
say which one they want.
"""

from __future__ import annotations

import tempfile
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ravel.domain.artifacts import SIMULATED_KIND, Artifact, ArtifactVersion, is_simulated
from ravel.domain.enums import JobState, LabEventType, LabExecutionMode
from ravel.domain.lab import (
    LabDeviationLink,
    LabEvent,
    LabHandover,
    LabLeadConfirmation,
    LabPackageBinding,
    LabUploadReceipt,
)
from ravel.domain.preparation import PreparationRecord
from ravel.state.mapping import to_row_data
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import ArtifactStore, hash_chunks
from ravel.state.tables import (
    LabDeviationLinkRow,
    LabEventRow,
    LabHandoverRow,
    LabLeadConfirmationRow,
    LabPackageBindingRow,
    LabUploadReceiptRow,
)


class LabHandoverRepository(ProjectScopedRepository[LabHandover]):
    """Every piece of work this project handed to a laboratory."""

    row_type = LabHandoverRow
    record_type = LabHandover

    def _order_by(self) -> Any:
        """When it was handed over.

        The base class sorts by `created_at`, and this record has no such
        column: a handover is written once, at the moment somebody was asked,
        and `handed_over_at` is that moment.
        """
        return self.row_type.handed_over_at

    def hand_over(self, handover: LabHandover) -> LabHandover:
        """Record that a bench has been given this work.

        Idempotent by the key of the work rather than by anything the caller
        does. A handover already recorded for this attempt is returned as it
        stands — including its state, so a retried submission of work that has
        since been completed answers `COMPLETED` rather than reopening it.

        Raises:
            ProjectScopeError: The handover belongs to a different project. The
                check is explicit because this method writes its own INSERT
                rather than going through `add`, so the guard every other write
                passes is not on this path by default — and a row written from
                a caller-supplied record is exactly where a supplied
                `project_id` would otherwise be taken at its word.
            ValueError: A handover is already recorded for this attempt and it
                describes different terms. That is not a retry: it means two
                different pieces of work were proposed for one attempt, and
                returning either one would hide it.
        """
        self._authorize(handover)
        statement = (
            pg_insert(LabHandoverRow)
            .values(**handover.model_dump(mode="python"))
            .on_conflict_do_nothing(
                index_elements=[
                    "project_id",
                    "node_id",
                    "execution_contract_version",
                    "attempt",
                ]
            )
            .returning(LabHandoverRow.handover_id)
        )
        self.session.execute(statement)
        stored = self.for_attempt(
            handover.node_id, handover.attempt, handover.execution_contract_version
        )
        if stored is None:  # pragma: no cover - the insert cannot vanish
            raise NotFound(
                f"handover for attempt {handover.attempt} of node "
                f"{handover.node_id} was neither inserted nor found, which means "
                "the row was removed"
            )
        if stored.handover_id != handover.handover_id and not _same_work(stored, handover):
            raise ValueError(
                f"attempt {handover.attempt} of node {handover.node_id} under "
                f"contract version {handover.execution_contract_version} is already "
                f"handed over as {stored.handover_id} to backend "
                f"{stored.backend!r}, which is not the work now being handed over"
            )
        return stored

    def note(self, handover_id: str, detail: str) -> LabHandover:
        """Say something about a handover without moving it.

        `detail` is the one column that is neither identity nor state, and it
        exists for the report that changes nothing: a bench that sent one of
        the two files it owes has not moved, and the run is still waiting —
        but "still owed: raw_data" is exactly what a person reading this run
        needs to see, and a job re-asserting the state it is already in writes
        nothing, so there is nowhere else for it to go.

        Raises:
            NotFound: This project has no such handover.
        """
        handover = self.get(handover_id=handover_id)
        noted = handover.model_copy(update={"detail": detail})
        row = self.session.get(LabHandoverRow, handover_id)
        if row is None:  # pragma: no cover - `get` just found the record
            raise NotFound(f"handover {handover_id} is no longer in this project")
        row.detail = noted.detail
        return noted

    def close(
        self, handover_id: str, state: JobState, *, detail: str = ""
    ) -> LabHandover:
        """End a handover, recording why.

        Re-closing a handover that has already ended returns it unchanged: the
        terminal states have no outgoing edges, so a second ending is not a
        move the state machine will make. That matters because two paths can
        reach here for one handover — the run that gave up waiting and the
        delivery that arrived while it was giving up — and the second must be a
        no-op rather than a contradiction of the first.

        Raises:
            NotFound: This project has no such handover.
            TransitionError: The move is not one the state machine allows.
        """
        handover = self.get(handover_id=handover_id)
        if handover.is_terminal:
            return handover
        moved = handover.moved_to(state, detail=detail)
        row = self.session.get(LabHandoverRow, handover_id)
        if row is None:  # pragma: no cover - `get` just found the record
            raise NotFound(f"handover {handover_id} is no longer in this project")
        row.state = moved.state.value
        row.detail = moved.detail
        row.closed_at = moved.closed_at
        return moved

    # ── Reading ─────────────────────────────────────────────────────────────

    def for_node(self, node_id: str) -> list[LabHandover]:
        """Everything this node was handed to a bench as, oldest first."""
        return self.all(node_id=node_id)

    def for_attempt(
        self, node_id: str, attempt: int, execution_contract_version: int
    ) -> LabHandover | None:
        """The handover for one attempt at a node under one contract version.

        All three are the key, for the reason `BackendJobRepository.for_attempt`
        takes all three: a node whose terms Master revised runs again, and its
        attempts start at one again. Keying by the node and the attempt alone
        would make attempt one under the revised contract find the handover
        made under the old one — a bench being told to deliver something it was
        never asked for.
        """
        found = self.all(
            node_id=node_id,
            attempt=attempt,
            execution_contract_version=execution_contract_version,
        )
        return found[-1] if found else None

    def latest_for_node(self, node_id: str) -> LabHandover | None:
        """The most recent handover for a node, if the bench ever had it."""
        handovers = self.for_node(node_id)
        return handovers[-1] if handovers else None

    def waiting(self) -> list[LabHandover]:
        """Every handover a bench has not answered yet.

        What a Lab User's screen is built from: the work that is actually in
        somebody's hands, across the project, rather than per node.
        """
        return [handover for handover in self.all() if handover.is_waiting]

    def waiting_for(self, node_id: str) -> LabHandover | None:
        """The handover this node is in somebody's hands under, if it is.

        At most one, and the reason is the state machine rather than the
        query: a handover ends before another can be made for the same node, so
        two would mean the first was never closed. The newest is returned if
        that ever fails to hold, because the run in flight is the one a caller
        asking this question is about.
        """
        open_here = [
            handover for handover in self.for_node(node_id) if handover.is_waiting
        ]
        return open_here[-1] if open_here else None


class LabEventRepository(ProjectScopedRepository[LabEvent]):
    """Every person-reported event this project recorded.

    Milestones are `ACKNOWLEDGED`, `START_REPORTED` and `STOP_CONFIRMED`: one
    per handover, enforced by partial unique indexes. Reporting them twice is a
    retry, not a second event, so `record_milestone` returns the event already
    there instead of conflict. `DEVIATION_REPORTED` and `OUTPUT_UPLOADED` are
    repeatable — a bench may report several problems and upload several
    corrections.
    """

    row_type = LabEventRow
    record_type = LabEvent

    def _order_by(self) -> Any:
        return self.row_type.reported_at

    def record(self, event: LabEvent) -> LabEvent:
        return self.add(event)

    def for_handover(self, handover_id: str) -> list[LabEvent]:
        return self.all(handover_id=handover_id)

    def of_type(self, handover_id: str, event_type: LabEventType) -> LabEvent | None:
        found = [
            event
            for event in self.for_handover(handover_id)
            if event.event_type is event_type
        ]
        return found[-1] if found else None

    def record_milestone(self, event: LabEvent) -> LabEvent:
        """Write a one-per-handover event, or return the one already written."""
        # The insert below is its own statement rather than `add`, so the
        # project-scope guard `add` performs is not on this path unless it is
        # asked for here. A milestone built from a caller-supplied record is
        # exactly where a foreign `project_id` must not be taken at its word.
        self._authorize(event)
        existing = self.of_type(event.handover_id, event.event_type)
        if existing is not None:
            return existing
        statement = (
            pg_insert(LabEventRow)
            .values(**to_row_data(event, LabEventRow))
            .on_conflict_do_nothing()
            .returning(LabEventRow.event_id)
        )
        inserted = self.session.execute(statement).scalar_one_or_none()
        if inserted is None:
            existing = self.of_type(event.handover_id, event.event_type)
            if existing is None:  # pragma: no cover - the conflict was our index
                raise NotFound(
                    f"event {event.event_id} conflicted but no "
                    f"{event.event_type.value} event exists for handover "
                    f"{event.handover_id}"
                )
            return existing
        return event

    def output_recorded(
        self, handover_id: str, artifact_id: str, sha256: str
    ) -> bool:
        """Whether this exact uploaded version already has its event."""
        return any(
            event.raw_output_ref == artifact_id
            and event.raw_output_sha256 == sha256
            for event in self.for_handover(handover_id)
            if event.event_type is LabEventType.OUTPUT_UPLOADED
        )

    def upload_event_for(
        self, handover_id: str, artifact_id: str, sha256: str
    ) -> LabEvent | None:
        """The person-reported upload event for one exact artifact version."""
        for event in reversed(self.for_handover(handover_id)):
            if (
                event.event_type is LabEventType.OUTPUT_UPLOADED
                and event.raw_output_ref == artifact_id
                and event.raw_output_sha256 == sha256
            ):
                return event
        return None


class LabDeviationLinkRepository(ProjectScopedRepository[LabDeviationLink]):
    """The explicit join from a deviation to the person-reported lab event.

    The unique keys in `lab_deviation_links` make the join one-to-one. Writes
    go through `add`, so the project scope is checked here as it is for every
    other record the Gateway produces.
    """

    row_type = LabDeviationLinkRow
    record_type = LabDeviationLink

    def _order_by(self) -> Any:
        return self.row_type.linked_at

    def record(self, link: LabDeviationLink) -> LabDeviationLink:
        return self.add(link)

    def for_deviation(self, deviation_id: str) -> LabDeviationLink | None:
        found = self.all(deviation_id=deviation_id)
        return found[-1] if found else None

    def for_event(self, event_id: str) -> LabDeviationLink | None:
        found = self.all(event_id=event_id)
        return found[-1] if found else None


class LabPackageRepository(ProjectScopedRepository[LabPackageBinding]):
    """The binding between a prepared package and what a bench was told it is.

    Written once per preparation. `ensure_bound` is idempotent through the
    unique key rather than a check-then-insert, because a retried handover and
    a lead confirmation can race the first write.
    """

    row_type = LabPackageBindingRow
    record_type = LabPackageBinding

    def _order_by(self) -> Any:
        return self.row_type.bound_at

    def binding_for(self, preparation_id: str) -> LabPackageBinding | None:
        found = self.all(preparation_id=preparation_id)
        return found[-1] if found else None

    def ensure_bound(self, preparation: PreparationRecord) -> LabPackageBinding:
        existing = self.binding_for(preparation.preparation_id)
        if existing is not None:
            return existing
        binding = binding_from_preparation(preparation)
        self._authorize(binding)
        statement = (
            pg_insert(LabPackageBindingRow)
            .values(**to_row_data(binding, LabPackageBindingRow))
            .on_conflict_do_nothing(
                index_elements=["project_id", "preparation_id"]
            )
            .returning(LabPackageBindingRow.binding_id)
        )
        self.session.execute(statement)
        stored = self.binding_for(preparation.preparation_id)
        if stored is None:  # pragma: no cover - the insert cannot vanish
            raise NotFound(
                f"binding for preparation {preparation.preparation_id} was "
                "neither inserted nor found"
            )
        return stored


class LabLeadConfirmationRepository(
    ProjectScopedRepository[LabLeadConfirmation]
):
    """One lab lead's confirmation per prepared package."""

    row_type = LabLeadConfirmationRow
    record_type = LabLeadConfirmation

    def _order_by(self) -> Any:
        return self.row_type.confirmed_at

    def for_package(self, preparation_id: str) -> LabLeadConfirmation | None:
        found = self.all(preparation_id=preparation_id)
        return found[-1] if found else None

    def record_once(self, confirmation: LabLeadConfirmation) -> LabLeadConfirmation:
        existing = self.for_package(confirmation.preparation_id)
        if existing is not None:
            return existing
        self._authorize(confirmation)
        statement = (
            pg_insert(LabLeadConfirmationRow)
            .values(**to_row_data(confirmation, LabLeadConfirmationRow))
            .on_conflict_do_nothing(
                index_elements=["project_id", "preparation_id"]
            )
            .returning(LabLeadConfirmationRow.confirmation_id)
        )
        self.session.execute(statement)
        stored = self.for_package(confirmation.preparation_id)
        if stored is None:  # pragma: no cover - the insert cannot vanish
            raise NotFound(
                f"confirmation for preparation {confirmation.preparation_id} "
                "was neither inserted nor found"
            )
        return stored


class LabUploadReceiptRepository(ProjectScopedRepository[LabUploadReceipt]):
    """Every upload attempt, including the ones the door refused."""

    row_type = LabUploadReceiptRow
    record_type = LabUploadReceipt

    def _order_by(self) -> Any:
        return self.row_type.uploaded_at

    def record(self, receipt: LabUploadReceipt) -> LabUploadReceipt:
        return self.add(receipt)

    def for_handover(self, handover_id: str) -> list[LabUploadReceipt]:
        return self.all(handover_id=handover_id)


def confirmation_for(
    session: Session, binding: LabPackageBinding
) -> LabLeadConfirmation | None:
    """The confirmation that covers this binding, if a lab lead made one."""
    candidate = LabLeadConfirmationRepository(
        session, binding.project_id
    ).for_package(binding.preparation_id)
    if candidate is not None and candidate.covers(binding):
        return candidate
    return None


def ready_for_real_execution(
    session: Session, binding: LabPackageBinding
) -> bool:
    """Whether the package may be opened at a real bench.

    A binding without a covering lead confirmation is never ready, however
    complete its sample and SOP lists are. That is the rule the task card
    states, and it is expressed as a property of records rather than a mutable
    flag so there is no path that sets it by assertion.
    """
    return confirmation_for(session, binding) is not None


def binding_from_preparation(preparation: PreparationRecord) -> LabPackageBinding:
    """Bind a prepared package to the terms its own manifest recorded.

    Everything is read from the manifest — the document preparation wrote and
    hashed when it built the directory — rather than from the contract, which
    may have moved since. `sample_ids` are the labels the laboratory
    materializer assigns by position (`S-1`, `S-2`, ...); the manifest records
    the samples in that same order, so the labels are reproducible without
    reading files.

    SOP references are the contract's existing references, carried in
    `resource_limits` as `sop_ref` (one reference) or `sop_refs` (a
    comma-separated list). The device is the contract's `instrument` limit.
    Nothing here invents a reference or an instrument.

    Raises:
        ValueError: The preparation manifest does not name its contract, which
            would make the binding unattributable.
    """
    manifest = preparation.manifest
    contract_ref = str(manifest.get("execution_contract_ref") or "")
    if not contract_ref:
        raise ValueError(
            f"preparation {preparation.preparation_id} has no "
            "execution_contract_ref in its manifest, so a package binding "
            "could not say which contract it was built from"
        )
    manifest_version = manifest.get("execution_contract_version")
    if manifest_version != preparation.execution_contract_version:
        raise ValueError(
            f"preparation {preparation.preparation_id} records contract version "
            f"{preparation.execution_contract_version} but its manifest was built "
            f"from version {manifest_version!r}; the package cannot be bound to "
            "both, and choosing one would make the other unattributable"
        )
    resource_request = manifest.get("resource_request")
    limits = resource_request if isinstance(resource_request, dict) else {}
    samples = manifest.get("samples")
    sample_names = samples if isinstance(samples, list) else []
    required = manifest.get("required_outputs")
    required_files = required if isinstance(required, list) else []
    return LabPackageBinding(
        project_id=preparation.project_id,
        node_id=preparation.node_id,
        preparation_id=preparation.preparation_id,
        execution_contract_ref=contract_ref,
        execution_contract_version=preparation.execution_contract_version,
        protocol_version=preparation.execution_contract_version,
        sample_ids=tuple(f"S-{position}" for position in range(1, len(sample_names) + 1)),
        required_raw_files=tuple(str(name) for name in required_files),
        device_identity=str(limits.get("instrument", "")).strip(),
        sop_refs=_sop_refs(limits),
    )


def _sop_refs(limits: dict[str, Any]) -> tuple[str, ...]:
    """The SOP references the contract already carries, verbatim.

    `sop_refs` is treated as a comma- or newline-separated list; `sop_ref` is
    one reference and is not split, because a reference may itself contain a
    comma. Order is the contract's and duplicates are collapsed.
    """
    refs: dict[str, None] = {}
    listed = limits.get("sop_refs", "")
    if isinstance(listed, str):
        for part in listed.replace("\n", ",").split(","):
            trimmed = part.strip()
            if trimmed:
                refs.setdefault(trimmed, None)
    single = limits.get("sop_ref", "")
    if isinstance(single, str) and single.strip():
        refs.setdefault(single.strip(), None)
    return tuple(refs)


def arrived_outputs(
    session: Session, handover: LabHandover
) -> tuple[tuple[str, Artifact], ...]:
    """What has been uploaded against this handover, in the contract's order.

    **This is the read that decides whether a run may finish**, so it is one
    function rather than a paragraph repeated in each caller. Three conditions,
    each doing work:

    - **The provenance** ties the file to *this handover*, which is the whole
      of the scoping: a handover is keyed by the contract version and the
      attempt, so a file filed against one cannot answer another, and an
      earlier run's delivery does not count towards a later one.
    - **The name** is the output the upload claimed, and it has to be a name
      this handover owed — checked again here because this is the read that
      decides a delivery, and a check that only ran at the door would be one
      refactor away from not running at all.
    - **The kind and the person's event** keep a stray mock out of a delivery.
      Simulated bytes never answer a REAL handover: the guard that stops a
      mock's output entering the Evidence Ledger would be worth very little if
      that output could be handed to a real bench as its result. A SIMULATED
      handover is itself a software proof, so its uploads carry `simulated` and
      may answer it — but only when the same artifact also has a person-reported
      `OUTPUT_UPLOADED` event. That event is what makes the file a bench's
      report rather than a mock row somebody wrote beside the handover.

    The contract version is not among them, and that is deliberate rather than
    an omission. It is *recorded* on every upload — `execution_ref` on the
    version, which is where the directive asks for it and where a reader finds
    it — and it does not need to be *compared* here, because the handover it is
    filed against already names the version. Comparing it twice would be a
    second place for the same rule to be written.
    """
    artifacts = ArtifactRepository(session, handover.project_id).all(
        node_id=handover.node_id
    )
    reported = {
        event.raw_output_ref
        for event in LabEventRepository(session, handover.project_id).for_handover(
            handover.handover_id
        )
        if event.event_type is LabEventType.OUTPUT_UPLOADED
        and event.raw_output_ref
    }
    arrived = {
        artifact.name: artifact
        for artifact in artifacts
        if artifact.provenance == handover.upload_provenance
        and handover.answers(artifact.name)
        and (
            not is_simulated(artifact.kind)
            or (
                handover.execution_mode is LabExecutionMode.SIMULATED
                and artifact.artifact_id in reported
            )
        )
    }
    return tuple((name, arrived[name]) for name in handover.arrived(arrived))


def _file_chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            yield chunk


def record_upload(
    session: Session,
    store: ArtifactStore,
    handover: LabHandover,
    *,
    output: str,
    chunks: Iterable[bytes],
    uploaded_by: str,
    filename: str = "",
    media_type: str = "",
    sample_id: str | None = None,
) -> tuple[Artifact, ArtifactVersion]:
    """File an uploaded file as the artifact that answers one output.

    **This is where everything an upload has to record is written**, and it is
    one function rather than a paragraph in a route so that every door that
    receives bytes files them the same way. The artifact carries `project_id`
    and `node_id`; the version carries `created_by` — who uploaded it —
    `created_at`, the media type, and the content hash; and the execution
    reference carries the contract version, spelled `{contract}.v{version}`
    because a bare number would be a version of whatever contract a reader
    happened to be looking at. The handover it answers is the provenance, so
    "which attempt's deliverable is this" is a field rather than a filename.

    **One artifact per `(handover, output)`, with repeats accumulating as
    versions.** A bench that corrects a file it already sent is doing the
    ordinary thing, and the second upload is version two of the same output
    rather than a second output that happens to share a name — the earlier
    bytes stay readable, which is what makes "it was corrected" legible instead
    of erased. Byte-identical bytes uploaded twice file once: the version that
    is already there is returned, so a retried upload is not a new version, and
    the same reasoning as `SlurmComputeBackend._already_collected`.

    `output` is not checked against the handover here. That check belongs to
    the door that receives the upload, where the answer can be a refusal with a
    message naming what the contract does require, and repeating it here would
    be a second place for it to be written and one place for it to be wrong.

    **An accepted file is also a person-reported event.** The `OUTPUT_UPLOADED`
    row carries the uploader, the time, the artifact and the version's digest,
    so the raw-data chain is traceable from the event to the bytes even after
    later versions of the artifact exist. It is written only once for a given
    `(artifact, digest)`: a retried upload returns the version that is already
    there, and writing a second event for the same bytes would turn a retry
    into a second delivery. The event is not a scientific verdict; that remains
    Review's, against criteria frozen before the run.

    Raises:
        ArtifactStoreError: The object store refused the write.
    """
    artifacts = ArtifactRepository(session, handover.project_id, store)
    existing = next(
        (
            artifact
            for artifact in artifacts.all(
                node_id=handover.node_id, name=output
            )
            if artifact.provenance == handover.upload_provenance
        ),
        None,
    )
    # A bench upload is streamed, not listed: the route already bounded it, but
    # a list of chunks would still hold the whole file in the Gateway's memory.
    # Spool once, hash by re-reading, then hand a fresh iterator to the store.
    temporary = tempfile.NamedTemporaryFile(  # noqa: SIM115
        mode="w+b", prefix="ravel-lab-repository-", suffix=".body", delete=False
    )
    path = Path(temporary.name)
    try:
        for chunk in chunks:
            temporary.write(chunk)
        temporary.flush()
        temporary.close()
        digest, _size = hash_chunks(_file_chunks(path))
        note = f"uploaded against handover {handover.handover_id}"
        if existing is None:
            artifact, version = artifacts.register(
                name=output,
                chunks=_file_chunks(path),
                created_by=uploaded_by,
                filename=filename,
                media_type=media_type,
                kind=(
                    SIMULATED_KIND
                    if handover.execution_mode is LabExecutionMode.SIMULATED
                    else ""
                ),
                provenance=handover.upload_provenance,
                node_id=handover.node_id,
                execution_ref=handover.execution_ref,
                note=note,
            )
        else:
            versions = artifacts.versions(existing.artifact_id)
            if versions and versions[-1].content_hash == digest:
                artifact, version = existing, versions[-1]
            else:
                artifact = existing
                version = artifacts.add_version(
                    existing.artifact_id,
                    chunks=_file_chunks(path),
                    created_by=uploaded_by,
                    filename=filename,
                    media_type=media_type,
                    node_id=handover.node_id,
                    execution_ref=handover.execution_ref,
                    note=note,
                )
        events = LabEventRepository(session, handover.project_id)
        if not events.output_recorded(
            handover.handover_id, artifact.artifact_id, version.sha256
        ):
            events.record(
                LabEvent(
                    project_id=handover.project_id,
                    handover_id=handover.handover_id,
                    actor_user_id=uploaded_by,
                    protocol_version=handover.execution_contract_version,
                    event_type=LabEventType.OUTPUT_UPLOADED,
                    reported_at=version.created_at,
                    sample_id=sample_id,
                    lead_confirmation_ref=handover.lead_confirmation_ref or None,
                    raw_output_ref=artifact.artifact_id,
                    raw_output_sha256=version.sha256,
                )
            )
        return artifact, version
    finally:
        temporary.close()
        path.unlink(missing_ok=True)


def _same_work(first: LabHandover, second: LabHandover) -> bool:
    """Whether two handovers for one attempt describe the same work.

    The terms a bench was given, compared rather than trusted: a second
    submission naming a different contract version is two pieces of work
    proposed for one attempt, which is a bug rather than a retry.
    """
    return (
        first.execution_contract_ref == second.execution_contract_ref
        and first.execution_contract_version == second.execution_contract_version
        and first.backend == second.backend
    )


__all__ = [
    "LabEventRepository",
    "LabHandoverRepository",
    "LabLeadConfirmationRepository",
    "LabPackageRepository",
    "LabUploadReceiptRepository",
    "arrived_outputs",
    "binding_from_preparation",
    "confirmation_for",
    "ready_for_real_execution",
    "record_upload",
]
