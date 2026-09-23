"""What RAVEL records about a workspace it built, and what it cannot record.

The row is the only trace of the step between a frozen contract and a started
run, and it carries two very different things: a workspace with the hashes of
what was written into it, or a refusal and why. What is asserted here is that
PostgreSQL holds both intact — every field round-trips, a retried preparation
adds to the history rather than replacing it, and nothing can rewrite a row
after the run it describes has happened.

The last one is the property that makes the manifest evidence: what RAVEL built
a result from is what makes the result traceable, and a row that could be
edited afterwards would be a trace that follows whatever was written last.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import text
from tests.integration.conftest import Prepared

from ravel.domain.enums import NodeType
from ravel.domain.preparation import (
    PreparationCheck,
    PreparationOutcome,
    PreparationRecord,
    PreparationRefusal,
)
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.repositories.base import ProjectScopeError
from ravel.state.repositories.preparations import PreparationRepository

pytestmark = pytest.mark.integration

#: The manifest a materializer writes, in the shape the plan fixes.
MANIFEST: dict[str, object] = {
    "execution_contract_ref": "ctr-1",
    "execution_contract_version": 1,
    "materializer": "raspa",
    "materializer_version": "1",
    "method": "GCMC",
    "parameters": {"temperature_k": "298"},
    "generated_files": [
        {"name": "simulation.input", "role": "INPUT", "sha256": "a" * 64, "size_bytes": 42}
    ],
}


def a_prepared(project: Project, node_id: str, **overrides: object) -> PreparationRecord:
    """A preparation that built a workspace, with nothing else decided here."""
    fields: dict[str, object] = {
        "project_id": project.project_id,
        "node_id": node_id,
        "execution_contract_ref": "ctr-1",
        "execution_contract_version": 1,
        "outcome": PreparationOutcome.PREPARED,
        "materializer": "raspa",
        "materializer_version": "1",
        "workspace_path": "/runtime/prepared/proj/n1/v1",
        "required_outputs": ("isotherm.csv",),
        "manifest": MANIFEST,
        "checks": (PreparationCheck(name="software_available", passed=True),),
        "execution_metadata": {"software": "raspa", "entrypoint": "job.slurm"},
    }
    fields.update(overrides)
    return PreparationRecord(**fields)  # type: ignore[arg-type]


def a_refusal(project: Project, node_id: str, **overrides: object) -> PreparationRecord:
    """A preparation that built nothing, and says why."""
    fields: dict[str, object] = {
        "project_id": project.project_id,
        "node_id": node_id,
        "execution_contract_ref": "ctr-1",
        "execution_contract_version": 1,
        "outcome": PreparationOutcome.REFUSED,
        "refusal": PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
        "reason": "the contract names no temperature for the run",
    }
    fields.update(overrides)
    return PreparationRecord(**fields)  # type: ignore[arg-type]


def stored(database: Database, project: Project) -> list[PreparationRecord]:
    """Everything this project has recorded, read out of PostgreSQL."""
    with database.read_only() as session:
        return PreparationRepository(session, project.project_id).all()


def test_a_workspace_reads_back_field_for_field(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """Through the repository, out of the database, as the same record.

    Including the JSONB fields, which are where a round trip could quietly
    lose something: the manifest is the document a result is traced through,
    and a manifest that came back with its file hashes missing would be worse
    than no manifest at all.
    """
    node_id = prepare(node_type=NodeType.COMPUTATION).node_id
    written = a_prepared(project, node_id)
    with database.transaction() as session:
        PreparationRepository(session, project.project_id).record(written)

    (read_back,) = stored(database, project)

    assert read_back.preparation_id == written.preparation_id
    assert read_back.node_id == node_id
    assert read_back.execution_contract_ref == "ctr-1"
    assert read_back.execution_contract_version == 1
    assert read_back.outcome is PreparationOutcome.PREPARED
    assert read_back.is_prepared
    assert read_back.materializer == "raspa"
    assert read_back.materializer_version == "1"
    assert read_back.workspace_path == "/runtime/prepared/proj/n1/v1"
    assert read_back.required_outputs == ("isotherm.csv",)
    assert read_back.manifest == MANIFEST
    assert read_back.checks == (
        PreparationCheck(name="software_available", passed=True),
    )
    assert read_back.execution_metadata == {
        "software": "raspa",
        "entrypoint": "job.slurm",
    }
    assert read_back.refusal is None
    assert read_back.created_at == written.created_at


def test_a_refusal_reads_back_with_the_class_that_routes_it(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """Master's next step is decided by the class, so it survives storage."""
    node_id = prepare(node_type=NodeType.COMPUTATION).node_id
    with database.transaction() as session:
        PreparationRepository(session, project.project_id).record(
            a_refusal(project, node_id)
        )

    (read_back,) = stored(database, project)

    assert read_back.outcome is PreparationOutcome.REFUSED
    assert read_back.refusal is PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER
    assert read_back.reason == "the contract names no temperature for the run"
    assert read_back.workspace_path == ""
    assert not read_back.is_prepared


def test_a_retried_preparation_adds_to_the_history_rather_than_replacing_it(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """The case a unique constraint would have hidden.

    An attempt that refused and the attempt after it that prepared are two
    facts about one run: the first says the contract could not be materialized
    when the host was asked, the second says it could. Collapsing them would
    make RAVEL unable to say either.

    What a Worker is handed is the newest, and what a Worker is handed when the
    newest is a refusal is the preparation before it — a run does not stop
    having a workspace because a later attempt failed to build another one.
    """
    node_id = prepare(node_type=NodeType.COMPUTATION).node_id
    repository = PreparationRepository
    with database.transaction() as session:
        repository(session, project.project_id).record(a_refusal(project, node_id))
    with database.transaction() as session:
        prepared = a_prepared(project, node_id)
        repository(session, project.project_id).record(prepared)
    with database.transaction() as session:
        later = a_refusal(
            project, node_id, reason="the workspace root was not writable"
        )
        repository(session, project.project_id).record(later)

    with database.read_only() as session:
        scoped = repository(session, project.project_id)
        history = scoped.for_run(node_id, 1)
        newest = scoped.latest_for_run(node_id, 1)
        usable = scoped.prepared_for_run(node_id, 1)

    assert [record.refusal for record in history] == [
        PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
        None,
        PreparationRefusal.MISSING_SCIENTIFIC_PARAMETER,
    ]
    assert newest is not None and newest.preparation_id == later.preparation_id
    assert usable is not None and usable.preparation_id == prepared.preparation_id


def test_each_contract_version_is_its_own_history(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """A node whose terms Master revised runs in a workspace built from the new
    terms, and the workspace the first run used is still on record."""
    node_id = prepare(node_type=NodeType.COMPUTATION).node_id
    first = a_prepared(project, node_id, workspace_path="/runtime/prepared/proj/n1/v1")
    second = a_prepared(
        project,
        node_id,
        execution_contract_version=2,
        workspace_path="/runtime/prepared/proj/n1/v2",
    )
    with database.transaction() as session:
        repository = PreparationRepository(session, project.project_id)
        repository.record(first)
        repository.record(second)

    with database.read_only() as session:
        scoped = PreparationRepository(session, project.project_id)
        one = scoped.prepared_for_run(node_id, 1)
        two = scoped.prepared_for_run(node_id, 2)
        assert len(scoped.for_node(node_id)) == 2

    assert one is not None and one.workspace_path.endswith("/v1")
    assert two is not None and two.workspace_path.endswith("/v2")


def test_the_refusal_reported_as_stopping_a_node_is_the_newest_one(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """A refusal that was answered is history, and is not the answer now.

    The read exists for a node whose run the reader did not watch — Master,
    handed a node that stopped. What it must not do is report the reason
    something stopped *earlier*: a node that refused under version one, was
    given new terms, built its workspace and ran, is not stopped by the first
    refusal any more, and a Master told that it is would revise terms that were
    already revised.

    Both orderings are asserted, because only one of them is about the newest
    record and the other is about a refusal that is not the newest: an earlier
    refusal followed by a preparation is history, and a preparation followed by
    a refusal is the reason the node is where it is.
    """
    node_id = prepare(node_type=NodeType.COMPUTATION).node_id
    with database.transaction() as session:
        PreparationRepository(session, project.project_id).record(
            a_refusal(project, node_id)
        )
    with database.read_only() as session:
        assert PreparationRepository(session, project.project_id).stopping_refusal(
            node_id
        ) is not None

    with database.transaction() as session:
        answered = a_prepared(project, node_id)
        PreparationRepository(session, project.project_id).record(answered)
    with database.read_only() as session:
        assert (
            PreparationRepository(session, project.project_id).stopping_refusal(node_id)
            is None
        ), "a refusal the next preparation answered is not why the node stopped"

    # And the other way round: the newest record is a refusal, so it is the one
    # reported — under the version it was refused at, not the first one.
    with database.transaction() as session:
        later = a_refusal(
            project,
            node_id,
            execution_contract_version=2,
            reason="the workspace root was not writable",
        )
        PreparationRepository(session, project.project_id).record(later)

    with database.read_only() as session:
        stopping = PreparationRepository(session, project.project_id).stopping_refusal(
            node_id
        )

    assert stopping is not None
    assert stopping.preparation_id == later.preparation_id
    assert stopping.execution_contract_version == 2
    assert stopping.reason == "the workspace root was not writable"


def test_a_preparation_belongs_to_its_project(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """The repository's scope refuses the row, not the record's `project_id`.

    A supplied identifier is a claim; the session already knows which project
    it serves.
    """
    node_id = prepare(node_type=NodeType.COMPUTATION).node_id
    with pytest.raises(ProjectScopeError), database.transaction() as session:
        PreparationRepository(session, "someone-elses-project").record(
            a_prepared(project, node_id)
        )


def test_a_preparation_is_append_only(
    database: Database, project: Project, prepare: Callable[..., Prepared]
) -> None:
    """What RAVEL built a result from cannot be edited after the fact.

    The table was made read-only by not appearing in `guards.UPDATABLE_TABLES`,
    and this is the assertion that the omission did what it was for: an
    `UPDATE` that turned a refusal into a success, or a `DELETE` that removed
    the record of a workspace, would both be refused by PostgreSQL itself.
    """
    node_id = prepare(node_type=NodeType.COMPUTATION).node_id
    with database.transaction() as session:
        PreparationRepository(session, project.project_id).record(
            a_prepared(project, node_id)
        )

    for statement in (
        "UPDATE execution_preparations SET outcome = 'REFUSED'",
        "UPDATE execution_preparations SET workspace_path = '/somewhere/else'",
        "DELETE FROM execution_preparations",
    ):
        with pytest.raises(Exception, match="append-only"), database.transaction() as session:
            session.execute(text(statement))
