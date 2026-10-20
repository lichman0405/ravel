"""The upload chain and the deviation record.

T11-AC1's other half: an unrequired output, a wrong contract version, a wrong
sample or a claimed delivery cannot complete a run. T11-AC2: a deviation keeps
its verbatim text, operator, time, file hash and sample label, and an upload is
a file rather than a scientific PASS.

Every acceptance and refusal here goes through the Gateway. The receipts are
read back from PostgreSQL, because a response that says "recorded" and a row
that does not exist would be the failure this suite is for.
"""

from __future__ import annotations

import hashlib

from fastapi.testclient import TestClient
from tests.support.lab import LOG_BYTES, OUTPUTS, RAW_BYTES, Handed

from ravel.backends.lab import HumanLabBackend
from ravel.domain.enums import JobState
from ravel.domain.project import Project
from ravel.execution.backends import ExternalDelivery
from ravel.state.database import Database
from ravel.state.repositories.records import DeviationRepository
from ravel.state.repositories.research import ArtifactRepository

from .support import SAMPLE_IDS, events_for, receipts_for, terms_with_sop


def task_url(project: Project, run: Handed) -> str:
    return f"/projects/{project.project_id}/lab/tasks/{run.node_id}"


async def test_an_accepted_upload_records_a_receipt_and_a_person_reported_event(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    lab_user: str,
    handed,
) -> None:
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/uploads",
        params={
            "output": OUTPUTS[0],
            "filename": "run-17-log.csv",
            "sample_id": SAMPLE_IDS[0],
        },
        content=LOG_BYTES,
        headers=lab_headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    digest = hashlib.sha256(LOG_BYTES).hexdigest()
    receipt = body["receipt"]
    assert receipt["outcome"] == "ACCEPTED"
    assert receipt["content_sha256"] == digest
    assert receipt["size_bytes"] == len(LOG_BYTES)
    assert receipt["artifact_id"] == body["artifact"]["artifact_id"]

    event = body["person_reported_event"]
    assert event["event_type"] == "OUTPUT_UPLOADED"
    assert event["person_reported"] is True
    assert event["actor_user_id"] == lab_user
    assert event["sample_id"] == SAMPLE_IDS[0]
    assert event["raw_output_ref"] == body["artifact"]["artifact_id"]
    assert event["raw_output_sha256"] == digest
    assert body["artifact"]["provenance"] == (
        f"lab-upload:{run.handover.handover_id}"
    ), "the upload no longer names the handover it answers"
    assert body["artifact"]["kind"] == "simulated", (
        "an automated stand-in's bytes were filed as real human-lab evidence"
    )
    assert body["scientific_verdict"] is None
    assert body["evidence_execution_type"] == "SIMULATED"

    stored_receipts = receipts_for(database, run.prepared, run.handover)
    assert [stored.outcome.value for stored in stored_receipts] == ["ACCEPTED"]
    assert stored_receipts[0].content_sha256 == digest
    assert [
        event.event_type.value for event in events_for(database, run.prepared, run.handover)
    ] == ["OUTPUT_UPLOADED"]

    view = client.get(task_url(project, run), headers=lab_headers).json()["handover"]
    assert view["handover"]["state"] == JobState.WAITING_EXTERNAL.value
    assert view["answered_outputs"] == [OUTPUTS[0]]
    assert view["upload_receipts"][0]["content_sha256"] == digest


async def test_an_unrequired_output_is_refused_and_leaves_a_rejection_receipt(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    deliveries,
    handed,
) -> None:
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": "an_extra_file", "filename": "extra.csv"},
        content=b"not asked for",
        headers=lab_headers,
    )

    assert response.status_code == 409
    assert "an_extra_file" in response.json()["detail"]
    assert all(output in response.json()["detail"] for output in OUTPUTS)

    receipts = receipts_for(database, run.prepared, run.handover)
    assert [receipt.outcome.value for receipt in receipts] == [
        "REJECTED_UNKNOWN_OUTPUT"
    ]
    assert receipts[0].artifact_id is None
    with database.read_only() as session:
        assert (
            ArtifactRepository(session, run.prepared.project_id).all(
                node_id=run.node_id
            )
            == []
        )
    assert deliveries.deliveries == []
    assert run.read_back(database).state is JobState.WAITING_EXTERNAL


async def test_a_wrong_contract_version_upload_is_recorded_and_cannot_complete_the_run(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    """T11-AC1: a file for another version is provenance, not a deliverable.

    The file is refused and receipted against the handover. The second output
    never arrives, so a delivery that *claims* both outputs leaves the run
    waiting — the claim is not consulted, the recorded artifacts are.
    """
    run = await handed()
    accepted = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": OUTPUTS[0]},
        content=LOG_BYTES,
        headers=lab_headers,
    )
    assert accepted.status_code == 201, accepted.text

    wrong = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": OUTPUTS[1], "protocol_version": 2},
        content=RAW_BYTES,
        headers=lab_headers,
    )
    assert wrong.status_code == 409
    assert "contract version 2" in wrong.json()["detail"]

    receipts = receipts_for(database, run.prepared, run.handover)
    assert [receipt.outcome.value for receipt in receipts] == [
        "ACCEPTED",
        "REJECTED_WRONG_CONTRACT_VERSION",
    ]
    assert receipts[1].content_sha256 == hashlib.sha256(RAW_BYTES).hexdigest(), (
        "the refused file's provenance was not kept in its receipt"
    )

    backend = HumanLabBackend(database=database)
    status = backend.deliver(
        run.ref,
        ExternalDelivery(
            summary="the bench says everything is here",
            delivered_outputs=OUTPUTS,
        ),
    )
    assert status.state is JobState.WAITING_EXTERNAL, (
        "a claimed delivery completed a run whose second output was only a "
        "rejected wrong-version upload"
    )
    assert OUTPUTS[1] in status.detail
    assert run.read_back(database).state is JobState.WAITING_EXTERNAL


async def test_an_upload_for_a_sample_the_package_never_bound_is_refused(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/uploads",
        params={
            "output": OUTPUTS[0],
            "sample_id": "a sample the package never named",
        },
        content=LOG_BYTES,
        headers=lab_headers,
    )

    assert response.status_code == 409
    assert "not one the package bound" in response.json()["detail"]
    receipts = receipts_for(database, run.prepared, run.handover)
    assert [receipt.outcome.value for receipt in receipts] == [
        "REJECTED_WRONG_SAMPLE"
    ]
    with database.read_only() as session:
        assert (
            ArtifactRepository(session, run.prepared.project_id).all(
                node_id=run.node_id
            )
            == []
        )


async def test_an_upload_is_a_file_not_a_scientific_verdict(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    """T11-AC2: completing the delivery chain still does not produce a PASS.

    The backend closes the handover because every required output has an
    artifact. What collection returns has no scientific verdict and no
    instrument state; the verdict is Review's, against frozen criteria.
    """
    run = await handed()
    hashes = {}
    for output, body in ((OUTPUTS[0], LOG_BYTES), (OUTPUTS[1], RAW_BYTES)):
        response = client.post(
            f"{task_url(project, run)}/uploads",
            params={"output": output},
            content=body,
            headers=lab_headers,
        )
        assert response.status_code == 201, response.text
        assert response.json()["scientific_verdict"] is None
        hashes[output] = hashlib.sha256(body).hexdigest()

    backend = HumanLabBackend(database=database)
    status = backend.deliver(
        run.ref,
        ExternalDelivery(summary="both files are in", delivered_outputs=OUTPUTS),
    )
    assert status.state is JobState.COMPLETED
    collected = backend.collect(run.ref)
    assert set(collected.delivered_outputs) == set(OUTPUTS)
    assert collected.completion_metadata["scientific_verdict"] is None
    assert collected.completion_metadata["instrument_state"] is None
    assert collected.completion_metadata["evidence_execution_type"] == "SIMULATED"
    raw_outputs = collected.completion_metadata["raw_outputs"]
    assert isinstance(raw_outputs, dict)
    assert {
        name: details["sha256"] for name, details in raw_outputs.items()
    } == hashes


async def test_a_deviation_keeps_verbatim_text_operator_time_hash_and_sample(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    lab_user: str,
    deliveries,
    handed,
) -> None:
    """T11-AC2, in the record rather than in the response alone."""
    run = await handed()
    description = (
        "The furnace would not hold 900C, so the run was done at 850C and the "
        "last two samples were held for 28 minutes instead of 30."
    )
    file_hash = "a" * 64
    response = client.post(
        f"{task_url(project, run)}/deviations",
        headers=lab_headers,
        json={
            "requested_action": "hold_at_900C",
            "description": description,
            "sample_id": SAMPLE_IDS[0],
            "raw_output_ref": "artifact-run-17-log",
            "raw_output_sha256": file_hash,
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["description"] == description
    assert body["raised_by"] == lab_user
    assert body["raised_at"]
    assert body["permitted"] is False

    event = body["person_reported_event"]
    assert event["event_type"] == "DEVIATION_REPORTED"
    assert event["person_reported"] is True
    assert event["actor_user_id"] == lab_user
    assert event["reported_at"]
    assert event["deviation_note"] == description
    assert event["sample_id"] == SAMPLE_IDS[0]
    assert event["raw_output_ref"] == "artifact-run-17-log"
    assert event["raw_output_sha256"] == file_hash

    with database.read_only() as session:
        stored = DeviationRepository(session, project.project_id).all(
            node_id=run.node_id
        )
    assert len(stored) == 1
    assert stored[0].description == description
    assert stored[0].raised_by == lab_user

    stored_events = events_for(database, run.prepared, run.handover)
    assert [event.event_type.value for event in stored_events] == [
        "DEVIATION_REPORTED"
    ]
    assert stored_events[0].deviation_note == description
    assert stored_events[0].raw_output_sha256 == file_hash

    assert body["delivered_to_run"] is True
    assert len(deliveries.deliveries) == 1
    _node, _version, result = deliveries.deliveries[0]
    assert result.payload == {"deviation_id": body["deviation_id"]}
    assert body["scientific_verdict"] is None


async def test_a_deviation_for_a_sample_the_package_never_bound_is_refused(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/deviations",
        headers=lab_headers,
        json={
            "requested_action": "run_at_pressure",
            "description": "It would not hold.",
            "sample_id": "an unknown sample",
        },
    )

    assert response.status_code == 409
    assert "not one the package bound" in response.json()["detail"]
    with database.read_only() as session:
        assert DeviationRepository(session, project.project_id).all(
            node_id=run.node_id
        ) == []
    assert events_for(database, run.prepared, run.handover) == ()


async def test_a_deviation_for_another_contract_version_is_refused(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/deviations",
        headers=lab_headers,
        json={
            "requested_action": "run_at_pressure",
            "description": "It would not hold.",
            "protocol_version": 2,
        },
    )

    assert response.status_code == 409
    assert "contract version 2" in response.json()["detail"]
    with database.read_only() as session:
        assert DeviationRepository(session, project.project_id).all(
            node_id=run.node_id
        ) == []


async def test_a_deviation_against_a_task_nobody_is_holding_is_still_recorded(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    prepare,
) -> None:
    """A report is an observation, not a handover event.

    Master reads open deviations on the next turn whether or not a bench holds
    the work. With no handover there is no person-reported lab event to attach
    it to, and the response says that rather than inventing one.
    """
    prepared = prepare(**terms_with_sop())
    response = client.post(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/deviations",
        headers=lab_headers,
        json={"requested_action": "run_at_pressure", "description": "It would not hold."},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["person_reported_event"] is None
    assert body["handover_id"] is None
    assert body["delivered_to_run"] is False
    assert "nothing to tell" in body["delivery_note"]
    with database.read_only() as session:
        stored = DeviationRepository(session, project.project_id).all(
            node_id=prepared.node_id
        )
    assert [record.description for record in stored] == ["It would not hold."]
