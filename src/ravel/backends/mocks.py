"""Mock compute and laboratory backends.

These exist so that a RAVEL project can be run end to end on one machine. They
are mock in exactly one sense — no computation happens and no experiment runs —
and real in every other. They write real artifacts into the object store, they
leave real rows behind, and every artifact they produce carries
`SIMULATED_KIND`, which is what stops the rest of the system from mistaking it
for a measurement.

That last property is the one that matters. `acceptance/V0_ACCEPTANCE.md`
requires mock compute and lab data to be explicitly marked simulated, and
`docs/06_EXECUTION_AND_REVIEW.md` §6 makes the rule absolute: mock output never
enters the Evidence Ledger as real scientific evidence. A marker is what makes
that rule enforceable, and `EvidenceSourceRepository` is where it is enforced.

**They are driven by the acceptance catalogue.** `MockComputeBackend(scenario=
"COMPUTE_MISSING_OUTPUT")` plays exactly the scenario
`acceptance/MOCK_SCENARIOS.yaml` names, and a scenario the catalogue does not
contain is refused when the backend is constructed rather than when a project
is halfway through a run.

Three properties they deliberately have:

**Idempotent submission, keyed by the work.** The reference a mock returns is
derived from `(project, node, attempt)` rather than generated, so a second
`submit` for the same attempt returns the same reference even if this process
has never seen the first call. `WorkBackend` requires that of every implementor
and L-01 records why: it is the only thing standing between a worker crash and
a duplicated experiment.

**Jobs live in memory.** A mock process that restarts forgets its jobs, and
`status` for an unknown reference is an error rather than a guess. A real
backend stores its own jobs; this one does not, and saying so is better than
pretending otherwise.

**Time is injected.** `clock` is a callable reading seconds, so a test can
drive a scenario's timeline without sleeping through it.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ravel.backends.scenarios import (
    Catalogue,
    ComputeScenario,
    LabScenario,
    catalogue,
)
from ravel.domain.artifacts import SIMULATED_KIND, simulated_provenance
from ravel.domain.enums import FailureClass, JobState, NodeType
from ravel.execution.backends import (
    DeviationReport,
    ExternalDelivery,
    JobHandle,
    JobOutputs,
    JobRequest,
    JobStatus,
)
from ravel.state.database import Database
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import ArtifactStore

#: What each mock calls the state it is in, in its own words. A separate
#: vocabulary from `JobState` on purpose: the two are kept side by side so that
#: a reader can see them disagree, which requires them not being the same word.
_BACKEND_WORD: dict[JobState, str] = {
    JobState.SUBMITTED: "queued",
    JobState.RUNNING: "in-progress",
    JobState.WAITING_EXTERNAL: "awaiting-input",
    JobState.COMPLETED: "done",
    JobState.FAILED: "failed",
    JobState.CANCELLED: "stopped",
    JobState.TIMED_OUT: "expired",
}

#: How long the compute mock holds each state in a scenario's sequence. Long
#: enough that a poll sees RUNNING before COMPLETED — a mock that finished
#: between two polls would make "it ran" something only the record could say.
DEFAULT_STEP_SECONDS = 0.5

#: How much of the work's digest goes into a job reference. See
#: `_MockBackend.reference_for` for why the reference is a digest at all, and why
#: this many characters of one is enough.
REF_DIGEST_CHARS = 16


@dataclass
class _Job:
    """What a mock remembers about work it accepted."""

    ref: str
    request: JobRequest
    scenario_id: str
    accepted_at: float
    state: JobState = JobState.SUBMITTED
    failure_class: FailureClass | None = None
    detail: str = ""
    #: The required outputs the work has handed over so far. A lab that
    #: delivered part of what was asked for leaves the rest absent here, and the
    #: completeness check is what notices.
    delivered: tuple[str, ...] = ()
    #: Set once the outputs have been written, so a retried `collect` returns
    #: the artifacts it already registered rather than storing them twice.
    outputs: JobOutputs | None = None
    cancelled: bool = False
    extra: dict[str, object] = field(default_factory=dict)


class _MockBackend:
    """What both mocks do: remember jobs, mark artifacts, answer idempotently."""

    #: The node type this backend runs. Set by each subclass.
    node_type: NodeType
    name: str

    def __init__(
        self,
        *,
        database: Database,
        store: ArtifactStore,
        scenario: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.store = store
        self.clock = clock
        self._catalogue: Catalogue = catalogue()
        self._jobs: dict[str, _Job] = {}
        self._scenario_id = scenario

    # ── The port ────────────────────────────────────────────────────────────

    def submit(self, request: JobRequest) -> JobHandle:
        """Accept work, or return the work already accepted for this attempt.

        The reference is derived from the work rather than generated, which is
        what makes a second call for the same attempt resume rather than
        restart: it is the same string whether or not this process made the
        first call.
        """
        ref = self.reference_for(request)
        if ref not in self._jobs:
            self._jobs[ref] = self._initial_job(ref, request)
        job = self._jobs[ref]
        return JobHandle(
            backend_job_ref=ref,
            state=job.state,
            backend_state=_BACKEND_WORD[job.state],
        )

    def status(self, backend_job_ref: str) -> JobStatus:
        """Report where the work is, advancing its scenario as time passes."""
        job = self._job(backend_job_ref)
        if not job.state.is_terminal:
            self._advance(job)
        return JobStatus(
            state=job.state,
            backend_state=_BACKEND_WORD[job.state],
            failure_class=job.failure_class,
            detail=job.detail,
            deviation=self._deviation(job),
        )

    def deliver(self, backend_job_ref: str, delivery: ExternalDelivery) -> JobStatus:
        """Tell waiting work that what it waited for has arrived."""
        job = self._job(backend_job_ref)
        if job.state.is_terminal:
            return JobStatus(
                state=job.state, backend_state=_BACKEND_WORD[job.state], detail=job.detail
            )
        # A delivery adds what it carries to what was delivered before, so a
        # lab that sent the log first and the raw data second is recorded as
        # having sent both rather than as having replaced one with the other.
        job.delivered = _union(job.delivered, delivery.delivered_outputs)
        job.state = JobState.COMPLETED
        job.detail = delivery.summary or "the work was answered from outside RAVEL"
        return JobStatus(
            state=job.state, backend_state=_BACKEND_WORD[job.state], detail=job.detail
        )

    def collect(self, backend_job_ref: str) -> JobOutputs:
        """Write the outputs as artifacts and hand back what they are called.

        The bytes are written here rather than as the work runs, because this
        is the call that happens once the work has ended — and each artifact
        carries `SIMULATED_KIND`, which is the whole reason a mock is allowed
        to write anything at all.
        """
        job = self._job(backend_job_ref)
        if job.outputs is not None:
            return job.outputs
        names = job.delivered if job.state is JobState.COMPLETED else ()
        artifacts = tuple(self._write(job, name) for name in names)
        job.outputs = JobOutputs(
            artifacts=artifacts,
            logs=(),
            delivered_outputs=names,
            completion_metadata={
                "simulated": True,
                "scenario": job.scenario_id,
                "backend": self.name,
            },
        )
        return job.outputs

    def cancel(self, backend_job_ref: str) -> bool:
        """Stop the work. Always accepted — there is nothing actually running."""
        job = self._job(backend_job_ref)
        if job.state.is_terminal:
            return False
        job.cancelled = True
        job.state = JobState.CANCELLED
        job.detail = "stopped at RAVEL's request"
        return True

    # ── Shared machinery ────────────────────────────────────────────────────

    def reference_for(self, request: JobRequest) -> str:
        """The reference one piece of work is known by, whoever asks.

        Derived from the work rather than from a counter, so that the same
        attempt submitted twice — by a retried activity, by a restarted worker —
        is recognisably the same work to a mock that never saw the first call.

        Derived by *hashing* the work rather than by spelling it out, because a
        reference is a `REF` column in RAVEL and those hold 64 characters: a
        path through three hex identifiers is seventy-six, and a mock that
        produced one would fail at the write rather than at the submission.
        Sixteen hex characters of a SHA-256 is not a collision risk at any
        number of jobs a V0 project will ever submit, and the work it stands for
        is a row RAVEL already has.
        """
        work = f"{request.project_id}/{request.node_id}/{request.attempt}"
        digest = hashlib.sha256(work.encode("utf-8")).hexdigest()[:REF_DIGEST_CHARS]
        return f"{self.name}/{digest}"

    def _job(self, backend_job_ref: str) -> _Job:
        """The job behind a reference.

        Raises:
            KeyError: This mock never accepted it. Reported rather than
                invented, because a mock that answered for work it does not
                have would be fabricating a result.
        """
        try:
            return self._jobs[backend_job_ref]
        except KeyError:
            raise KeyError(
                f"{self.name} has no job {backend_job_ref!r}; it knows "
                f"{len(self._jobs)} job(s), and a mock forgets them when its "
                "process restarts"
            ) from None

    def _initial_job(self, ref: str, request: JobRequest) -> _Job:
        raise NotImplementedError

    def _advance(self, job: _Job) -> None:
        raise NotImplementedError

    def _deviation(self, job: _Job) -> DeviationReport | None:
        return None

    def _write(self, job: _Job, output: str) -> str:
        """Register one simulated output as an artifact, and return its id."""
        body = _simulated_body(self.name, job.scenario_id, output)
        with self.database.transaction() as session:
            artifact, _ = ArtifactRepository(session, job.request.project_id, self.store).register(
                name=output,
                chunks=[body.encode("utf-8")],
                created_by=self.name,
                filename=output,
                media_type=_media_type(output),
                kind=SIMULATED_KIND,
                provenance=simulated_provenance(self.name, job.scenario_id),
                node_id=job.request.node_id,
                note=(
                    "produced by a mock backend; not a measurement and not "
                    "admissible as evidence"
                ),
            )
        return artifact.artifact_id


class MockComputeBackend(_MockBackend):
    """A compute backend that plays one named scenario from the catalogue.

    The scenario's `sequence` is read as the states one attempt passes through,
    one per `step_seconds`, which is what makes `SUBMITTED, RUNNING, COMPLETED`
    observable rather than instantaneous. A scenario written as a
    first/second-attempt pair is read as what each attempt ends as.
    """

    node_type = NodeType.COMPUTATION
    name = "mock-compute"

    def __init__(
        self,
        *,
        database: Database,
        store: ArtifactStore,
        scenario: str,
        step_seconds: float = DEFAULT_STEP_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(database=database, store=store, scenario=scenario, clock=clock)
        self.scenario: ComputeScenario = self._catalogue.compute_scenario(scenario)
        self.step_seconds = step_seconds

    def _initial_job(self, ref: str, request: JobRequest) -> _Job:
        sequence = self.scenario.sequence_for(request.attempt)
        return _Job(
            ref=ref,
            request=request,
            scenario_id=self.scenario.scenario_id,
            accepted_at=self.clock(),
            state=sequence[0],
            detail=f"{self.name} accepted the work as {self.scenario.scenario_id}",
        )

    def _advance(self, job: _Job) -> None:
        sequence = self.scenario.sequence_for(job.request.attempt)
        elapsed = self.clock() - job.accepted_at
        index = min(int(elapsed / self.step_seconds), len(sequence) - 1)
        state = sequence[index]
        if state is job.state:
            return
        job.state = state
        job.detail = f"{self.scenario.scenario_id} reached {state.value}"
        # A failure class belongs to a failure and to nothing else. Reporting
        # one on a job that is still running, or on one that succeeded, would
        # be read by the retry policy as a reason to run the work again.
        if state in (JobState.FAILED, JobState.TIMED_OUT):
            job.failure_class = self.scenario.failure_class
        if state is JobState.COMPLETED:
            job.delivered = self._delivered(job)

    def _delivered(self, job: _Job) -> tuple[str, ...]:
        """Which required outputs the work hands over.

        The catalogue says whether the delivery is complete but not which
        output is missing, so an incomplete one drops the *last* required
        output. Which one it is comes from the contract rather than from here,
        which keeps the gap a property of the run and not of the mock.
        """
        required = job.request.required_outputs
        if self.scenario.delivers_complete_outputs or not required:
            return required
        return required[:-1]


class MockLabBackend(_MockBackend):
    """A laboratory backend that plays one named scenario from the catalogue.

    A lab takes time to answer, so this mock is driven by the clock rather than
    by a count of polls: after `wait_seconds` it does whatever the scenario says
    it does — answer, report an out-of-contract condition, or wait for
    something outside RAVEL to tell it to continue.
    """

    node_type = NodeType.EXPERIMENT
    name = "mock-lab"

    def __init__(
        self,
        *,
        database: Database,
        store: ArtifactStore,
        scenario: str,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(database=database, store=store, scenario=scenario, clock=clock)
        self.scenario: LabScenario = self._catalogue.lab_scenario(scenario)

    def _initial_job(self, ref: str, request: JobRequest) -> _Job:
        return _Job(
            ref=ref,
            request=request,
            scenario_id=self.scenario.scenario_id,
            accepted_at=self.clock(),
            detail=f"{self.name} accepted the task as {self.scenario.scenario_id}",
        )

    def _advance(self, job: _Job) -> None:
        if job.cancelled:
            return
        if self.clock() - job.accepted_at < self.scenario.wait_seconds:
            return
        if job.state is JobState.SUBMITTED:
            job.state = JobState.RUNNING
            job.detail = f"{self.scenario.scenario_id} is under way"
            return
        if self.scenario.waits_for_signal:
            # The wait is the scenario: the lab answers when something outside
            # RAVEL says so, and not before.
            job.state = JobState.WAITING_EXTERNAL
            job.detail = "the lab is waiting to be told to continue"
            return
        if self.scenario.report is not None:
            # The lab is running and has hit something it will not settle on
            # its own. It keeps running — the report is not an ending — and
            # what happens next is RAVEL's decision.
            job.detail = self.scenario.report.description or self.scenario.scenario_id
            return
        job.state = JobState.COMPLETED
        job.delivered = self._delivered(job)
        job.detail = f"{self.scenario.scenario_id} answered"

    def _deviation(self, job: _Job) -> DeviationReport | None:
        """What the lab reports, once it has had time to reach the question.

        Not reported before the wait has passed: a lab that complained the
        instant it was given the task would be reporting a question it had not
        yet looked at.
        """
        if self.scenario.report is None or job.state.is_terminal:
            return None
        if self.clock() - job.accepted_at < self.scenario.wait_seconds:
            return None
        return self.scenario.report

    def _delivered(self, job: _Job) -> tuple[str, ...]:
        """What the lab sends when it answers.

        A scenario that names a partial delivery sends exactly that. One that
        names none sends everything the contract required — the ordinary case,
        and the one that must not be the special case.
        """
        if self.scenario.delivery:
            return _union(self.scenario.delivered_outputs, job.delivered)
        return _union(job.request.required_outputs, job.delivered)


def _union(first: tuple[str, ...], second: tuple[str, ...]) -> tuple[str, ...]:
    """Both tuples' names, each once, in the order they were first seen."""
    seen: dict[str, None] = {}
    for name in (*first, *second):
        seen.setdefault(name, None)
    return tuple(seen)


def _media_type(filename: str) -> str:
    """A media type from the extension, for the two a mock produces."""
    if filename.endswith(".csv"):
        return "text/csv"
    if filename.endswith(".json"):
        return "application/json"
    return "text/plain"


def _simulated_body(backend: str, scenario: str, output: str) -> str:
    """The bytes a simulated output contains.

    Deliberately unmistakable. The first line says what the file is, so a
    person who opens one — or a program that reads the first line — cannot
    mistake it for a measurement, whatever the filename suggests. Numbers that
    looked plausible would be the more dangerous thing to write.
    """
    return (
        "simulated,not a measurement\n"
        f"output,{output}\n"
        f"backend,{backend}\n"
        f"scenario,{scenario}\n"
        'note,"produced by a mock backend so that a RAVEL project can be run '
        'end to end without a computing cluster or a laboratory"\n'
    )
