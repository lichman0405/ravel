"""T13-AC3: every versioned write, probed with an owner's credential.

The contract test reads the schema; this one uses it. A node is planned and
made runnable, the versioned DAG is read, every non-GET `/v1` operation is
addressed with the owner's token, and the graph is read again — over HTTP and
from PostgreSQL.

The probe asserts that it went somewhere: it must have attempted the
conversation, the mail draft, and at least one control route. A loop that
addressed nothing would satisfy "the DAG did not move" without testing anything,
which is exactly the failure `tests/support/routes.py` documents.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import Prepared
from tests.premium.T13.conftest import account, bearer, sign_in
from tests.premium.T13.support import EXPECTED_V1

from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.repositories.dag import DagRepository

pytestmark = pytest.mark.integration


def _address(path: str, project: Project, task_id: str) -> str:
    """The path a probe can request, with every placeholder the schema names."""
    values = {
        "project_id": project.project_id,
        "action_id": "an-action-that-does-not-exist",
        "task_id": task_id,
        "source_id": "a-source-that-does-not-exist",
        "evidence_id": "an-evidence-that-does-not-exist",
        "execution_id": "an-execution-that-does-not-exist",
        "document": "a-document-that-does-not-exist",
    }
    filled = path
    for name, value in values.items():
        filled = filled.replace("{" + name + "}", value)
    assert "{" not in filled, f"the probe cannot address {path!r}"
    return filled


def test_probing_every_v1_write_leaves_the_scientific_dag_unchanged(
    client: TestClient,
    database: Database,
    project: Project,
    prepare: Any,
) -> None:
    """An owner is the strongest human credential; even they cannot move a node.

    The DAG is Master's, mutated only through tools bound to a server-side
    session. Every `/v1` write returns a refusal, an accepted record, or the
    documented `UNSUPPORTED` for V0's absent sign-off — and none of them adds,
    cancels, re-binds or re-plans a node.
    """
    prepared: Prepared = prepare()
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])

    before_response = client.get(
        f"/v1/projects/{project.project_id}/dag", headers=headers
    )
    assert before_response.status_code == 200, before_response.text
    before = before_response.json()
    assert [node["node_id"] for node in before["items"]] == [prepared.node_id]

    probed: list[str] = []
    for path, methods in sorted(EXPECTED_V1.items()):
        if path.startswith("/v1/auth/"):
            continue
        for method in sorted(methods - {"get"}):
            target = _address(path, project, prepared.node_id)
            response = client.request(method, target, headers=headers, json={})
            assert response.status_code < 500 or response.status_code == 501, (
                f"{method} {target} failed with {response.status_code}: {response.text}"
            )
            probed.append(f"{method.upper()} {target}")

    after_response = client.get(
        f"/v1/projects/{project.project_id}/dag", headers=headers
    )
    assert after_response.status_code == 200, after_response.text
    after = after_response.json()
    assert after == before

    assert any("/conversation" in entry for entry in probed), probed
    assert any("/mail" in entry for entry in probed), probed
    assert any("/pause" in entry for entry in probed), probed
    assert "POST /v1/projects" in probed, probed
    assert any("lab/tasks" in entry for entry in probed), probed

    with database.read_only() as session:
        nodes = DagRepository(session, project.project_id).nodes()
    assert [node.node_id for node in nodes] == [prepared.node_id]
    assert nodes[0].status.value == "READY"
    assert nodes[0].objective == prepared.node.objective
