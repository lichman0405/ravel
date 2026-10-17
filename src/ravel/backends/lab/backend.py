"""The human laboratory backend: real work, done by a person at a bench.

Phase 11's other real backend runs work on a cluster. This one hands work to
somebody, and almost nothing about the port changes: `submit`, `status`,
`deliver`, `collect`, `cancel`, exactly as `WorkBackend` says. What changes is
what each call has to get right, because the thing on the other side is not a
machine that can be queried. RAVEL cannot see the bench, cannot tell whether
anybody is standing at it, and cannot make it stop.

**A job here is never `RUNNING`.** A cluster reports queues, starts and
wall-clock limits; a person reports nothing until they are finished. A backend
that answered `RUNNING` while it waited would be inventing an observation, and
it is the specific invention the directive names: waiting for a human is
`WAITING_EXTERNAL`, and a state that means "somebody is probably working on it"
is a long-running lie. So `submit` returns `WAITING_EXTERNAL` and there is no
code path here that can return anything else before the work is over.

**The handover is the durable record.** A Slurm job is found again by reading
`submission.json` out of the directory it ran in; a bench has nowhere to upload
a note to, so this backend's note is a row — see `ravel.domain.lab`. The port
hands `status`, `deliver`, `collect` and `cancel` a reference and nothing else,
so without it a backend restarted mid-experiment would have to reassemble its
own work from the job row, the contract, the package and the artifacts, all of
which are free to move while the bench works.

**What arrived is recorded as artifacts, and the check reads them.** A lab user
uploads a file against a named output; the file becomes an artifact version
whose columns carry the project, the node, the author, the time, the media type
and the hash, and whose `execution_ref` carries the contract version it answers.
`deliver` then compares the *recorded* names against `required_outputs` — never
against what a delivery claims to have brought. A delivery that says it brought
everything is a signal, and a signal is a claim; the artifacts are the fact. So
an arbitrary file cannot complete a run, and neither can an assertion.

**Nothing here decides anything scientific, and nothing here decides anything
about authority.** A deviation a lab user reports is carried up unchanged — the
adjudication is the Worker's, against the frozen contract, in
`_stop_for_deviation`. This backend reports what happened and stops.
"""

from __future__ import annotations

from dataclasses import dataclass

from ravel.domain.artifacts import Artifact
from ravel.domain.enums import JobState, NodeType
from ravel.domain.lab import LabHandover
from ravel.domain.preparation import PreparationOutcome, PreparationRecord
from ravel.execution.backends import (
    DeviationReport,
    ExternalDelivery,
    JobHandle,
    JobOutputs,
    JobRequest,
    JobStatus,
)
from ravel.state.database import Database
from ravel.state.mapping import from_row
from ravel.state.repositories.base import NotFound
from ravel.state.repositories.lab import LabHandoverRepository, arrived_outputs
from ravel.state.repositories.preparations import PreparationRepository
from ravel.state.repositories.records import DeviationRepository
from ravel.state.tables import LabHandoverRow

#: How this backend is named in `ExecutionRecord.backend` and in every
#: `BACKEND_STATUS_CHANGED` event. The word "human" is in it because a record
#: that came from a person has to be tellable apart from one a machine
#: reported — the same reason a mock says so in its name.
BACKEND_NAME = "human-lab"

#: What this backend says when it has nothing else to report. A bench's own
#: word for its state, kept beside RAVEL's in `JobStatus.backend_state`: RAVEL
#: says `WAITING_EXTERNAL`, the bench's word is this, and a reader who sees
#: them next to each other does not have to guess which is which.
BACKEND_WORD = "with-the-bench"


class LabConfigurationError(RuntimeError):
    """A laboratory run was handed to this backend with nothing to hand over.

    Raised rather than papered over: a job with no prepared package has no
    protocol for a person to follow and no manifest naming what it owes, and a
    backend that invented a directory to work in would be inventing the
    experiment. The contract names an environment, preparation builds it, and
    this backend's whole job is to pass it to somebody.
    """


@dataclass
class HumanLabBackend:
    """The `WorkBackend` port, against a person at a bench.

    Holds a database and nothing else. No object store: the bytes are stored by
    whoever receives the upload, and this backend reads the records — which is
    what makes it safe to construct from a worker whose only job is to poll.
    """

    database: Database
    name: str = BACKEND_NAME
    #: Which kind of node this backend runs. A property of the backend rather
    #: than of a deployment's mood, and set here rather than passed in so that
    #: a laboratory backend cannot be pointed at a computation node.
    node_type: NodeType = NodeType.EXPERIMENT

    # ── The port ────────────────────────────────────────────────────────────

    def submit(self, request: JobRequest) -> JobHandle:
        """Hand the work over, or return the handover already made.

        Idempotent in `(project_id, node_id, attempt)`, which is the port's
        requirement of every backend and here is PostgreSQL's: the row is keyed
        by the three, and a retried submission finds the handover it already
        made rather than asking the same bench to run the same experiment
        twice.

        The state returned is `WAITING_EXTERNAL` — always, and from the moment
        of the handover. A bench that answered anything else would be reporting
        something it has not observed.

        Raises:
            LabConfigurationError: The request names no prepared package, or
                names one the project does not have. A laboratory run is
                executed by a person reading files, so a run with no files is
                not a run that fails later; it is one that was never handed
                over.
        """
        if not request.workspace_path.strip():
            raise LabConfigurationError(
                "no workspace was prepared for this run, so there is no protocol "
                "to hand a bench; a contract that names a laboratory environment "
                "has to be materialized before it can run"
            )
        prepared = self._package(request)
        handover = LabHandover(
            project_id=request.project_id,
            node_id=request.node_id,
            attempt=request.attempt,
            backend=self.name,
            execution_contract_ref=request.execution_contract_ref,
            execution_contract_version=request.execution_contract_version,
            preparation_id=prepared.preparation_id,
            workspace_path=request.workspace_path,
            protocol=request.entrypoint,
            required_outputs=request.required_outputs,
            detail=(
                f"handed to a bench as {prepared.materializer} "
                f"v{prepared.materializer_version}"
            ),
        )
        with self.database.transaction() as session:
            stored = LabHandoverRepository(session, request.project_id).hand_over(handover)
        return JobHandle(
            backend_job_ref=self.reference_for(stored),
            state=stored.state,
            backend_state=BACKEND_WORD,
        )

    def status(self, backend_job_ref: str) -> JobStatus:
        """Report what is known, which is less than a cluster would report.

        Three answers and no fourth. A handover that has ended is reported as
        it ended; one still open is `WAITING_EXTERNAL` with the outputs that
        have arrived and the ones still owed; and a handover that is not there
        is an error rather than a state, because a reference this backend
        issued and cannot resolve means the record it was issued against is
        gone.

        What it deliberately does not report is progress *by the bench*. RAVEL
        has no way to know how far along a person is, and a percentage would be
        a number RAVEL made up.

        Raises:
            LabConfigurationError: The reference names no handover in this
                project.
        """
        handover = self._handover(backend_job_ref)
        if handover.is_terminal:
            return JobStatus(
                state=handover.state,
                backend_state=BACKEND_WORD,
                detail=handover.detail,
                progress=self._progress(handover),
            )
        return JobStatus(
            state=JobState.WAITING_EXTERNAL,
            backend_state=BACKEND_WORD,
            detail=handover.detail or "with the bench",
            progress=self._progress(handover),
        )

    def deliver(self, backend_job_ref: str, delivery: ExternalDelivery) -> JobStatus:
        """Record that something arrived from outside, and say whether it was enough.

        Two things can be delivered through this door and they are handled in
        this order: **a report**, which stops the work, and **files**, which
        may finish it.

        A delivery completes the handover only when every output the bench owed
        has an artifact recorded against it. The check reads what is *recorded*
        — `delivery.delivered_outputs` is a claim and is not consulted — so a
        partial delivery leaves the run waiting, and the handover's own note
        says what is still owed. That is not a failure: a bench that sends the
        log today and the raw data tomorrow is doing the ordinary thing, and
        the run waits for the second one rather than being completed by the
        first.

        Raises:
            LabConfigurationError: The reference names no handover.
        """
        handover = self._handover(backend_job_ref)
        if handover.is_terminal:
            return JobStatus(
                state=handover.state,
                backend_state=BACKEND_WORD,
                detail=handover.detail,
            )

        reported = self._reported_deviation(handover, delivery)
        if reported is not None:
            # Reported and not adjudicated: whether the contract permits what
            # was asked for is a question about the contract, and the answer
            # belongs to the Worker that holds a frozen copy of it. This
            # backend says what was said and stops — the caller decides.
            return JobStatus(
                state=JobState.WAITING_EXTERNAL,
                backend_state=BACKEND_WORD,
                detail=delivery.summary or "the bench reported something",
                deviation=reported,
            )

        arrived = self._arrived(handover)
        owed = handover.owed(name for name, _artifact in arrived)
        if owed:
            return self._still_owed(handover, owed, delivery)

        with self.database.transaction() as session:
            closed = LabHandoverRepository(session, handover.project_id).close(
                handover.handover_id,
                JobState.COMPLETED,
                detail=self._completed_note(arrived, delivery),
            )
        return JobStatus(
            state=closed.state,
            backend_state=BACKEND_WORD,
            detail=closed.detail,
            progress=self._progress(closed),
        )

    def collect(self, backend_job_ref: str) -> JobOutputs:
        """Hand back what the bench delivered, and what it did not.

        Every artifact that arrived answers the handover by name, so the
        collected set is the handover's own owed list filtered by what is
        there — an extra file that answers nothing is not collected, and there
        is no path by which one could be, because the upload door refuses a
        name the contract never required.

        `delivered_outputs` names outputs rather than artifacts, which is what
        the completeness check compares: one output may arrive as several
        artifacts, or as none.

        Raises:
            LabConfigurationError: The reference names no handover.
        """
        handover = self._handover(backend_job_ref)
        arrived = self._arrived(handover)
        names = tuple(name for name, _artifact in arrived)
        return JobOutputs(
            artifacts=tuple(artifact.artifact_id for _name, artifact in arrived),
            logs=(),
            delivered_outputs=names,
            completion_metadata={
                "backend": self.name,
                "handover_id": handover.handover_id,
                "preparation_id": handover.preparation_id,
                "protocol": handover.protocol,
                "required_outputs": list(handover.required_outputs),
                "missing_outputs": list(handover.owed(names)),
                "uploaded": {
                    name: artifact.created_by for name, artifact in arrived
                },
            },
        )

    def cancel(self, backend_job_ref: str) -> bool:
        """Withdraw the handover, and say what that does and does not mean.

        Returns:
            Whether the withdrawal was recorded. **It is not a claim that the
            bench stopped.** RAVEL cannot stop a person and does not pretend
            to: what this does is end the handover, so that a delivery arriving
            afterwards cannot complete a run that has been given up on, and so
            that the record says RAVEL stopped waiting rather than that the
            bench was interrupted. Something may well still be happening at a
            bench somewhere, and the run will never see it.

            A handover that has already ended is reported as not withdrawn,
            which is the port's ordinary answer for work that is over.

        Raises:
            LabConfigurationError: The reference names no handover.
        """
        handover = self._handover(backend_job_ref)
        if handover.is_terminal:
            return False
        with self.database.transaction() as session:
            LabHandoverRepository(session, handover.project_id).close(
                handover.handover_id,
                JobState.CANCELLED,
                detail=(
                    "RAVEL withdrew this handover and will not accept a delivery "
                    "against it; whether the bench stopped is not something RAVEL "
                    "can see"
                ),
            )
        return True

    # ── References ──────────────────────────────────────────────────────────

    def reference_for(self, handover: LabHandover) -> str:
        """The reference one handover is known by, whoever asks.

        Derived from the handover rather than generated, for the reason the
        mocks and the Slurm backend derive theirs: the same work submitted
        twice — by a retried activity, by a restarted worker — has to be
        recognisably the same work to a backend that never saw the first call.

        Spelled with the identifier rather than hashed from the key, because
        this backend has a database to look it up in and no sixty-four
        character limit to fit inside: `handover_id` is thirty-two characters
        and the prefix brings it to forty-two, which is a `REF` column with
        room to spare. A reference a person can read is a reference a person
        can search for.
        """
        return f"{BACKEND_NAME}:{handover.handover_id}"

    def parse_ref(self, backend_job_ref: str) -> str:
        """The handover a reference names.

        Raises:
            LabConfigurationError: The reference was not issued by this
                backend. Checked here rather than left to fail as a missing
                row, because "no such handover" and "that is a Slurm job
                reference" are different mistakes with different fixes.
        """
        prefix = f"{BACKEND_NAME}:"
        if not backend_job_ref.startswith(prefix):
            raise LabConfigurationError(
                f"{backend_job_ref!r} is not a {BACKEND_NAME} reference; this "
                f"backend's references begin {prefix!r}"
            )
        return backend_job_ref[len(prefix) :]

    # ── Reading the work ────────────────────────────────────────────────────

    def _handover(self, backend_job_ref: str) -> LabHandover:
        """The handover this reference names.

        **The lookup crosses projects, and that is the design rather than a
        hole in it.** The port hands `status`, `deliver`, `collect` and
        `cancel` a reference and nothing else, so a backend bound to one
        project could not answer any of them — and the reference is this
        backend's own, issued in `submit` against a record it wrote itself. So
        the row is read by primary key and the project is taken *from the
        record*, exactly as `SlurmComputeBackend` takes it from the
        `submission.json` it uploaded. Every read after this one is scoped:
        the artifacts, the deviations and every later write go through
        repositories bound to the project the handover names.

        Raises:
            LabConfigurationError: The reference names no handover.
        """
        handover_id = self.parse_ref(backend_job_ref)
        with self.database.read_only() as session:
            row = session.get(LabHandoverRow, handover_id)
            if row is None:
                raise LabConfigurationError(
                    f"no handover {handover_id!r} is recorded, so there is nothing "
                    "for this reference to be about"
                )
            return from_row(LabHandover, row)

    def _package(self, request: JobRequest) -> PreparationRecord:
        """The prepared package this run was built from.

        Read rather than taken from the request, because the request carries
        the workspace path and the entry point but not which preparation wrote
        them: a handover has to name the package it handed over, and the
        newest *prepared* record for this run is that package. A refusal is
        skipped rather than accepted — a run that prepared and was then
        refused would have a refusal as its newest record, and a refusal has
        no files to hand anybody.

        Raises:
            LabConfigurationError: Nothing was ever prepared for this run.
        """
        with self.database.read_only() as session:
            prepared = PreparationRepository(
                session, request.project_id
            ).prepared_for_run(request.node_id, request.execution_contract_version)
        if prepared is None or prepared.outcome is not PreparationOutcome.PREPARED:
            raise LabConfigurationError(
                f"node {request.node_id} names a workspace but no prepared package "
                f"is recorded for contract version "
                f"{request.execution_contract_version}; a bench handed a directory "
                "RAVEL cannot account for would be working to no document"
            )
        return prepared

    def _arrived(self, handover: LabHandover) -> tuple[tuple[str, Artifact], ...]:
        """What has been uploaded against this handover, in the contract's order.

        A session, and the rule itself in `arrived_outputs` — which lives in
        the repository layer because the Gateway serves a lab user the same
        question ("what is still owed") and two answers to it would be two
        answers about whether a run may finish.
        """
        with self.database.read_only() as session:
            return arrived_outputs(session, handover)

    def _progress(self, handover: LabHandover) -> dict[str, object]:
        """Which outputs are in and which are owed, for a reader of the job row."""
        arrived = tuple(name for name, _artifact in self._arrived(handover))
        return {
            "delivered_outputs": list(arrived),
            "missing_outputs": list(handover.owed(arrived)),
            "required_outputs": list(handover.required_outputs),
        }

    def _reported_deviation(
        self, handover: LabHandover, delivery: ExternalDelivery
    ) -> DeviationReport | None:
        """The lab user's report, if this delivery is carrying one.

        A report travels the way everything else from outside RAVEL travels —
        through the signal that ends the wait — because a handover that is
        waiting is never polled: the workflow is blocked in a durable wait and
        `check_job` is not called until it resumes. So the deviation is
        delivered rather than discovered, and this is where it is turned back
        into the vocabulary the Worker adjudicates in.

        The record itself is the lab user's, written by the route they
        reported through, and its identifier travels with the delivery so that
        the Worker can stop the run *against that record* rather than raising
        a second one for the same sentence. `raised_by` is not copied onto the
        report: who said it is on the deviation, and a report that carried a
        reporter would be a second place for that attribution to be wrong.

        Returns `None` when the delivery carries no report, when the identifier
        names nothing in this project, or when the report has already been
        resolved — none of which is an error. A resolved report arriving late
        is the ordinary result of two paths racing, and the answer to it is
        that there is nothing left to adjudicate.
        """
        deviation_id = delivery.payload.get("deviation_id")
        if not isinstance(deviation_id, str) or not deviation_id:
            return None
        with self.database.read_only() as session:
            try:
                deviation = DeviationRepository(session, handover.project_id).get(
                    deviation_id=deviation_id
                )
            except NotFound:
                return None
        if not deviation.is_open or deviation.node_id != handover.node_id:
            return None
        return DeviationReport(
            requested_action=deviation.requested_action,
            description=deviation.description,
            deviation_id=deviation.deviation_id,
        )

    # ── Writing the work down ───────────────────────────────────────────────

    def _still_owed(
        self,
        handover: LabHandover,
        owed: tuple[str, ...],
        delivery: ExternalDelivery,
    ) -> JobStatus:
        """Say what is still missing, and keep waiting for it.

        The note is written to the *handover* rather than left to the job row,
        because a job re-asserting the state it is already in writes nothing —
        so a partial delivery would otherwise leave no trace at all, and "the
        bench sent the log and is owed for the raw data" is exactly the thing
        a person reading this run needs to see.

        The state returned is `WAITING_EXTERNAL`, which the workflow reads as
        "still waiting": the wait re-enters and the next delivery is what ends
        it. Nothing here completes a run early.
        """
        note = (
            f"{delivery.summary or 'a delivery arrived'}; still owed: "
            f"{', '.join(owed)}"
        )
        with self.database.transaction() as session:
            noted = LabHandoverRepository(session, handover.project_id).note(
                handover.handover_id, note
            )
        return JobStatus(
            state=JobState.WAITING_EXTERNAL,
            backend_state=BACKEND_WORD,
            detail=noted.detail,
            progress=self._progress(noted),
        )

    def _completed_note(
        self,
        arrived: tuple[tuple[str, Artifact], ...],
        delivery: ExternalDelivery,
    ) -> str:
        """What the record says when the bench has delivered everything it owed."""
        brought = ", ".join(name for name, _artifact in arrived)
        said = delivery.summary or "the bench delivered"
        return f"{said}; everything this handover owed arrived: {brought}"


