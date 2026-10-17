"""The laboratory backend, against a real database and a real object store.

`tests/unit/backends/lab/` covers what this backend decides with nothing around
it. What cannot be covered there is the half that *writes*: a handover is a row,
an upload is an artifact version in a real bucket, and "a run may finish only
when every owed output has been recorded" is a claim about a query over both. So
this file drives the real `HumanLabBackend` through its own port — no Temporal,
no workflow — with a real prepared package behind it.

**The package is built by the real preparation path**, which is what
`tests/support/lab.py` exists to arrange; its docstring says why a hand-written
directory would not do. What that module builds is shared with the Gateway
suite, so the package the routes serve a lab user and the package the backend
waits on are built by the same code path.

Four claims this file exists to make, and each is one a double could not make:

1. **A bench is never `RUNNING`** — not as a convention, but three times over:
   the backend has no path that returns it, the record refuses to be built in
   it, and the database refuses to store it.
2. **A delivery's *claim* does not finish a run.** A signal that says it brought
   everything, with nothing recorded, leaves the handover waiting with the
   outputs still owed — and so does a file that answers a name the handover
   never owed, or one a mock produced.
3. **A report is carried, not adjudicated.** A lab user's deviation reaches this
   backend through the delivery and comes back out unchanged. Whether the
   contract permits what was asked is the Worker's question, and this backend
   neither answers it nor moves the handover over it.
4. **Withdrawing a handover is RAVEL giving up, and says so.** `cancel` ends the
   handover so that a later delivery cannot complete it, and claims nothing
   about whether the person stopped working.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.integration.conftest import Prepared
from tests.support.lab import (
    CONDITIONS,
    LIMITS,
    LOG_BYTES,
    OUTPUTS,
    PROCEDURE,
    RAW_BYTES,
    REQUIREMENTS,
    SAMPLES,
    UPLOADER,
    Handed,
    hand_over,
    raise_a_deviation,
    terms,
    upload,
)

from ravel.backends.lab import (
    BACKEND_NAME,
    BACKEND_WORD,
    HumanLabBackend,
    LabConfigurationError,
)
from ravel.config import Settings
from ravel.domain.artifacts import SIMULATED_KIND
from ravel.domain.enums import JobState, NodeType
from ravel.domain.lab import UPLOAD_PROVENANCE, LabHandover
from ravel.execution.backends import ExternalDelivery, JobRequest
from ravel.state.database import Database
from ravel.state.repositories.lab import (
    LabHandoverRepository,
    arrived_outputs,
)
from ravel.state.repositories.records import DeviationRepository, RecordRepositories
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import S3ArtifactStore, hash_chunks

pytestmark = [pytest.mark.integration]


@pytest.fixture
def bench(database: Database) -> HumanLabBackend:
    """The backend under test, against the real database."""
    return HumanLabBackend(database=database)


@pytest.fixture
def handed_over(
    database: Database,
    integration_settings: Settings,
    artifact_store: S3ArtifactStore,
    bench: HumanLabBackend,
    prepare: Callable[..., Prepared],
) -> Callable[..., Awaitable[Handed]]:
    """Build a package, begin the run, and hand it to the bench.

    `terms()` states the contract — procedure, samples, conditions, outputs —
    and `hand_over` runs the production path over it. Overrides let a test
    change one term without restating the rest.
    """

    async def make(**overrides: object) -> Handed:
        prepared = prepare(**terms(**overrides))
        return await hand_over(
            database=database,
            settings=integration_settings,
            store=artifact_store,
            bench=bench,
            prepared=prepared,
        )

    return make


# ── The handover ────────────────────────────────────────────────────────────


async def test_the_handover_records_what_the_bench_was_given(
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """Which work, under which terms, out of which package, owing what.

    All four are columns rather than sentences, and that is the point: a
    handover assembled out of the running job row, the contract and the newest
    preparation would answer differently after any of them moved, and what the
    bench was handed would no longer be reconstructable.
    """
    handed = await handed_over()
    handover = handed.handover

    assert handover.execution_contract_ref == handed.prepared.contract.contract_id
    assert handover.execution_contract_version == handed.prepared.contract.version
    assert handover.preparation_id == handed.report.preparation_id
    assert handover.workspace_path == handed.report.workspace_path
    assert handover.required_outputs == OUTPUTS
    assert handover.attempt == 1
    assert handover.backend == BACKEND_NAME

    # The protocol is named rather than assumed: a materializer decides what a
    # package starts from, and the handover is what tells the bench which file
    # that is.
    assert handover.protocol == handed.report.execution_metadata["entrypoint"]
    assert (handed.workspace / handover.protocol).is_file()

    # And the package a person reads is the one the contract described.
    protocol = (handed.workspace / handover.protocol).read_text()
    assert PROCEDURE in protocol
    assert OUTPUTS[0] in protocol
    assert SAMPLES[0] in protocol


async def test_a_bench_is_never_reported_as_running(
    database: Database, bench: HumanLabBackend, handed_over: Callable[..., Awaitable[Handed]]
) -> None:
    """The directive's rule, asserted at all three levels it holds at.

    A backend that answered `RUNNING` while it waited would be reporting
    something it has not observed — RAVEL cannot see a person working — and a
    long `RUNNING` is exactly the disguise a wait for a human must not wear.
    Three layers are checked because code changes: the backend's own answer, the
    record's refusal to exist in that state, and the database's constraint,
    which is what a later refactor would have to get past.
    """
    handed = await handed_over()

    status = bench.status(handed.ref)
    assert status.state is JobState.WAITING_EXTERNAL
    assert status.backend_state == BACKEND_WORD
    assert status.progress["missing_outputs"] == list(OUTPUTS)
    assert status.progress["delivered_outputs"] == []

    with pytest.raises(ValueError, match="WAITING_EXTERNAL"):
        LabHandover.model_validate({**handed.handover.model_dump(), "state": "RUNNING"})

    with pytest.raises(IntegrityError) as refused, database.transaction() as session:
        session.execute(
            text("UPDATE lab_handovers SET state = 'RUNNING' WHERE handover_id = :h"),
            {"h": handed.handover.handover_id},
        )
    # Which of the two rules refuses this depends on the order PostgreSQL
    # evaluates them in, and both are rules about this table: `RUNNING` is not
    # a state a handover may be in, and it cannot be a closing state either.
    # What the assertion pins is that a *check on this table* refused — not a
    # foreign key, not the identity guard, not something else entirely that
    # happened to go wrong at the same moment.
    assert "ck_lab_handovers_" in str(refused.value), (
        "the database refused the write, but not for the reason this test is "
        f"about: {refused.value}"
    )


async def test_handing_the_same_attempt_over_twice_hands_it_over_once(
    database: Database, bench: HumanLabBackend, handed_over: Callable[..., Awaitable[Handed]]
) -> None:
    """The port's idempotence, which here is PostgreSQL's.

    A worker killed between the backend accepting the work and the job reference
    being recorded is retried, and the retry has to find the handover it already
    made rather than asking the same bench to run the same experiment twice. A
    second *attempt* is different work and does get its own handover — which is
    what the second half asserts, since an idempotence that deduplicated
    everything would pass the first half.
    """
    handed = await handed_over()

    again = bench.submit(handed.request)
    assert again.backend_job_ref == handed.ref

    with database.read_only() as session:
        recorded = LabHandoverRepository(
            session, handed.prepared.project_id
        ).for_node(handed.node_id)
    assert [handover.handover_id for handover in recorded] == [
        handed.handover.handover_id
    ], "one attempt was handed over twice"

    retry = bench.submit(replace(handed.request, attempt=2, is_retry=True))
    assert retry.backend_job_ref != handed.ref
    with database.read_only() as session:
        attempts = LabHandoverRepository(session, handed.prepared.project_id).for_node(
            handed.node_id
        )
    assert sorted(handover.attempt for handover in attempts) == [1, 2]


# ── What completes a run ────────────────────────────────────────────────────


async def test_a_delivery_that_claims_what_it_never_uploaded_cannot_finish_the_run(
    database: Database,
    artifact_store: S3ArtifactStore,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """A signal is a claim and the artifacts are the fact.

    Two shapes of one mistake: a delivery that says it brought everything when
    nothing was uploaded, and one that arrives with only the first of the two
    files recorded. Neither finishes the run, and the handover's own note says
    what is still owed — which is not a failure. A bench that sends the log
    today and the raw data tomorrow is doing the ordinary thing.
    """
    handed = await handed_over()

    claimed = bench.deliver(
        handed.ref,
        ExternalDelivery(
            summary="Everything the contract asked for.",
            delivered_outputs=OUTPUTS,
        ),
    )
    assert claimed.state is JobState.WAITING_EXTERNAL, (
        "a delivery completed a run by claiming outputs that were never recorded"
    )
    assert handed.read_back(database).is_waiting

    upload(database, artifact_store, handed.handover, "experiment_log", LOG_BYTES)
    partial = bench.deliver(
        handed.ref,
        ExternalDelivery(summary="The log, so far.", delivered_outputs=OUTPUTS),
    )
    assert partial.state is JobState.WAITING_EXTERNAL
    assert partial.progress["delivered_outputs"] == ["experiment_log"]
    assert partial.progress["missing_outputs"] == ["raw_data"]

    still_waiting = handed.read_back(database)
    assert still_waiting.is_waiting
    assert "raw_data" in still_waiting.detail, (
        "a partial delivery left no trace: a job re-asserting the state it is "
        "already in writes nothing, so the note is the only place this can live"
    )


async def test_the_run_finishes_when_every_owed_output_has_been_recorded(
    database: Database,
    artifact_store: S3ArtifactStore,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """The other half, and what the run collected.

    `collect` reads the handover's own owed list filtered by what arrived, so
    the outputs it names and the artifacts it hands back are the same set — one
    per name the contract required, each with the person who uploaded it.
    """
    handed = await handed_over()
    log, _ = upload(database, artifact_store, handed.handover, "experiment_log", LOG_BYTES)
    raw, _ = upload(database, artifact_store, handed.handover, "raw_data", RAW_BYTES)

    delivered = bench.deliver(
        handed.ref,
        ExternalDelivery(summary="The bench finished.", delivered_outputs=OUTPUTS),
    )
    assert delivered.state is JobState.COMPLETED

    closed = handed.read_back(database)
    assert closed.state is JobState.COMPLETED
    assert closed.closed_at is not None
    assert "experiment_log" in closed.detail and "raw_data" in closed.detail

    outputs = bench.collect(handed.ref)
    assert outputs.delivered_outputs == OUTPUTS
    assert set(outputs.artifacts) == {log.artifact_id, raw.artifact_id}
    assert outputs.completion_metadata["missing_outputs"] == []
    assert outputs.completion_metadata["handover_id"] == handed.handover.handover_id
    assert outputs.completion_metadata["uploaded"] == {
        "experiment_log": UPLOADER,
        "raw_data": UPLOADER,
    }

    # A delivery arriving after the run is over reports where it ended, rather
    # than completing it a second time or reopening it.
    late = bench.deliver(
        handed.ref,
        ExternalDelivery(summary="One more thing.", delivered_outputs=OUTPUTS),
    )
    assert late.state is JobState.COMPLETED
    assert handed.read_back(database).closed_at == closed.closed_at


async def test_a_simulated_artifact_cannot_answer_an_output(
    database: Database,
    artifact_store: S3ArtifactStore,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """What a mock produced cannot be handed in as a bench's delivery.

    The Evidence Ledger refuses simulated bytes for the same reason, and this is
    the second door. A mock's output filed against a handover — by a bug, by a
    confused caller, by anything — must not satisfy a real backend's
    completeness check, or a run would reach Review with a result no bench ever
    produced.
    """
    handed = await handed_over()
    log, _ = upload(database, artifact_store, handed.handover, "experiment_log", LOG_BYTES)
    raw, _ = upload(database, artifact_store, handed.handover, "raw_data", RAW_BYTES)

    with database.transaction() as session:
        simulated, _version = ArtifactRepository(
            session, handed.prepared.project_id, artifact_store
        ).register(
            name="experiment_log",
            chunks=[b"sample,conductivity\nbatch-17,9.9\n"],
            created_by="mock-lab",
            provenance=handed.handover.upload_provenance,
            kind=SIMULATED_KIND,
            node_id=handed.node_id,
        )
    assert simulated.artifact_id not in {log.artifact_id, raw.artifact_id}

    with database.read_only() as session:
        arrived = arrived_outputs(session, handed.handover)
    assert [name for name, _artifact in arrived] == list(OUTPUTS), (
        "the simulated artifact displaced a real one, or added a third output"
    )
    assert {artifact.artifact_id for _name, artifact in arrived} == {
        log.artifact_id,
        raw.artifact_id,
    }

    delivered = bench.deliver(
        handed.ref, ExternalDelivery(summary="Done.", delivered_outputs=OUTPUTS)
    )
    assert delivered.state is JobState.COMPLETED, (
        "the simulated artifact was counted as the bench's own delivery"
    )


async def test_an_upload_is_recorded_with_who_what_when_and_against_which_contract(
    database: Database,
    artifact_store: S3ArtifactStore,
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """Every column an upload has to record, and two repeats of one.

    The artifact carries the project and the node; the version carries the
    uploader, the time, the media type and the hash; and the execution reference
    carries the contract *and* the version, because a bare number would be a
    version of whatever contract a reader happened to be looking at.

    The two repeats are the ordinary life of a bench file: byte-identical bytes
    uploaded twice are one version rather than two, and a corrected file is
    version two while version one stays readable. What a correction must not do
    is erase what was there, since that is what a decision may have cited.
    """
    handed = await handed_over()
    artifact, version = upload(
        database,
        artifact_store,
        handed.handover,
        "experiment_log",
        LOG_BYTES,
        filename="run-17-log.csv",
        media_type="text/csv",
    )

    assert artifact.project_id == handed.prepared.project_id
    assert artifact.node_id == handed.node_id
    assert artifact.provenance == f"{UPLOAD_PROVENANCE}:{handed.handover.handover_id}"

    assert version.created_by == UPLOADER
    assert version.created_at is not None
    assert version.media_type == "text/csv"
    assert version.filename == "run-17-log.csv"
    assert version.content_hash == hash_chunks([LOG_BYTES])[0]
    assert version.node_id == handed.node_id
    assert version.execution_ref == (
        f"{handed.prepared.contract.contract_id}.v{handed.prepared.contract.version}"
    )

    again, same = upload(
        database, artifact_store, handed.handover, "experiment_log", LOG_BYTES
    )
    assert again.artifact_id == artifact.artifact_id
    assert same.version == version.version, (
        "identical bytes uploaded twice filed a second version, so a retried "
        "upload would be recorded as a correction"
    )

    corrected = b"sample,conductivity\nbatch-17,1.4\nbatch-18,2.0\n"
    same_artifact, second = upload(
        database, artifact_store, handed.handover, "experiment_log", corrected
    )
    assert same_artifact.artifact_id == artifact.artifact_id
    assert second.version == version.version + 1

    with database.read_only() as session:
        versions = ArtifactRepository(
            session, handed.prepared.project_id
        ).versions(artifact.artifact_id)
    assert [item.content_hash for item in versions] == [
        hash_chunks([LOG_BYTES])[0],
        hash_chunks([corrected])[0],
    ], "the bytes that were there first are not readable any more"


# ── The report ──────────────────────────────────────────────────────────────


async def test_a_delivery_can_carry_a_report_and_this_backend_decides_nothing(
    database: Database,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """A report travels the only door there is, and stops at the Worker.

    A handover that is waiting is never polled — the workflow is blocked in a
    durable wait — so the lab user's sentence has to arrive on the delivery that
    ends the wait. What comes back is a report for the Worker to adjudicate
    against the frozen contract; whether the action was permitted is not a
    question this backend asks, and the handover is left exactly where it was:
    still waiting, still owing everything.
    """
    handed = await handed_over()
    deviation = raise_a_deviation(database, handed.prepared)

    status = bench.deliver(
        handed.ref,
        ExternalDelivery(
            summary="The bench cannot do what the plan says.",
            payload={"deviation_id": deviation.deviation_id},
        ),
    )

    assert status.deviation is not None, "the report did not reach the caller"
    assert status.deviation.deviation_id == deviation.deviation_id
    assert status.deviation.requested_action == deviation.requested_action
    assert status.deviation.description == deviation.description
    assert status.state is JobState.WAITING_EXTERNAL, (
        "this backend stopped or completed a run over a question that is not "
        "its to answer"
    )

    unchanged = handed.read_back(database)
    assert unchanged.is_waiting
    assert unchanged.detail == handed.handover.detail, (
        "a report is not a partial delivery; nothing about what is owed changed"
    )

    with database.read_only() as session:
        stored = DeviationRepository(session, handed.prepared.project_id).get(
            deviation_id=deviation.deviation_id
        )
    assert stored.is_open, "the report was resolved by something other than Master"
    assert stored.permitted is False, (
        "a report is never a permission, in the record or anywhere else"
    )


async def test_a_report_nothing_is_waiting_on_is_not_delivered(
    database: Database,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
    prepare: Callable[..., Prepared],
) -> None:
    """Three deliveries that look like reports and are not, and each is read as
    the ordinary signal it also is.

    A report Master has already ruled on is the ordinary result of two paths
    racing, and there is nothing left to adjudicate. A reference that names
    nothing is the same shape of mistake. A report about a *different* node is
    the one worth asserting separately: deviations are scoped by the node they
    are about, so a delivery for this handover cannot be stopped by a question
    raised somewhere else in the project.
    """
    handed = await handed_over()

    answered = raise_a_deviation(database, handed.prepared)
    with database.transaction() as session:
        RecordRepositories(session, handed.prepared.project_id).deviations.resolve(
            answered.deviation_id, decision_ref="dec-answered"
        )
    elsewhere_node = prepare(
        node_type=NodeType.EXPERIMENT,
        required_outputs=OUTPUTS,
        procedure=PROCEDURE,
        inputs=SAMPLES,
        parameter_targets=CONDITIONS,
        resource_limits=LIMITS,
        execution_requirements=REQUIREMENTS,
    )
    elsewhere = raise_a_deviation(database, handed.prepared, node_id=elsewhere_node.node_id)

    for deviation_id in (answered.deviation_id, "no-such-deviation", elsewhere.deviation_id):
        status = bench.deliver(
            handed.ref,
            ExternalDelivery(
                summary="About the pressure.",
                payload={"deviation_id": deviation_id},
            ),
        )
        assert status.deviation is None, (
            f"{deviation_id} was delivered as a report for this handover"
        )
        assert status.state is JobState.WAITING_EXTERNAL


# ── Giving up ───────────────────────────────────────────────────────────────


async def test_withdrawing_a_handover_stops_ravel_waiting_and_claims_nothing_else(
    database: Database,
    artifact_store: S3ArtifactStore,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """`cancel` is not a claim that the person stopped working.

    RAVEL cannot stop a bench and must not say that it did. What withdrawal does
    is end the handover, so that a delivery arriving afterwards cannot complete
    a run that has been given up on — and the ending says out loud that this is
    RAVEL giving up rather than anybody being interrupted.
    """
    handed = await handed_over()
    assert bench.cancel(handed.ref) is True

    withdrawn = handed.read_back(database)
    assert withdrawn.state is JobState.CANCELLED
    assert withdrawn.closed_at is not None
    assert "RAVEL can see" in withdrawn.detail, (
        "the record has to say that RAVEL does not know whether the bench "
        f"stopped: {withdrawn.detail!r}"
    )

    # A bench that works on anyway and delivers everything cannot revive it.
    upload(database, artifact_store, withdrawn, "experiment_log", LOG_BYTES)
    upload(database, artifact_store, withdrawn, "raw_data", RAW_BYTES)
    late = bench.deliver(
        handed.ref,
        ExternalDelivery(summary="Finished after all.", delivered_outputs=OUTPUTS),
    )
    assert late.state is JobState.CANCELLED
    assert handed.read_back(database).state is JobState.CANCELLED

    assert bench.cancel(handed.ref) is False, (
        "a handover that has ended is not withdrawn a second time"
    )


# ── Refusals ────────────────────────────────────────────────────────────────


async def test_a_run_with_no_prepared_package_is_refused(
    database: Database,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
    prepare: Callable[..., Prepared],
) -> None:
    """Two requests that name a workspace, and two reasons there is none.

    A laboratory run is executed by a person reading files. A run with no files
    is not one that fails later; it is one that was never handed over, and a
    backend that invented a directory to work in would be inventing the
    experiment.
    """
    handed = await handed_over()

    with pytest.raises(LabConfigurationError, match="no workspace was prepared"):
        bench.submit(replace(handed.request, workspace_path="", entrypoint=""))

    # A workspace path with no preparation behind it: the request is what a
    # backend is given, and it is not evidence that anything was built.
    unbuilt = prepare(node_type=NodeType.EXPERIMENT, required_outputs=OUTPUTS)
    with pytest.raises(LabConfigurationError, match="no prepared package"):
        bench.submit(
            JobRequest(
                project_id=unbuilt.project_id,
                node_id=unbuilt.node_id,
                attempt=1,
                execution_contract_ref=unbuilt.contract.contract_id,
                execution_contract_version=unbuilt.contract.version,
                objective=unbuilt.contract.objective,
                required_outputs=OUTPUTS,
                workspace_path=str(handed.workspace),
                entrypoint="experimental_protocol.md",
            )
        )

    with database.read_only() as session:
        recorded = LabHandoverRepository(session, unbuilt.project_id).for_node(
            unbuilt.node_id
        )
    assert recorded == [], "a refused submission left a handover behind"


async def test_a_reference_this_backend_did_not_issue_is_refused(
    bench: HumanLabBackend, handed_over: Callable[..., Awaitable[Handed]]
) -> None:
    """Two mistakes with two different fixes, and neither of them is a state.

    A reference issued by another backend means the caller is asking the wrong
    service; one of this backend's own that resolves to nothing means a row is
    gone. Both are errors rather than states, because a job that is not there is
    not a job in a state.
    """
    handed = await handed_over()

    with pytest.raises(LabConfigurationError, match="slurm"):
        bench.status("slurm:12345")
    with pytest.raises(LabConfigurationError, match="no handover"):
        bench.status(f"{BACKEND_NAME}:00000000000000000000000000000000")

    assert bench.status(handed.ref).state is JobState.WAITING_EXTERNAL


async def test_a_node_under_another_contract_version_is_a_different_handover(
    database: Database,
    artifact_store: S3ArtifactStore,
    bench: HumanLabBackend,
    handed_over: Callable[..., Awaitable[Handed]],
) -> None:
    """A revised contract is different work, not a retry of the old one.

    Master may widen the terms while a bench holds the work, and the node then
    runs again under a version of the terms that bench never saw. A handover
    keyed by the node and the attempt alone would make that second run find the
    first handover — a bench being asked to deliver something it was never asked
    for, and an earlier delivery counting towards a later run.
    """
    handed = await handed_over()
    upload(database, artifact_store, handed.handover, "experiment_log", LOG_BYTES)
    upload(database, artifact_store, handed.handover, "raw_data", RAW_BYTES)

    revised = LabHandover(
        project_id=handed.prepared.project_id,
        node_id=handed.node_id,
        attempt=1,
        backend=BACKEND_NAME,
        execution_contract_ref=handed.prepared.contract.contract_id,
        execution_contract_version=handed.prepared.contract.version + 1,
        preparation_id=handed.handover.preparation_id,
        workspace_path=handed.handover.workspace_path,
        protocol=handed.handover.protocol,
        required_outputs=OUTPUTS,
    )
    with database.transaction() as session:
        stored = LabHandoverRepository(session, handed.prepared.project_id).hand_over(
            revised
        )

    assert stored.handover_id != handed.handover.handover_id
    assert bench.status(bench.reference_for(stored)).progress["missing_outputs"] == list(
        OUTPUTS
    ), "files the bench sent under the old terms count towards the new run"
    assert handed.read_back(database).state is JobState.WAITING_EXTERNAL, (
        "handing the same node over under revised terms moved the earlier handover"
    )
