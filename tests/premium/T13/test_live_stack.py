"""T13-AC4: the acceptance suite runs the real application on PostgreSQL.

The evidence for this criterion is the suite's composition, and this case makes
that composition visible rather than assumed. It holds the object the
application actually uses, writes rows *after* the application is running, and
watches the API's answers change to match the rows. A static JSON fixture
standing in for a backend would keep answering the same snapshot, which is the
failure the task names.

Everything below is read back with SQL against the same engine the routes use —
including the database's own name, so a suite accidentally pointed at a
developer database is visible in the evidence rather than inferred from it.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.premium.T13.conftest import account, bearer, sign_in
from tests.premium.T13.support import emit_note

from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.state.database import Database

pytestmark = pytest.mark.integration


def test_the_api_reads_the_rows_the_test_writes_through_the_same_engine(
    client: TestClient, database: Database, project: Project
) -> None:
    """The application under test is the real one, wired to this database.

    The snapshot's anchor, counts, and project title move when rows are written
    after startup. That is only possible if the response was assembled by
    querying PostgreSQL through the application's own engine — the thing this
    criterion requires and a fixture payload cannot imitate.
    """
    assert isinstance(client.app, FastAPI)  # type: ignore[attr-defined]
    state = client.app.state.ravel  # type: ignore[attr-defined]
    assert state.database is database

    with database.read_only() as session:
        current_database = session.execute(text("SELECT current_database()")).scalar_one()
    assert current_database.endswith("_test"), current_database
    assert database.engine.url.database == current_database

    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    first = client.get(f"/v1/projects/{project.project_id}/snapshot", headers=headers)
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert first_body["project"]["title"] == project.title
    assert first_body["anchor_seq"] == 3
    assert first_body["counts"]["events"] == 3

    for body in ("one", "two"):
        emit_note(database, project.project_id, body)

    second = client.get(f"/v1/projects/{project.project_id}/snapshot", headers=headers)
    assert second.status_code == 200, second.text
    second_body = second.json()
    assert second_body["anchor_seq"] == first_body["anchor_seq"] + 2
    assert second_body["counts"]["events"] == first_body["counts"]["events"] + 2

    with database.read_only() as session:
        counted = session.execute(
            text(
                "SELECT count(*), max(seq) FROM project_events WHERE project_id = :project_id"
            ),
            {"project_id": project.project_id},
        ).one()
    assert counted[0] == second_body["counts"]["events"] == 5
    assert counted[1] == second_body["anchor_seq"] == 5

    legacy = client.get(
        f"/projects/{project.project_id}/events?after_seq=3", headers=headers
    )
    assert legacy.status_code == 200
    assert [event["payload"] for event in legacy.json()["events"]] == [
        {"note": "one"},
        {"note": "two"},
    ]
