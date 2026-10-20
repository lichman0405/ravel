"""T13-AC2: keyset pagination with a stable cursor and the contract's ceiling.

The source ledger is used because its ordering key is visible in the response:
sources sort by URL and identifier, so a row can be inserted *between* two
pages at a chosen position. That is the case an offset cursor gets wrong — the
new row shifts every later page — and the case this test makes deterministic.

The limit cases assert the refusal as a refusal. A route that silently clamped
101 to 100 would leave a client believing it had read a whole collection, which
is the failure `CONTRACTS.md` §8 forbids by naming the maximum.
"""

from __future__ import annotations

import base64
from typing import Any

import pytest
from fastapi.testclient import TestClient
from tests.premium.T13.conftest import (
    a_source,
    account,
    bearer,
    sign_in,
)

from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.gateway.routes.premium.support import MAX_LIMIT
from ravel.state.database import Database

pytestmark = pytest.mark.integration


def _urls(page: dict[str, Any]) -> list[str]:
    return [item["url"] for item in page["items"]]


def _source_page(
    client: TestClient, project: Project, headers: dict[str, str], **params: Any
) -> dict[str, Any]:
    response = client.get(
        f"/v1/projects/{project.project_id}/sources", headers=headers, params=params
    )
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def test_a_keyset_cursor_does_not_shift_when_a_row_is_inserted_between_pages(
    client: TestClient, database: Database, project: Project
) -> None:
    """Page two is selected by the last key, so an insertion cannot move it.

    Five sources are written. The first page reads two. A sixth with a URL that
    sorts between the second and third is then written, and the second page is
    asked for with the cursor from the first. An offset-based page would start
    at row three *of the new ordering* and silently skip the insertion; this one
    starts after the URL it was given, so the insertion is the first thing it
    returns.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    for suffix in ("01", "02", "03", "04", "05"):
        a_source(
            database,
            project.project_id,
            url=f"https://example.org/{suffix}",
            tier=None,
        )

    first = _source_page(client, project, headers, limit=2)
    assert _urls(first) == ["https://example.org/01", "https://example.org/02"]
    assert first["has_more"] is True
    assert isinstance(first["next_cursor"], str)
    cursor = str(first["next_cursor"])

    a_source(
        database,
        project.project_id,
        url="https://example.org/02b",
        tier=None,
    )

    second = _source_page(client, project, headers, limit=2, cursor=cursor)
    assert _urls(second) == ["https://example.org/02b", "https://example.org/03"]
    assert second["has_more"] is True
    second_cursor = str(second["next_cursor"])

    # Asking again with the same cursor is the same page: the cursor is a key,
    # not a position that some other request can consume.
    repeated = _source_page(client, project, headers, limit=2, cursor=cursor)
    assert repeated == second

    third = _source_page(
        client, project, headers, limit=2, cursor=second_cursor
    )
    assert _urls(third) == ["https://example.org/04", "https://example.org/05"]
    assert third["has_more"] is False
    assert third["next_cursor"] is None

    walked = _urls(first) + _urls(second) + _urls(third)
    assert walked == [
        "https://example.org/01",
        "https://example.org/02",
        "https://example.org/02b",
        "https://example.org/03",
        "https://example.org/04",
        "https://example.org/05",
    ]
    assert len(walked) == len(set(walked))
    source_ids = [
        item["source_id"]
        for page in (first, second, third)
        for item in page["items"]
    ]
    assert len(source_ids) == len(set(source_ids))


def test_limit_above_the_contract_maximum_is_refused_and_the_maximum_is_accepted(
    client: TestClient, database: Database, project: Project
) -> None:
    """101 is the caller's mistake, 100 is the ceiling, and the cursor is checked.

    Each refusal is the v1 envelope with a stable code rather than FastAPI's
    default `{"detail": [...]}`: the generated client speaks one error shape.
    """
    account(database, username="ada", role=UserRole.PROJECT_OWNER, project=project)
    headers = bearer(sign_in(client, "ada")["access_token"])
    for suffix in ("01", "02", "03"):
        a_source(
            database,
            project.project_id,
            url=f"https://example.org/{suffix}",
            tier=None,
        )

    assert MAX_LIMIT == 100
    too_many = client.get(
        f"/v1/projects/{project.project_id}/sources",
        headers=headers,
        params={"limit": MAX_LIMIT + 1},
    )
    assert too_many.status_code == 422, too_many.text
    body = too_many.json()
    assert body["code"] == "INVALID_REQUEST"
    assert body["request_id"]
    assert body["details"]["errors"][0]["loc"] == ["query", "limit"]

    zero = client.get(
        f"/v1/projects/{project.project_id}/sources",
        headers=headers,
        params={"limit": 0},
    )
    assert zero.status_code == 422
    assert zero.json()["code"] == "INVALID_REQUEST"

    accepted = client.get(
        f"/v1/projects/{project.project_id}/sources",
        headers=headers,
        params={"limit": MAX_LIMIT},
    )
    assert accepted.status_code == 200, accepted.text
    assert len(accepted.json()["items"]) == 3
    assert accepted.json()["has_more"] is False

    malformed = client.get(
        f"/v1/projects/{project.project_id}/sources",
        headers=headers,
        params={"cursor": "!!!!"},
    )
    assert malformed.status_code == 400, malformed.text
    assert malformed.json()["code"] == "INVALID_REQUEST"
    assert malformed.json()["message"] == "the cursor is not one this endpoint issued"

    not_a_key = base64.urlsafe_b64encode(b'"a-string-not-a-key"').decode().rstrip("=")
    wrong_shape = client.get(
        f"/v1/projects/{project.project_id}/sources",
        headers=headers,
        params={"cursor": not_a_key},
    )
    assert wrong_shape.status_code == 400, wrong_shape.text
    assert wrong_shape.json()["code"] == "INVALID_REQUEST"
