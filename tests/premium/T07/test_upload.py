"""Uploads: a counted stream, a cap, and never an orphan record.

T07's third criterion has three parts, and they are one mechanism seen from
three sides:

- a 65 MiB body is refused with 413;
- the process's memory does not track the file's size while an accepted 64 MiB
  body is read;
- a refusal — too large, or refused late by the object store — leaves no formal
  Artifact behind.

All three are properties of *where* the bytes wait. The route reads the request
in the chunks Starlette yields, counts them against the cap, and writes them to
a temporary file; the store hashes that file and uploads a spool, so neither
side ever holds the artifact in memory. The temporary file is removed on every
path, and the store is written before any row is staged, so a store that
refuses produces a 502 and no row.

The memory assertions use `tracemalloc` around a direct ASGI call rather than
`TestClient`, because `TestClient` buffers the whole request body before the
application sees it — a 64 MiB measurement through it would be measuring the
test client. The direct call hands the same 1 MiB object back sixty-four times,
so the test itself allocates nothing that grows with the upload.

The last group reaches the store directly, with a scripted S3 client. What is
asserted there is the part the route cannot show: the body handed to
`put_object` is the spooled file, the conditional write is sent, a precondition
failure becomes `ArtifactImmutableError`, and any other failure is reported by
its type without its text.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import io
import json
import logging
import tarfile
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import pytest
from sqlalchemy import text
from tests.integration.gateway.conftest import a_project, account, bearer
from tests.premium.T07.conftest import UPLOAD_LIMIT_BYTES, RecordingStore

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.routes.artifacts import StreamingS3ArtifactStore
from ravel.state.database import Database
from ravel.state.store import ArtifactImmutableError, ArtifactStoreError

MIB = 1024 * 1024

#: The two row counts every refusal case has to leave at zero. A refusal that
#: staged an Artifact before it decided to fail would still be a formal record
#: of bytes nobody can read, which is the orphan the criterion forbids.
_ARTIFACT_ROWS = text("SELECT count(*) FROM artifacts")
_VERSION_ROWS = text("SELECT count(*) FROM artifact_versions")


def _count(database: Database, statement: Any) -> int:
    with database.read_only() as session:
        return int(session.execute(statement).scalar_one())


@pytest.fixture
def upload_owner(database: Database, tokens: TokenService) -> tuple[Any, str]:
    """A project with one member, and an access token for that member."""
    project = a_project(database, title="Upload boundary")
    user_id = account(
        database, username="ada", role=UserRole.PROJECT_OWNER, project=project
    )
    token, _ = tokens.issue_access(user_id)
    return project, token


@pytest.fixture
def temporary_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Every temporary file these cases provoke lands in an isolated directory.

    Asserting that the directory is empty afterwards is how "the refusal
    removed what it wrote" is observed rather than assumed.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


# ── The route, over TestClient: what is stored and what is registered ──────


def test_a_small_upload_is_stored_once_and_registered(
    client: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
) -> None:
    """The bytes reach the store unchanged and one version row describes them."""
    project, token = upload_owner
    recording_store.capture_bytes = True
    body = b"sample,conductivity\nniobium,1.2\n"

    response = client.post(
        f"/projects/{project.project_id}/artifacts",
        params={
            "name": "conductivity",
            "filename": "conductivity.csv",
            "media_type": "text/csv",
            "kind": "measurement",
        },
        content=body,
        headers=bearer(token),
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    artifact = payload["artifact"]
    version = payload["version"]
    assert artifact["project_id"] == project.project_id
    assert artifact["name"] == "conductivity"
    assert artifact["kind"] == "measurement"
    assert version["version"] == 1
    assert version["filename"] == "conductivity.csv"
    assert version["media_type"] == "text/csv"
    assert version["size_bytes"] == len(body)
    digest = hashlib.sha256(body).hexdigest()
    assert version["content_hash"] == f"sha256:{digest}"

    [stored] = recording_store.uploads
    assert stored.key == (
        f"projects/{project.project_id}/{artifact['artifact_id']}"
        "/1/conductivity.csv"
    )
    assert stored.body == body, "the store received different bytes than were sent"
    assert stored.content_hash == version["content_hash"]
    assert stored.media_type == "text/csv"

    assert _count(database, _ARTIFACT_ROWS) == 1
    assert _count(database, _VERSION_ROWS) == 1
    assert list(temporary_directory.iterdir()) == []


def test_a_new_artifact_without_a_name_is_refused_before_the_body_is_read(
    client: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
) -> None:
    """The name rule is answered before a store is resolved or a byte is kept."""
    project, token = upload_owner

    response = client.post(
        f"/projects/{project.project_id}/artifacts",
        content=b"bytes",
        headers=bearer(token),
    )

    assert response.status_code == 400, response.text
    assert response.json()["detail"] == (
        "a new artifact needs a name; adding a version does not"
    )
    assert recording_store.uploads == []
    assert _count(database, _ARTIFACT_ROWS) == 0
    assert list(temporary_directory.iterdir()) == []


# ── The cap ─────────────────────────────────────────────────────────────────


def test_a_declared_oversize_body_is_refused_before_it_is_read(
    app: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
) -> None:
    """`Content-Length` is answered from the header; the body is never pulled.

    The receive function fails the test if it is called at all, so this is not
    merely "the answer was 413": a route that read the declared length after
    buffering the body would fail here.
    """
    project, token = upload_owner

    async def unread_body() -> dict[str, Any]:
        raise AssertionError("the body was read before the declared length was refused")

    status, body = _drive(
        app,
        _scope(
            project.project_id,
            token,
            params={"name": "oversize"},
            content_length=UPLOAD_LIMIT_BYTES + 1,
        ),
        unread_body,
    )

    assert status == 413, body
    assert json.loads(body)["detail"] == (
        f"this deployment accepts uploads up to {UPLOAD_LIMIT_BYTES} bytes"
    )
    assert recording_store.uploads == []
    assert _count(database, _ARTIFACT_ROWS) == 0
    assert _count(database, _VERSION_ROWS) == 0
    assert list(temporary_directory.iterdir()) == []


def test_a_sixty_five_mebibyte_body_without_a_length_is_counted_and_refused(
    app: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
) -> None:
    """A chunked upload has no header to trust, so the count has to do it.

    Sixty-five chunks of one mebibyte: the sixty-fifth would take the temporary
    file past the cap, and the route refuses before writing it. The store is
    never reached, which is what keeps a refused body from becoming an object
    nobody has a row for.
    """
    project, token = upload_owner
    receive, sent = _body(b"c" * MIB, count=65)

    status, body = _drive(
        app,
        _scope(project.project_id, token, params={"name": "chunked"}),
        receive,
    )

    assert status == 413, body
    assert len(sent) == 65, "the route stopped reading before the cap was exceeded"
    assert json.loads(body)["detail"] == (
        f"this deployment accepts uploads up to {UPLOAD_LIMIT_BYTES} bytes"
    )
    assert recording_store.uploads == []
    assert _count(database, _ARTIFACT_ROWS) == 0
    assert _count(database, _VERSION_ROWS) == 0
    assert list(temporary_directory.iterdir()) == []


def test_a_sixty_four_mebibyte_upload_does_not_grow_process_memory(
    app: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
) -> None:
    """Memory stays a constant while a body at the cap goes through.

    A buffering route would need at least the file's sixty-four mebibytes at
    once. The bound asserted here is a quarter of that, which no implementation
    that held the body — in the route or in the store — could meet; the exact
    peak is left loose so the test measures the shape and not an allocator's
    mood.
    """
    project, token = upload_owner
    chunk = b"u" * MIB
    receive, sent = _body(chunk, count=64)

    tracemalloc = pytest.importorskip("tracemalloc")
    tracemalloc.start()
    try:
        status, body = _drive(
            app,
            _scope(project.project_id, token, params={"name": "big"}),
            receive,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert status == 201, body
    assert len(sent) == 64
    [stored] = recording_store.uploads
    assert stored.size_bytes == UPLOAD_LIMIT_BYTES
    assert peak < 16 * MIB, (
        f"a {UPLOAD_LIMIT_BYTES}-byte upload used {peak} bytes of process memory; "
        "the body is being retained instead of streamed"
    )
    assert _count(database, _ARTIFACT_ROWS) == 1
    assert _count(database, _VERSION_ROWS) == 1
    assert list(temporary_directory.iterdir()) == []


# ── Refusals leave nothing behind ──────────────────────────────────────────


def test_a_store_that_refuses_after_reading_leaves_no_formal_artifact(
    client: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A late refusal is a 502, and no row survives the rolled-back transaction.

    The recorder consumes the chunks *before* raising, so the ordering the
    repository promises is exercised: the object write happens first, the
    metadata is staged afterwards, and a failure between the two leaves no
    artifact pointing at bytes that were refused.
    """
    project, token = upload_owner
    secret = "AKIAIOSFODNN7EXAMPLE"
    recording_store.failure = ArtifactStoreError(
        f"the store refused the write with credential {secret}"
    )
    caplog.set_level(logging.WARNING, logger="ravel.gateway.routes.artifacts")

    response = client.post(
        f"/projects/{project.project_id}/artifacts",
        params={"name": "refused"},
        content=b"bytes that were read and then refused",
        headers=bearer(token),
    )

    assert response.status_code == 502, response.text
    assert response.json()["detail"] == "the artifact store refused the upload"
    assert secret not in response.text
    assert secret not in caplog.text
    assert len(recording_store.uploads) == 1, "the store did not consume the upload"
    assert _count(database, _ARTIFACT_ROWS) == 0
    assert _count(database, _VERSION_ROWS) == 0
    assert list(temporary_directory.iterdir()) == []


@pytest.mark.parametrize(
    ("filename", "problem"),
    [
        pytest.param("../../etc/passwd", "path separator", id="parent-traversal"),
        pytest.param("..", "names a directory", id="dot-dot"),
        pytest.param(".", "names a directory", id="dot"),
        pytest.param("a/b", "path separator", id="forward-slash"),
        pytest.param("a\\b", "path separator", id="backslash"),
        pytest.param("   ", "it is empty", id="blank"),
    ],
)
def test_a_filename_that_could_escape_the_key_prefix_is_refused(
    client: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
    filename: str,
    problem: str,
) -> None:
    """The name becomes a storage-key component, so it is checked as one.

    The submitted value is not echoed: the answer says what is wrong with it,
    not what it was. A 400 raised here is what keeps `artifact_key` from
    turning a traversal attempt into a 500 after the body has been accepted.
    """
    project, token = upload_owner

    response = client.post(
        f"/projects/{project.project_id}/artifacts",
        params={"name": "result", "filename": filename},
        content=b"bytes",
        headers=bearer(token),
    )

    assert response.status_code == 400, response.text
    detail = response.json()["detail"]
    assert detail.startswith("the filename is not usable: "), detail
    assert problem in detail
    if filename.strip():
        assert filename.strip() not in response.text.replace(detail, ""), (
            "the refused filename was echoed back"
        )
    assert recording_store.uploads == []
    assert _count(database, _ARTIFACT_ROWS) == 0
    assert list(temporary_directory.iterdir()) == []


def test_an_archive_is_stored_verbatim_and_never_extracted(
    client: Any,
    upload_owner: tuple[Any, str],
    database: Database,
    recording_store: RecordingStore,
    temporary_directory: Path,
) -> None:
    """A bomb is a file like any other, and RAVEL does not unpack uploads.

    The archive below contains a member that would escape the extraction
    directory. RAVEL has no extraction step, so the bytes are stored exactly as
    they arrived and nothing is written beside them — including on the path
    where the temporary file was written and then removed.
    """
    project, token = upload_owner
    recording_store.capture_bytes = True
    archive = _tar_gz("../escape.txt", b"written outside the extraction root")

    response = client.post(
        f"/projects/{project.project_id}/artifacts",
        params={
            "name": "archive",
            "filename": "archive.tar.gz",
            "media_type": "application/gzip",
        },
        content=archive,
        headers=bearer(token),
    )

    assert response.status_code == 201, response.text
    [stored] = recording_store.uploads
    assert stored.body == archive, "the archive was altered before it was stored"
    assert stored.media_type == "application/gzip"
    assert b"../escape.txt" in gzip.decompress(archive), (
        "the fixture no longer contains the member this case is about"
    )
    assert not (temporary_directory / "escape.txt").exists()
    assert not (temporary_directory.parent / "escape.txt").exists()
    assert list(temporary_directory.iterdir()) == []
    assert _count(database, _ARTIFACT_ROWS) == 1


# ── The streaming store, against a scripted S3 client ──────────────────────


class S3Failure(Exception):
    """A client error carrying the shape boto3 uses for a precondition."""

    def __init__(self, message: str, response: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.response = response


class FakeS3:
    """The narrow part of an S3 client `put_object` touches.

    It reads the body the way the real client does — in chunks, until EOF — so
    a store that handed it the whole payload would be visible as a peak in the
    memory case, and a store that handed it a closed spool would fail here.
    """

    def __init__(self, *, capture: bool = True, failure: Exception | None = None) -> None:
        self.capture = capture
        self.failure = failure
        self.calls: list[dict[str, Any]] = []
        self.body: Any = None
        self.kept = b""
        self.size = 0
        self.digest = ""

    def put_object(self, **request: Any) -> dict[str, Any]:
        body = request["Body"]
        self.body = body
        self.calls.append(request)
        digest = hashlib.sha256()
        kept = bytearray()
        while block := body.read(MIB):
            digest.update(block)
            self.size += len(block)
            if self.capture:
                kept.extend(block)
        self.digest = digest.hexdigest()
        self.kept = bytes(kept)
        if self.failure is not None:
            raise self.failure
        return {}


def _streaming_store(
    settings: Settings, client: FakeS3, *, memory_bytes: int = MIB
) -> StreamingS3ArtifactStore:
    return StreamingS3ArtifactStore(
        settings, client=client, bucket="artifacts", memory_bytes=memory_bytes
    )


def test_the_streaming_store_hashes_what_it_uploads_and_refuses_to_replace(
    gateway_settings: Settings,
) -> None:
    """One pass: hash while spooling, then one conditional write of that spool."""
    fake = FakeS3()
    store = _streaming_store(gateway_settings, fake)
    body = b"first half|" + b"second half"
    key = "projects/p1/artifacts/a1/1/result.bin"

    stored = store.put(
        key, [body[:10], body[10:]], media_type="application/octet-stream"
    )

    digest = hashlib.sha256(body).hexdigest()
    assert stored.key == key
    assert stored.content_hash == f"sha256:{digest}"
    assert stored.size_bytes == len(body)
    assert fake.kept == body
    assert fake.size == len(body)
    assert fake.digest == digest

    [request] = fake.calls
    assert request["Bucket"] == "artifacts"
    assert request["Key"] == key
    assert request["IfNoneMatch"] == "*", "the write was not conditional"
    assert request["ContentType"] == "application/octet-stream"
    assert hasattr(request["Body"], "read"), "the payload was buffered rather than spooled"
    assert request["Body"].closed is True, "the spool was left open after the upload"


def test_the_store_spills_past_its_memory_budget_and_removes_the_file(
    gateway_settings: Settings, temporary_directory: Path
) -> None:
    """More than sixteen bytes is a file on disk, and the file is gone afterwards."""
    fake = FakeS3()
    store = _streaming_store(gateway_settings, fake, memory_bytes=16)

    stored = store.put("projects/p1/artifacts/a1/1/result.bin", [b"z" * 64])

    assert stored.size_bytes == 64
    assert fake.kept == b"z" * 64
    assert getattr(fake.body, "_rolled", False) is True, "the payload never spilled to disk"
    assert fake.body.closed is True
    assert list(temporary_directory.iterdir()) == [], "the spool outlived the upload"


def test_the_streaming_store_does_not_keep_a_large_upload_in_memory(
    gateway_settings: Settings, temporary_directory: Path
) -> None:
    """Thirty-two mebibytes through the store, under eight mebibytes of memory.

    The caller reuses one chunk, so the only thing that could grow with the
    payload is the store itself. A store that buffered would need at least the
    payload; the bound is a quarter of it.
    """
    fake = FakeS3(capture=False)
    store = _streaming_store(gateway_settings, fake)
    chunk = b"m" * MIB

    tracemalloc = pytest.importorskip("tracemalloc")
    tracemalloc.start()
    try:
        stored = store.put(
            "projects/p1/artifacts/a1/1/result.bin", (chunk for _ in range(32))
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert stored.size_bytes == 32 * MIB
    assert fake.size == 32 * MIB
    assert peak < 8 * MIB, (
        f"a 32 MiB store write used {peak} bytes of process memory; "
        "the payload is being buffered"
    )
    assert fake.body.closed is True
    assert list(temporary_directory.iterdir()) == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        pytest.param(
            S3Failure(
                "precondition failed for AKIAIOSFODNN7EXAMPLE",
                {"ResponseMetadata": {"HTTPStatusCode": 412}},
            ),
            ArtifactImmutableError,
            id="http-412",
        ),
        pytest.param(
            S3Failure(
                "concurrent create for AKIAIOSFODNN7EXAMPLE",
                {"ResponseMetadata": {"HTTPStatusCode": 409}},
            ),
            ArtifactImmutableError,
            id="http-409",
        ),
        pytest.param(
            S3Failure(
                "precondition failed for AKIAIOSFODNN7EXAMPLE",
                {"Error": {"Code": "PreconditionFailed"}},
            ),
            ArtifactImmutableError,
            id="precondition-code",
        ),
        pytest.param(
            S3Failure("access denied for AKIAIOSFODNN7EXAMPLE"),
            ArtifactStoreError,
            id="other-failure",
        ),
    ],
)
def test_a_store_refusal_keeps_its_kind_and_never_carries_the_client_text(
    gateway_settings: Settings,
    failure: S3Failure,
    expected: type[ArtifactStoreError],
) -> None:
    """Preconditions are immutability; everything else is a store error.

    Neither message may contain the client library's text: it can name a
    credential, an endpoint, or a request identifier, and the route above turns
    either error into a fixed sentence anyway. The distinction is preserved
    because a caller that *can* act on "this key exists" needs to be able to
    tell it apart from an outage.
    """
    store = _streaming_store(gateway_settings, FakeS3(failure=failure))

    with pytest.raises(ArtifactStoreError) as refused:
        store.put("projects/p1/artifacts/a1/1/result.bin", [b"bytes"])

    assert type(refused.value) is expected
    assert refused.value.__cause__ is failure
    message = str(refused.value)
    assert "AKIAIOSFODNN7EXAMPLE" not in message
    assert "precondition failed" not in message
    assert "access denied" not in message
    if expected is ArtifactImmutableError:
        assert "already exists" in message
        assert "immutable" in message
    else:
        assert "S3Failure" in message
        assert "could not store" in message


# ── Direct ASGI helpers ────────────────────────────────────────────────────


def _scope(
    project_id: str,
    token: str,
    *,
    params: dict[str, str],
    content_length: int | None = None,
) -> dict[str, Any]:
    """A POST scope for the upload route, with the body left to `receive`."""
    path = f"/projects/{project_id}/artifacts"
    headers = [
        (b"host", b"testserver"),
        (b"authorization", f"Bearer {token}".encode()),
        (b"content-type", b"application/octet-stream"),
        (b"accept", b"application/json"),
    ]
    if content_length is not None:
        headers.append((b"content-length", str(content_length).encode()))
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "root_path": "",
        "query_string": urlencode(params).encode(),
        "headers": headers,
        "client": ("203.0.113.9", 5000),
        "server": ("testserver", 80),
    }


def _body(chunk: bytes, *, count: int) -> tuple[Any, list[int]]:
    """A receive callable that yields one reused chunk `count` times.

    Reusing the object is deliberate: a test that allocated sixty-four distinct
    mebibytes would be the thing under test in the memory case.
    """
    sent: list[int] = []

    async def receive() -> dict[str, Any]:
        if len(sent) < count:
            sent.append(len(chunk))
            return {"type": "http.request", "body": chunk, "more_body": True}
        return {"type": "http.request", "body": b"", "more_body": False}

    return receive, sent


def _drive(app: Any, scope: dict[str, Any], receive: Any) -> tuple[int, bytes]:
    """Run one request through the real ASGI application and collect the answer."""
    messages: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    asyncio.run(app(scope, receive, send))
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    return int(start["status"]), body


def _tar_gz(name: str, payload: bytes) -> bytes:
    """A gzipped tar with exactly one member, named whatever the caller says."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo(name)
        member.size = len(payload)
        archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()
