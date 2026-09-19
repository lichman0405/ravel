"""Bytes in and out of a project, through the only door there is.

`docs/04` says artifact bytes live in object storage and never in PostgreSQL,
and that is the claim most of this file exists to check — not by reading the
upload route's source, which could be right today and wrong after a refactor,
but by uploading a recognisable body and then searching every column of both
artifact tables for it.

The rest is the boundary. An artifact is the most sensitive thing a project
holds and the one thing a lab user must be able to write, so the two halves of
the authorization story get a test each: a member who is not the owner may
upload, and a member of a different project may not read it.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from tests.integration.gateway.conftest import account, bearer, sign_in

from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.store import S3ArtifactStore

pytestmark = pytest.mark.integration

#: A body with something in it that no other column could plausibly contain, so
#: that finding it in a table means the bytes were stored there and not that a
#: hash or a filename happened to collide with the search.
BODY = b"conductivity,siemens_per_cm\n0.0412,0.0419,0.0398\n"

MARKER = "0.0412,0.0419,0.0398"


def upload(
    client: TestClient,
    project: Project,
    headers: dict[str, str],
    body: bytes = BODY,
    **query: str,
) -> Any:
    """POST one artifact, with the query string the route reads its metadata from."""
    return client.post(
        f"/projects/{project.project_id}/artifacts",
        params=query,
        content=body,
        headers=headers,
    )


@pytest.fixture
def store(artifact_store: S3ArtifactStore) -> S3ArtifactStore:
    """The real bucket, so a download is a download and not a mock's replay."""
    return artifact_store


# ── Where the bytes live ────────────────────────────────────────────────────


def test_an_upload_puts_the_bytes_in_the_bucket_and_not_in_postgres(
    client: TestClient,
    database: Database,
    project: Project,
    store: S3ArtifactStore,
) -> None:
    """The rule `docs/04` states, checked against the database itself.

    Every text and bytea column of both artifact tables is searched for a
    substring of the uploaded body. The upload route could change shape freely
    and this test would still be about the thing that matters: whether the
    database is holding research data it has no business holding.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = upload(
        client, project, headers, name="Run 1", filename="run1.csv", media_type="text/csv"
    )
    assert response.status_code == 201, response.text
    version = response.json()["version"]

    with database.read_only() as session:
        rows = [
            row
            for table in ("artifacts", "artifact_versions")
            for row in session.execute(text(f"SELECT * FROM {table}")).mappings().all()
        ]

    assert rows, "the upload registered nothing"
    for row in rows:
        for column, value in row.items():
            if isinstance(value, str):
                assert MARKER not in value, f"{column} is holding the artifact's bytes"
            elif isinstance(value, bytes):
                assert MARKER.encode() not in value, f"{column} is holding the bytes"

    # And the bytes are where they should be, readable under the key the
    # version row names. Checked through the store rather than through the
    # route so that the two halves of the claim are independent.
    with store.open(version["storage_key"]) as stream:
        assert stream.read() == BODY

    assert version["content_hash"]
    assert version["size_bytes"] == len(BODY)


@pytest.mark.usefixtures("artifact_store")
def test_the_hash_recorded_is_the_hash_of_the_bytes(
    client: TestClient, database: Database, project: Project
) -> None:
    """Because a decision cites the hash, and a hash that describes nothing is worse than none.

    The bucket is required but not read: the upload writes to it, and without
    the fixture a deployment with no object storage would fail here instead of
    skipping, which would look like a broken hash rather than an absent MinIO.
    """
    from ravel.state.store import hash_chunks

    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = upload(client, project, headers, name="Run 1", filename="run1.csv")

    digest, size = hash_chunks([BODY])
    assert response.json()["version"]["content_hash"] == digest
    # Both halves, because a hash recorded against the wrong length describes
    # bytes that were never stored.
    assert response.json()["version"]["size_bytes"] == size


# ── Versions ────────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("artifact_store")
def test_a_second_upload_becomes_a_new_version_and_the_first_stays_readable(
    client: TestClient, database: Database, project: Project
) -> None:
    """An artifact is appended to, never replaced.

    The point of immutability is that a result cited in a decision still means
    what it meant, so the test reads version 1 back *after* version 2 exists and
    checks it is still the original bytes.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    first = upload(client, project, headers, name="Run 1", filename="run1.csv")
    assert first.status_code == 201, first.text
    artifact_id = first.json()["artifact"]["artifact_id"]

    corrected = b"conductivity,siemens_per_cm\n0.0401,0.0404,0.0399\n"
    second = upload(
        client,
        project,
        headers,
        corrected,
        artifact_id=artifact_id,
        filename="run1.csv",
    )
    assert second.status_code == 201, second.text
    assert second.json()["version"]["version"] == 2
    assert second.json()["artifact"]["artifact_id"] == artifact_id

    one = client.get(
        f"/projects/{project.project_id}/artifacts/{artifact_id}/versions/1/content",
        headers=headers,
    )
    two = client.get(
        f"/projects/{project.project_id}/artifacts/{artifact_id}/versions/2/content",
        headers=headers,
    )
    assert one.content == BODY
    assert two.content == corrected
    assert one.headers["etag"] != two.headers["etag"]

    listed = client.get(f"/projects/{project.project_id}/artifacts", headers=headers).json()
    assert [artifact["artifact_id"] for artifact in listed] == [artifact_id]

    detail = client.get(
        f"/projects/{project.project_id}/artifacts/{artifact_id}", headers=headers
    ).json()
    assert [version["version"] for version in detail["versions"]] == [1, 2]


# ── Downloads ───────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("artifact_store")
def test_a_download_carries_the_bytes_and_an_etag_that_saves_the_next_one(
    client: TestClient, database: Database, project: Project
) -> None:
    """The hash is the only fact about an artifact a decision cites.

    So "has this changed since I read it" is the question a reader has, and the
    answer has to be cheaper than the download or nobody will ask it.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    artifact_id = upload(
        client, project, headers, name="Run 1", filename="run1.csv", media_type="text/csv"
    ).json()["artifact"]["artifact_id"]

    url = f"/projects/{project.project_id}/artifacts/{artifact_id}/versions/1/content"
    response = client.get(url, headers=headers)

    assert response.status_code == 200
    assert response.content == BODY
    assert response.headers["content-type"].startswith("text/csv")
    etag = response.headers["etag"]
    assert etag == f'"{response.headers["x-ravel-content-hash"]}"'

    again = client.get(url, headers={**headers, "If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""


@pytest.mark.usefixtures("artifact_store")
def test_a_filename_cannot_talk_its_way_out_of_the_header(
    client: TestClient, database: Database, project: Project
) -> None:
    """The filename is chosen by whoever uploaded it, so it is not pasted in raw.

    A quote would end the value and everything after it would be read as another
    header parameter; a newline would end the header. Both are stripped, and the
    response is still a well-formed download.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    artifact_id = upload(
        client,
        project,
        headers,
        name="Run 1",
        filename='re"port\nX-Evil: yes',
    ).json()["artifact"]["artifact_id"]

    response = client.get(
        f"/projects/{project.project_id}/artifacts/{artifact_id}/versions/1/content",
        headers=headers,
    )

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition.startswith("attachment; filename=")
    assert "\n" not in disposition
    assert "X-Evil" not in response.headers


def test_a_version_that_does_not_exist_is_a_404(
    client: TestClient, database: Database, project: Project, store: S3ArtifactStore
) -> None:
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    artifact_id = upload(client, project, headers, name="Run 1").json()["artifact"]["artifact_id"]

    missing = client.get(
        f"/projects/{project.project_id}/artifacts/{artifact_id}/versions/9/content",
        headers=headers,
    )
    assert missing.status_code == 404

    unknown = client.get(
        f"/projects/{project.project_id}/artifacts/no-such-artifact/versions/1/content",
        headers=headers,
    )
    assert unknown.status_code == 404


# ── What an upload will not accept ──────────────────────────────────────────


def test_a_new_artifact_without_a_name_is_refused(
    client: TestClient, database: Database, project: Project, store: S3ArtifactStore
) -> None:
    """Adding a version may go unnamed; creating one may not.

    An unnamed artifact is a row nothing can refer to by anything a person
    recognises, and the name is the only part of the record a human wrote.

    The refusal is a 400 from the route and not a 422 from the schema, which is
    a distinction this test exists to keep. `name` once carried `min_length=1`,
    and because a default is validated like any other value that made the empty
    default itself invalid — so the version-append path, where empty is correct,
    answered 422 to every caller. See `test_a_second_upload_becomes_a_new_
    version_and_the_first_stays_readable`.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = upload(client, project, headers)
    assert response.status_code == 400, response.text
    assert "name" in response.json()["detail"]

    with database.read_only() as session:
        assert session.execute(text("SELECT count(*) FROM artifacts")).scalar() == 0


def test_an_oversized_upload_is_refused_by_its_declared_length(
    client: TestClient, database: Database, project: Project, store: S3ArtifactStore
) -> None:
    """Checked before the body is read.

    The cap exists because the store hashes the whole object in the Gateway's
    own process, so a body that is too large must be refused before it is
    buffered — which means believing `Content-Length` when it is sent.
    """
    from ravel.gateway.routes.artifacts import MAX_UPLOAD_BYTES

    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    response = client.post(
        f"/projects/{project.project_id}/artifacts",
        params={"name": "Huge"},
        content=b"x",
        headers={**headers, "Content-Length": str(MAX_UPLOAD_BYTES + 1)},
    )
    assert response.status_code == 413, response.text


# ── Who may do it ───────────────────────────────────────────────────────────


def test_a_lab_member_may_upload(
    client: TestClient, database: Database, project: Project, store: S3ArtifactStore
) -> None:
    """The person holding the file is the person who ran the experiment.

    Requiring the owner to relay it would mean the owner uploading files they
    have never seen, and the record would say the owner was at the bench.
    """
    user_id = account(database, username="lab", role=UserRole.LAB_USER, project=project)
    headers = bearer(sign_in(client, "lab")["access_token"])

    response = upload(client, project, headers, name="Run 1", filename="run1.csv")

    assert response.status_code == 201, response.text
    assert response.json()["version"]["created_by"] == user_id


def test_a_member_of_another_project_cannot_read_or_write(
    client: TestClient,
    database: Database,
    project: Project,
    other_project: Project,
    store: S3ArtifactStore,
) -> None:
    """404 rather than 403, and the same 404 a stranger gets.

    A refusal that distinguishes "exists but not yours" from "does not exist"
    is a membership oracle: it tells a caller which project identifiers are
    real. The upload is refused too, which is the half that matters — a member
    of one project writing into another would put a file in a bucket no record
    in their own project points at.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    owner = bearer(sign_in(client, "ada")["access_token"])
    artifact_id = upload(client, project, owner, name="Run 1").json()["artifact"]["artifact_id"]

    account(database, username="bob", role=UserRole.PROJECT_OWNER, project=other_project)
    intruder = bearer(sign_in(client, "bob")["access_token"])

    assert (
        client.get(f"/projects/{project.project_id}/artifacts", headers=intruder).status_code
        == 404
    )
    assert (
        client.get(
            f"/projects/{project.project_id}/artifacts/{artifact_id}/versions/1/content",
            headers=intruder,
        ).status_code
        == 404
    )
    assert upload(client, project, intruder, name="Sneaky").status_code == 404

    with database.read_only() as session:
        assert session.execute(text("SELECT count(*) FROM artifacts")).scalar() == 1


def test_no_token_is_refused(
    client: TestClient, database: Database, project: Project
) -> None:
    assert client.get(f"/projects/{project.project_id}/artifacts").status_code == 401
    assert (
        client.post(
            f"/projects/{project.project_id}/artifacts",
            params={"name": "Anonymous"},
            content=b"hello",
        ).status_code
        == 401
    )
