"""T13-AC3: the published `/v1` surface, its generated client, and no DAG write.

Four things are asserted here, and they are different claims:

- the OpenAPI document describes exactly the routes `CONTRACTS.md` lists, with
  the methods it lists, plus the versioned WebSocket (which OpenAPI does not
  describe and which is therefore checked on the router);
- no operation anywhere in the document writes the Scientific DAG — every
  `/dag` path is a GET, and the premium route package does not name the DAG
  mutation service;
- `packages/api-client` regenerates byte for byte from the current application,
  so the committed TypeScript is a function of the schema rather than a file
  somebody edited;
- the legacy surface the TUI already uses still answers with the same shapes,
  values, and socket anchor.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.premium.T13.conftest import PASSWORD, account, bearer, sign_in
from tests.premium.T13.support import EXPECTED_V1
from tests.support.routes import effective_routes

from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.state.database import Database

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "packages" / "api-client"


def _versioned(document: dict[str, Any]) -> dict[str, set[str]]:
    return {
        path: {
            method
            for method, operation in operations.items()
            if isinstance(operation, dict)
        }
        for path, operations in document["paths"].items()
        if path.startswith("/v1/")
    }


def test_the_published_v1_surface_matches_the_contract(
    client: TestClient, database: Database, project: Project
) -> None:
    """Every contract route, no extra route, and the versioned socket.

    `/v1` is a prefix a client can pin. A route that appeared here without a
    line in the contract would be an API the runtime did not promise; a route
    that disappeared would be a broken client.

    The WebSocket is not in an OpenAPI document at all, so it is checked by
    connecting to it. `effective_routes` reports the nested router's socket
    without the outer `/v1` prefix — a limitation of flattening nested
    wrappers, not of the route — which is why the live connection is the
    evidence rather than a path string from that walk.
    """
    document = client.app.openapi()  # type: ignore[attr-defined]
    published = _versioned(document)
    assert published == EXPECTED_V1

    http_routes = {
        (route.path, method)
        for route in effective_routes(client.app)  # type: ignore[attr-defined]
        if not route.websocket
        for method in route.methods
    }
    assert ("/v1/projects/{project_id}/snapshot", "GET") in http_routes
    assert ("/v1/projects/{project_id}/actions/{action_id}/approve", "POST") in http_routes

    account(database, username="socket-owner", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "socket-owner")["access_token"])
    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events?after_seq=0", headers=headers
    ) as socket:
        anchor = socket.receive_json()
    assert anchor["type"] == "anchor"
    assert anchor["project_id"] == project.project_id

    assert document["components"]["securitySchemes"], "the schema names no auth scheme"
    for path in published:
        assert path.startswith("/v1/")
    total_operations = sum(len(methods) for methods in published.values())
    assert total_operations == 30


def test_no_route_in_the_schema_can_write_the_scientific_dag(client: TestClient) -> None:
    """The graph is published read-only, on both the versioned and legacy paths.

    A path containing `dag` is not merely read-only by convention: the only
    method the application registers on any of them is GET. The premium route
    package is also read directly, because a write could be reached from a
    non-DAG-looking path; the service that mutates nodes is not imported there
    at all.
    """
    document = client.app.openapi()  # type: ignore[attr-defined]
    for path, operations in document["paths"].items():
        written = {
            method
            for method, operation in operations.items()
            if isinstance(operation, dict)
        }
        if path.rstrip("/").endswith("/dag") or "/dag/" in path:
            assert written == {"get"}, (path, written)

    banned = ("DagRepository", "DagMutationService")
    premium = ROOT / "src" / "ravel" / "gateway" / "routes" / "premium"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in premium.rglob("*.py")
        if any(name in path.read_text(encoding="utf-8") for name in banned)
    ]
    assert offenders == []


def test_the_generated_client_reproduces_itself_byte_for_byte(tmp_path: Path) -> None:
    """Generation is deterministic, and the committed files are what it emits.

    The comparison is over every generated byte, not a parsed subset: a
    hand-edited type or a client method that drifted from the schema would
    survive a semantic check and fail this one. The headers say DO NOT EDIT and
    this is the test that makes the sentence true.
    """
    result = subprocess.run(
        [sys.executable, str(PACKAGE / "generate.py"), "--output", str(tmp_path)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    relative = [
        Path("openapi.json"),
        Path("src/types.ts"),
        Path("src/client.ts"),
        Path("src/index.ts"),
    ]
    for name in relative:
        committed = PACKAGE / name
        regenerated = tmp_path / name
        assert regenerated.exists(), f"the generator did not write {name}"
        # A checkout may rewrite line endings (CRLF on Windows). That is the
        # transport's EOL, not generator drift; the content still has to match
        # the generator's LF output byte for byte.
        committed_bytes = committed.read_bytes().replace(b"\r\n", b"\n")
        assert committed_bytes == regenerated.read_bytes(), (
            f"{name} is not what the generator emits; run "
            f"`.venv/bin/python packages/api-client/generate.py` and commit the result"
        )

    for name in relative[1:]:
        first = (PACKAGE / name).read_text(encoding="utf-8").splitlines()[0]
        assert "DO NOT EDIT" in first

    generated_schema = json.loads((tmp_path / "openapi.json").read_text(encoding="utf-8"))
    assert _versioned(generated_schema) == EXPECTED_V1
    assert not any(not path.startswith("/v1/") for path in generated_schema["paths"])

    client_source = (PACKAGE / "src/client.ts").read_text(encoding="utf-8")
    assert "export class V1Client" in client_source
    assert "export function openProjectEvents" in client_source
    assert 'code: "RESYNC_REQUIRED"' in client_source
    method_count = client_source.count("\n  async ")
    assert method_count == sum(len(methods) for methods in EXPECTED_V1.values())

    types_source = (PACKAGE / "src/types.ts").read_text(encoding="utf-8")
    for name in ("NodeView", "EvidenceSource", "MailDraft", "ProjectSummary"):
        assert f"export type {name} = " in types_source


def test_the_legacy_tui_surface_answers_as_before(
    client: TestClient, database: Database, project: Project
) -> None:
    """The routes the shipped TUI calls keep their shapes and their values.

    The legacy list is a bare array, the legacy projection has
    `last_event_seq`, the legacy DAG is a bare array, and the legacy socket's
    anchor describes the same head the versioned one does. `/v1` adds a prefix
    and an envelope; it does not replace the surface a client already speaks.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)

    legacy_login = client.post(
        "/auth/login", json={"username": "ada", "password": PASSWORD}
    )
    assert legacy_login.status_code == 200, legacy_login.text
    legacy_pair = legacy_login.json()
    versioned_login = client.post(
        "/v1/auth/login",
        json={"username": "ada", "password": PASSWORD},
    )
    assert versioned_login.status_code == 200
    assert set(versioned_login.json()) == set(legacy_pair)

    headers = bearer(legacy_pair["access_token"])
    listed = client.get("/projects", headers=headers)
    assert listed.status_code == 200
    assert isinstance(listed.json(), list)
    assert [item["project_id"] for item in listed.json()] == [project.project_id]

    projection = client.get(f"/projects/{project.project_id}", headers=headers)
    assert projection.status_code == 200
    assert projection.json()["last_event_seq"] == 3
    assert projection.json()["project"]["project_id"] == project.project_id

    legacy_dag = client.get(f"/projects/{project.project_id}/dag", headers=headers)
    assert legacy_dag.status_code == 200
    assert isinstance(legacy_dag.json(), list)
    versioned_dag = client.get(
        f"/v1/projects/{project.project_id}/dag", headers=headers
    )
    assert versioned_dag.status_code == 200
    assert versioned_dag.json()["items"] == legacy_dag.json()

    legacy_events = client.get(
        f"/projects/{project.project_id}/events?after_seq=0", headers=headers
    )
    assert legacy_events.status_code == 200
    assert set(legacy_events.json()) == {"events", "more"}
    assert [event["seq"] for event in legacy_events.json()["events"]] == [1, 2, 3]

    with client.websocket_connect(
        f"/projects/{project.project_id}/events/stream", headers=headers
    ) as legacy_socket:
        legacy_anchor = legacy_socket.receive_json()
    with client.websocket_connect(
        f"/v1/projects/{project.project_id}/events", headers=headers
    ) as versioned_socket:
        versioned_anchor = versioned_socket.receive_json()
    assert legacy_anchor == versioned_anchor
    assert legacy_anchor["head_seq"] == 3
