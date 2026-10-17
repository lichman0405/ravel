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

from collections.abc import Iterable
from typing import Any

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ravel.domain.artifacts import Artifact, ArtifactVersion, is_simulated
from ravel.domain.enums import JobState
from ravel.domain.lab import LabHandover
from ravel.state.repositories.base import NotFound, ProjectScopedRepository
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import ArtifactStore, hash_chunks
from ravel.state.tables import LabHandoverRow


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
    - **The kind** keeps simulated bytes out: the guard that stops a mock's
      output entering the Evidence Ledger would be worth very little if a
      mock's output could be handed to a real backend as a bench's delivery.

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
    arrived = {
        artifact.name: artifact
        for artifact in artifacts
        if artifact.provenance == handover.upload_provenance
        and handover.answers(artifact.name)
        and not is_simulated(artifact.kind)
    }
    return tuple((name, arrived[name]) for name in handover.arrived(arrived))


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
    body = list(chunks)
    note = f"uploaded against handover {handover.handover_id}"
    if existing is None:
        return artifacts.register(
            name=output,
            chunks=body,
            created_by=uploaded_by,
            filename=filename,
            media_type=media_type,
            provenance=handover.upload_provenance,
            node_id=handover.node_id,
            execution_ref=handover.execution_ref,
            note=note,
        )
    versions = artifacts.versions(existing.artifact_id)
    if versions and versions[-1].content_hash == hash_chunks(body)[0]:
        return existing, versions[-1]
    added = artifacts.add_version(
        existing.artifact_id,
        chunks=body,
        created_by=uploaded_by,
        filename=filename,
        media_type=media_type,
        node_id=handover.node_id,
        execution_ref=handover.execution_ref,
        note=note,
    )
    return existing, added


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


__all__ = ["LabHandoverRepository", "arrived_outputs", "record_upload"]
