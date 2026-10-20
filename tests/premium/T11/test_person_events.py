"""The bench's own statements: sign-off, start and stop confirmation.

T11-AC3 is the pivot. RAVEL cannot see a bench, so a handover stays
`WAITING_EXTERNAL` until something ends it; a start report is a person's
statement and never a `RUNNING` state. T11 requirement 4's operational edge is
here too: a system cancellation revokes receiving eligibility, says it is
awaiting a manual stop confirmation, and never writes the person's
`STOP_CONFIRMED`.

Every event asserted below is person-reported by construction. A test that
found a system-authored event in `lab_events` would be finding the bug this
file exists to prevent.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.integration.gateway.conftest import account, bearer, sign_in
from tests.support.lab import OUTPUTS, Handed

from ravel.backends.lab import HumanLabBackend
from ravel.domain.enums import JobState, UserRole
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.repositories.research import ArtifactRepository

from .support import events_for, receipts_for, stop_confirmed


def task_url(project: Project, run: Handed) -> str:
    return f"/projects/{project.project_id}/lab/tasks/{run.node_id}"


async def test_sign_off_is_person_reported_and_leaves_the_run_waiting(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    lab_user: str,
    handed,
) -> None:
    """T11-AC3: a signature is a human record, not a state called RUNNING.

    The event names the person, the handover and the contract version, and it
    says `person_reported` true in the record itself. The handover keeps the
    only state a bench can honestly be in while a person works: waiting.
    """
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/sign-off",
        headers=lab_headers,
        json={"protocol_version": run.prepared.contract.version},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    event = body["person_reported_event"]
    assert event["event_type"] == "ACKNOWLEDGED"
    assert event["person_reported"] is True
    assert event["actor_user_id"] == lab_user
    assert event["handover_id"] == run.handover.handover_id
    assert event["protocol_version"] == run.prepared.contract.version
    assert body["handover_state"] == JobState.WAITING_EXTERNAL.value
    assert body["instrument_state"] is None
    assert body["scientific_verdict"] is None

    view = client.get(task_url(project, run), headers=lab_headers).json()["handover"]
    assert view["handover"]["state"] == JobState.WAITING_EXTERNAL.value
    assert [
        recorded["event_type"] for recorded in view["person_reported_events"]
    ] == ["ACKNOWLEDGED"]

    stored = events_for(database, run.prepared, run.handover)
    assert len(stored) == 1
    assert stored[0].person_reported is True
    assert stored[0].event_type.value == "ACKNOWLEDGED"


async def test_sign_off_is_idempotent_because_a_retry_is_not_a_second_signature(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    url = f"{task_url(project, run)}/sign-off"

    first = client.post(url, headers=lab_headers)
    second = client.post(url, headers=lab_headers)

    assert first.status_code == second.status_code == 201, second.text
    assert (
        first.json()["person_reported_event"]["event_id"]
        == second.json()["person_reported_event"]["event_id"]
    ), "a retried sign-off created a second signature"
    assert len(events_for(database, run.prepared, run.handover)) == 1


async def test_start_requires_the_persons_sign_off_and_never_reports_running(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    """The order is part of the record, and the state never becomes RUNNING.

    A start without a sign-off would be work begun under terms nobody accepted.
    After the sign-off, the start is still an event beside a waiting handover:
    RAVEL does not get a `RUNNING` out of a person saying they began.
    """
    run = await handed()
    start_url = f"{task_url(project, run)}/start"

    refused = client.post(start_url, headers=lab_headers)
    assert refused.status_code == 409
    assert "signed" in refused.json()["detail"]
    assert events_for(database, run.prepared, run.handover) == ()

    client.post(f"{task_url(project, run)}/sign-off", headers=lab_headers)
    started = client.post(start_url, headers=lab_headers)

    assert started.status_code == 201, started.text
    body = started.json()
    assert body["person_reported_event"]["event_type"] == "START_REPORTED"
    assert body["handover_state"] == JobState.WAITING_EXTERNAL.value

    stored = run.read_back(database)
    assert stored.state is JobState.WAITING_EXTERNAL, (
        "a person's start report was presented as an observed RUNNING state"
    )
    assert [
        event.event_type.value
        for event in events_for(database, run.prepared, run.handover)
    ] == ["ACKNOWLEDGED", "START_REPORTED"]

    again = client.post(start_url, headers=lab_headers)
    assert again.status_code == 201
    assert (
        again.json()["person_reported_event"]["event_id"]
        == body["person_reported_event"]["event_id"]
    )


async def test_only_a_lab_user_reports_a_bench_event(
    client: TestClient,
    database: Database,
    project: Project,
    owner_headers: dict[str, str],
    handed,
) -> None:
    """T11-AC1's wrong-role half: an owner cannot sign for an operator.

    All five person-reported doors are refused before anything is written. The
    owner may intervene, approve and read, but a sign-off or a stop they wrote
    would make the record a claim about somebody else's actions.
    """
    run = await handed()
    base = task_url(project, run)

    assert client.post(f"{base}/sign-off", headers=owner_headers).status_code == 403
    assert client.post(f"{base}/start", headers=owner_headers).status_code == 403
    assert (
        client.post(f"{base}/stop-confirmation", headers=owner_headers).status_code
        == 403
    )
    assert (
        client.post(
            f"{base}/deviations",
            headers=owner_headers,
            json={
                "requested_action": "run_at_pressure",
                "description": "not mine to say",
            },
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{base}/uploads",
            params={"output": OUTPUTS[0]},
            content=b"owner pretending to be the bench",
            headers=owner_headers,
        ).status_code
        == 403
    )

    assert events_for(database, run.prepared, run.handover) == ()
    assert receipts_for(database, run.prepared, run.handover) == ()
    assert run.read_back(database).state is JobState.WAITING_EXTERNAL


async def test_a_foreign_project_member_cannot_reach_any_human_door(
    client: TestClient,
    database: Database,
    project: Project,
    other_project: Project,
    handed,
) -> None:
    """404 rather than 403, so a project's existence is not leaked."""
    run = await handed()
    account(
        database,
        username="bob",
        role=UserRole.PROJECT_OWNER,
        project=other_project,
    )
    headers = bearer(sign_in(client, "bob")["access_token"])
    base = task_url(project, run)

    assert client.post(f"{base}/sign-off", headers=headers).status_code == 404
    assert client.post(f"{base}/start", headers=headers).status_code == 404
    assert (
        client.post(f"{base}/stop-confirmation", headers=headers).status_code == 404
    )
    assert (
        client.post(
            f"{base}/lead-confirmation",
            headers=headers,
            json={
                "protocol_version": run.prepared.contract.version,
                "sop_ref": "SOP-SOMEWHERE-ELSE",
                "site_ref": "site-elsewhere",
                "device_identity": "instrument-elsewhere",
                "statement": "not my project's package",
            },
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{base}/deviations",
            headers=headers,
            json={"requested_action": "x", "description": "y"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{base}/uploads",
            params={"output": OUTPUTS[0]},
            content=b"foreign project",
            headers=headers,
        ).status_code
        == 404
    )


async def test_a_system_cancel_awaits_a_manual_stop_and_never_claims_the_instrument_stopped(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    """Requirement 4, in one case.

    Withdrawal revokes handover and receiving eligibility. It does not write a
    stop event, does not claim the instrument stopped, and shows the bench as
    awaiting a person's confirmation. The late upload is refused and recorded
    as a receipt; the person's later stop confirmation is the only thing that
    clears the awaiting flag, and even that does not move the handover.
    """
    run = await handed()
    assert HumanLabBackend(database=database).cancel(run.ref) is True
    assert run.read_back(database).state is JobState.CANCELLED

    view = client.get(task_url(project, run), headers=lab_headers).json()["handover"]
    assert view["handover"]["state"] == JobState.CANCELLED.value
    assert view["awaiting_manual_stop_confirmation"] is True
    assert view["person_reported_events"] == []
    assert "does not claim the instrument stopped" in view["handover"]["detail"]
    assert not stop_confirmed(database, run.prepared, run.handover), (
        "a system cancellation fabricated a person's STOP_CONFIRMED event"
    )

    late = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": OUTPUTS[0], "filename": "too-late.csv"},
        content=b"the bench kept working",
        headers=lab_headers,
    )
    assert late.status_code == 409, late.text
    assert "CANCELLED" in late.json()["detail"]
    receipts = receipts_for(database, run.prepared, run.handover)
    assert [receipt.outcome.value for receipt in receipts] == ["REJECTED_LATE"]
    with database.read_only() as session:
        assert (
            ArtifactRepository(session, run.prepared.project_id).all(
                node_id=run.node_id
            )
            == []
        )

    confirmed = client.post(
        f"{task_url(project, run)}/stop-confirmation",
        headers=lab_headers,
        json={"protocol_version": run.prepared.contract.version},
    )
    assert confirmed.status_code == 201, confirmed.text
    assert (
        confirmed.json()["person_reported_event"]["event_type"]
        == "STOP_CONFIRMED"
    )
    assert confirmed.json()["handover_state"] == JobState.CANCELLED.value
    assert confirmed.json()["instrument_state"] is None

    after = client.get(task_url(project, run), headers=lab_headers).json()["handover"]
    assert after["awaiting_manual_stop_confirmation"] is False
    assert run.read_back(database).state is JobState.CANCELLED, (
        "the person's stop confirmation changed the handover's cancelled state"
    )
