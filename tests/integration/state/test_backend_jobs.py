"""The in-flight job record, against real PostgreSQL.

The property this file exists for is the one the durable layer leans on:
**starting a job is idempotent, and the database is what makes it so.** A
worker that dies between handing work to a backend and recording the result is
retried, and a retry that submitted a second job would run the experiment
twice and pay for it twice. An activity that checked first and inserted second
would have a window; an insert that conflicts does not.

The rest is the same idea applied to the columns that carry meaning. `attempt`
is what identifies the work, so it is guarded; the failure class decides
whether work runs again, so it cannot outlive the failure it explains; a job
that ended ended at a time. Each is asserted by writing the wrong thing
directly, so that what refuses is PostgreSQL rather than the Python model — a
rule only the model enforces is a rule a bug can walk around.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ravel.domain.dag import DagNode
from ravel.domain.enums import FailureClass, JobState, NodeType
from ravel.domain.execution import BackendJob
from ravel.domain.ids import new_id
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole
from ravel.state.database import Database
from ravel.state.repositories.base import ProjectScopeError
from ravel.state.repositories.dag import DagRepository
from ravel.state.repositories.records import BackendJobRepository

pytestmark = pytest.mark.integration


@pytest.fixture
def node(database: Database, project: Project) -> DagNode:
    """A COMPUTATION node for a job to belong to.

    Left in PLANNED rather than moved to RUNNING, because this repository does
    not check a node's status and should not: the activity that begins a run is
    what moves the node, and it does so through `DagRepository.transition_node`
    where the frozen-contract preconditions live. Putting a second copy of that
    rule here would give it two places to be right.
    """
    with database.transaction() as session:
        return DagRepository(session, project.project_id).add_node(
            DagNode.create(
                project_id=project.project_id,
                node_type=NodeType.COMPUTATION,
                objective="Measure conductivity across the dopant series.",
                created_by="master",
            ),
            role=AgentRole.MASTER,
            decision_ref="dec-1",
        )


def _job(project_id: str, node_id: str, **overrides: object) -> BackendJob:
    defaults: dict[str, object] = {
        "project_id": project_id,
        "node_id": node_id,
        "attempt": 1,
        "execution_contract_ref": "ctr-1",
        "execution_contract_version": 1,
        "backend": "mock-compute",
    }
    defaults.update(overrides)
    return BackendJob(**defaults)  # type: ignore[arg-type]


# ── Idempotent start ────────────────────────────────────────────────────────


def test_starting_the_same_attempt_twice_yields_one_job(
    database: Database, project: Project, node: DagNode
) -> None:
    """The retry that must not submit twice.

    The second call is built from scratch, with a new `job_id`, exactly as a
    re-run activity would build it. What it gets back is the first job.
    """
    first = _job(project.project_id, node.node_id)
    with database.transaction() as session:
        stored = BackendJobRepository(session, project.project_id).start(first)
        again = BackendJobRepository(session, project.project_id).start(
            _job(project.project_id, node.node_id)
        )

        assert stored.job_id == first.job_id
        assert again.job_id == first.job_id
        assert again.submitted_at == stored.submitted_at

    with database.read_only() as session:
        count = session.execute(
            text("SELECT count(*) FROM backend_jobs WHERE node_id = :n"),
            {"n": node.node_id},
        ).scalar_one()
    assert count == 1


def test_a_job_cannot_be_started_into_another_project(
    database: Database, project: Project, other_project: Project, node: DagNode
) -> None:
    """The repository's scope, not the record's `project_id`, decides where a row goes.

    `start` writes its own INSERT rather than going through `add`, because the
    insert is the thing that has to conflict. That also means it does not pass
    the guard `add` applies, so it applies the guard itself: without it, a job
    built with a foreign `project_id` would be written into a project the
    caller has no scope over, and `ON CONFLICT DO NOTHING` would then report
    the confusing failure of a row that exists but cannot be found.
    """
    with database.transaction() as session:
        repository = BackendJobRepository(session, other_project.project_id)
        with pytest.raises(ProjectScopeError, match="belongs to project"):
            repository.start(_job(project.project_id, node.node_id))

    with database.read_only() as session:
        count = session.execute(text("SELECT count(*) FROM backend_jobs")).scalar_one()
    assert count == 0


def test_a_second_attempt_is_a_second_job(
    database: Database, project: Project, node: DagNode
) -> None:
    """Retrying scientifically is a new attempt, and the row says so.

    The whole reason the key names the attempt rather than the node alone: an
    infrastructure failure that is retried must leave both jobs visible, so a
    reader can see that the work ran twice and why.
    """
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        first = repository.start(_job(project.project_id, node.node_id))
        second = repository.start(_job(project.project_id, node.node_id, attempt=2))

        assert first.job_id != second.job_id
        assert [job.attempt for job in repository.for_node(node.node_id)] == [1, 2]


def test_a_revised_contracts_run_counts_its_attempts_from_one(
    database: Database, project: Project, node: DagNode
) -> None:
    """The version is part of what an attempt counts.

    Answering a Worker's escalation with a revised contract runs the node
    again, and that run's first attempt is the first attempt of the work the
    new contract describes. Numbering it after the run that raised the question
    would produce an Execution Record the domain refuses — a record holds one
    run's attempts and numbers them from one — so the key has to carry the
    version, and the two jobs coexist rather than colliding.
    """
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        first = repository.start(_job(project.project_id, node.node_id))
        revised = repository.start(
            _job(
                project.project_id,
                node.node_id,
                execution_contract_ref="ctr-2",
                execution_contract_version=2,
            )
        )

        assert first.job_id != revised.job_id
        assert revised.attempt == 1, (
            "the run under the revised terms is the first attempt at that work"
        )
        assert [job.attempt for job in repository.for_node(node.node_id)] == [1, 1]

    with database.read_only() as session:
        stored = BackendJobRepository(session, project.project_id).for_attempt(
            node.node_id, 1, 2
        )
    assert stored is not None and stored.job_id == revised.job_id, (
        "an attempt is named by the node, the attempt number, and the version"
    )


def test_the_same_attempt_cannot_become_different_work(
    database: Database, project: Project, node: DagNode
) -> None:
    """A retry is the same work proposed again, not a different piece of work.

    Returning whichever arrived first would hide a real contradiction: two
    callers asking one attempt to run two different contracts on two different
    backends.
    """
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        repository.start(_job(project.project_id, node.node_id))

        with pytest.raises(ValueError, match="is not the job now being started"):
            repository.start(_job(project.project_id, node.node_id, backend="mock-lab"))


def test_a_retry_within_one_attempt_keeps_the_original_timestamp(
    database: Database, project: Project, node: DagNode
) -> None:
    """The job started when it started, not when the retry happened to run."""
    yesterday = datetime.now(UTC) - timedelta(days=1)
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        repository.start(_job(project.project_id, node.node_id, submitted_at=yesterday))
        again = repository.start(_job(project.project_id, node.node_id))

    assert again.submitted_at == yesterday


def test_starting_a_job_records_it_in_the_project_stream(
    database: Database, project: Project, node: DagNode
) -> None:
    """The stream is how a TUI watches work start without polling the tables."""
    with database.transaction() as session:
        BackendJobRepository(session, project.project_id).start(
            _job(project.project_id, node.node_id)
        )

    events = _backend_events(database, project)

    assert [event["change"] for event in events] == ["STARTED"]
    assert events[0]["attempt"] == 1


def test_a_job_that_names_no_node_is_refused(
    database: Database, project: Project
) -> None:
    """The foreign key, not a convention.

    A job that belongs to no node is work nobody asked for, and it would be
    invisible to everything that reads the DAG.
    """
    with (
        pytest.raises(IntegrityError, match="fk_backend_jobs_node_id_dag_nodes"),
        database.transaction() as session,
    ):
        BackendJobRepository(session, project.project_id).start(
            _job(project.project_id, new_id())
        )


# ── State changes ───────────────────────────────────────────────────────────


def test_a_job_moves_through_its_life_and_ends_once(
    database: Database, project: Project, node: DagNode
) -> None:
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        job = repository.start(_job(project.project_id, node.node_id))
        job = repository.record_state(
            job.job_id, JobState.RUNNING, backend_state="RUNNING", backend_job_ref="slurm-7"
        )
        job = repository.record_state(
            job.job_id, JobState.WAITING_EXTERNAL, backend_state="WAITING"
        )
        job = repository.record_state(job.job_id, JobState.COMPLETED, backend_state="COMPLETED")

    assert job.state is JobState.COMPLETED
    assert job.backend_job_ref == "slurm-7"
    assert job.ended_at is not None


def test_a_repeated_poll_does_not_fill_the_stream(
    database: Database, project: Project, node: DagNode
) -> None:
    """A poll loop reports the same state every few seconds; the stream is not a log."""
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        job = repository.start(_job(project.project_id, node.node_id))
        repository.record_state(job.job_id, JobState.RUNNING, backend_state="RUNNING")
        for _ in range(5):
            repository.record_state(job.job_id, JobState.RUNNING, backend_state="RUNNING")

    assert [event["change"] for event in _backend_events(database, project)] == [
        "STARTED",
        "STATE",
    ]


def test_a_report_that_contradicts_an_ending_is_refused(
    database: Database, project: Project, node: DagNode
) -> None:
    """What a retry policy reads has to be the one thing that happened."""
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        job = repository.start(_job(project.project_id, node.node_id))
        repository.record_state(job.job_id, JobState.COMPLETED)

        with pytest.raises(ValueError, match="contradicts a recorded ending"):
            repository.record_state(
                job.job_id, JobState.FAILED, failure_class=FailureClass.NON_RETRYABLE
            )


def test_the_backend_job_reference_can_be_recorded_without_moving_the_state(
    database: Database, project: Project, node: DagNode
) -> None:
    """A backend that answers with its identifier only on the first poll.

    The state is unchanged, so this is not a transition — but the identifier
    has to be recorded, because it is what a retry uses to find the work again,
    and the moment it was learned is worth being able to find.
    """
    with database.transaction() as session:
        repository = BackendJobRepository(session, project.project_id)
        job = repository.start(_job(project.project_id, node.node_id))
        repository.record_state(job.job_id, JobState.RUNNING, backend_state="RUNNING")
        moved = repository.record_state(job.job_id, JobState.RUNNING, backend_job_ref="job-9")

    assert moved.backend_job_ref == "job-9"
    assert moved.state is JobState.RUNNING
    events = _backend_events(database, project)
    assert [event["backend_job_ref"] for event in events] == [None, None, "job-9"]


# ── What the database refuses ───────────────────────────────────────────────


def test_an_attempt_cannot_be_rewritten(
    database: Database, project: Project, node: DagNode
) -> None:
    """`attempt` is what makes starting idempotent.

    A row whose attempt could be moved would let one job stand in for another:
    a retry for attempt 2 would find attempt 1's job and adopt it.
    """
    _a_stored_job(database, project, node)

    with pytest.raises(IntegrityError, match="attempt is immutable"):
        _raw_sql(database, "UPDATE backend_jobs SET attempt = 2")


def test_a_jobs_contract_cannot_be_rewritten(
    database: Database, project: Project, node: DagNode
) -> None:
    """The contract version is what a result is measured against."""
    _a_stored_job(database, project, node)

    with pytest.raises(IntegrityError, match="execution_contract_version is immutable"):
        _raw_sql(database, "UPDATE backend_jobs SET execution_contract_version = 2")


def test_a_jobs_backend_cannot_change_under_it(
    database: Database, project: Project, node: DagNode
) -> None:
    """Swapping the backend would misattribute work nobody re-ran."""
    _a_stored_job(database, project, node)

    with pytest.raises(IntegrityError, match="backend is immutable"):
        _raw_sql(database, "UPDATE backend_jobs SET backend = 'mock-lab'")


def test_a_terminal_state_cannot_be_written_without_an_end_time(
    database: Database, project: Project, node: DagNode
) -> None:
    """A job that is over with no record of when reads as fact downstream."""
    _a_stored_job(database, project, node)

    with pytest.raises(IntegrityError, match="ending_is_all_or_nothing"):
        _raw_sql(database, "UPDATE backend_jobs SET state = 'COMPLETED'")


def test_a_failure_class_cannot_outlive_its_failure(
    database: Database, project: Project, node: DagNode
) -> None:
    _a_stored_job(database, project, node)

    with pytest.raises(IntegrityError, match="failure_class_only_on_failure"):
        _raw_sql(
            database,
            "UPDATE backend_jobs SET state = 'COMPLETED', ended_at = now(), "
            "failure_class = 'NON_RETRYABLE'",
        )


def test_a_job_cannot_be_deleted(
    database: Database, project: Project, node: DagNode
) -> None:
    """Nothing in RAVEL is deleted, and this table is not an exception."""
    _a_stored_job(database, project, node)

    with pytest.raises(IntegrityError, match="never deleted"):
        _raw_sql(database, "DELETE FROM backend_jobs")


def test_an_unknown_job_state_cannot_be_stored(
    database: Database, project: Project, node: DagNode
) -> None:
    """The vocabulary is closed at the column, so a backend's own word for a
    state cannot be mistaken for one RAVEL understands."""
    _a_stored_job(database, project, node)

    with pytest.raises(IntegrityError, match="state_is_known"):
        _raw_sql(database, "UPDATE backend_jobs SET state = 'ACTIVE'")


# ── Helpers ─────────────────────────────────────────────────────────────────


def _a_stored_job(database: Database, project: Project, node: DagNode) -> BackendJob:
    """One job in SUBMITTED, so the UPDATEs below have a row to act on."""
    with database.transaction() as session:
        return BackendJobRepository(session, project.project_id).start(
            _job(project.project_id, node.node_id)
        )


def _raw_sql(database: Database, statement: str) -> None:
    """Run SQL directly, so the guard is what refuses rather than a Python model."""
    with database.engine.begin() as connection:
        connection.execute(text(statement))


def _backend_events(database: Database, project: Project) -> list[dict[str, object]]:
    """Every `BACKEND_STATUS_CHANGED` payload this project emitted, in order."""
    with database.read_only() as session:
        payloads = list(
            session.execute(
                text(
                    "SELECT payload FROM project_events "
                    "WHERE project_id = :p AND event_type = 'BACKEND_STATUS_CHANGED' "
                    "ORDER BY seq"
                ),
                {"p": project.project_id},
            ).scalars()
        )
    return payloads
