"""The database constraints behind the person-reported chain.

The routes are not the guarantee. A report that could be edited, a mode that
could be relabelled REAL after the fact, a second signature written around the
route, or a receipt that could be deleted would all make the audit trail a
story somebody could revise. These tests write the wide SQL the repositories
never write and assert PostgreSQL refuses it.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from tests.support.lab import OUTPUTS

from ravel.domain.enums import LabEventType
from ravel.domain.lab import LabEvent
from ravel.state.database import Database
from ravel.state.repositories.lab import (
    LabEventRepository,
    LabLeadConfirmationRepository,
    LabPackageRepository,
)
from ravel.state.repositories.research import ArtifactRepository

from .support import (
    DEVICE,
    SITE,
    SOP_REF,
    STATEMENT,
    events_for,
    receipts_for,
)


async def test_events_are_append_only(
    database: Database,
    project,
    handed,
) -> None:
    run = await handed()
    with database.transaction() as session:
        event = LabEventRepository(session, run.prepared.project_id).record(
            LabEvent(
                project_id=run.prepared.project_id,
                handover_id=run.handover.handover_id,
                actor_user_id="lab-1",
                protocol_version=run.prepared.contract.version,
                event_type=LabEventType.ACKNOWLEDGED,
            )
        )

    with (
        pytest.raises(IntegrityError, match="is append-only"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE lab_events SET deviation_note = 'rewritten' "
                "WHERE event_id = :event_id"
            ),
            {"event_id": event.event_id},
        )

    with (
        pytest.raises(IntegrityError, match="is append-only"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text("DELETE FROM lab_events WHERE event_id = :event_id"),
            {"event_id": event.event_id},
        )


async def test_a_person_reported_event_cannot_be_written_as_a_system_event(
    database: Database,
    project,
    handed,
) -> None:
    """The constant is in the schema, not only in the Pydantic model."""
    run = await handed()
    event = LabEvent(
        project_id=run.prepared.project_id,
        handover_id=run.handover.handover_id,
        actor_user_id="lab-1",
        protocol_version=run.prepared.contract.version,
        event_type=LabEventType.ACKNOWLEDGED,
    )

    with (
        pytest.raises(IntegrityError, match="events_are_person_reported"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "INSERT INTO lab_events (event_id, project_id, handover_id, "
                "actor_user_id, protocol_version, event_type, reported_at, "
                "person_reported) VALUES (:event_id, :project_id, :handover_id, "
                ":actor_user_id, :protocol_version, :event_type, now(), false)"
            ),
            {
                "event_id": event.event_id,
                "project_id": event.project_id,
                "handover_id": event.handover_id,
                "actor_user_id": event.actor_user_id,
                "protocol_version": event.protocol_version,
                "event_type": event.event_type.value,
            },
        )


async def test_a_signature_cannot_be_written_twice_around_the_route(
    database: Database,
    project,
    handed,
) -> None:
    """The partial unique index is the idempotency's foundation."""
    run = await handed()
    with database.transaction() as session:
        first = LabEventRepository(session, run.prepared.project_id).record(
            LabEvent(
                project_id=run.prepared.project_id,
                handover_id=run.handover.handover_id,
                actor_user_id="lab-1",
                protocol_version=run.prepared.contract.version,
                event_type=LabEventType.ACKNOWLEDGED,
            )
        )

    second = LabEvent(
        project_id=run.prepared.project_id,
        handover_id=run.handover.handover_id,
        actor_user_id="lab-2",
        protocol_version=run.prepared.contract.version,
        event_type=LabEventType.ACKNOWLEDGED,
    )
    with (
        pytest.raises(IntegrityError, match="uq_lab_events_acknowledged"),
        database.transaction() as session,
    ):
        LabEventRepository(session, run.prepared.project_id).record(second)

    assert [event.event_id for event in events_for(database, run.prepared, run.handover)] == [
        first.event_id
    ]


async def test_the_handover_mode_and_confirmation_are_immutable(
    database: Database,
    project,
    handed,
) -> None:
    """A SIMULATED handover cannot be relabelled REAL after the bench worked."""
    run = await handed()

    with (
        pytest.raises(IntegrityError, match="execution_mode is immutable"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE lab_handovers SET execution_mode = 'REAL' "
                "WHERE handover_id = :handover_id"
            ),
            {"handover_id": run.handover.handover_id},
        )

    with (
        pytest.raises(
            IntegrityError, match="lead_confirmation_ref is immutable"
        ),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE lab_handovers SET lead_confirmation_ref = 'invented' "
                "WHERE handover_id = :handover_id"
            ),
            {"handover_id": run.handover.handover_id},
        )


async def test_an_upload_receipt_cannot_be_edited_or_removed(
    client,
    database: Database,
    project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    """A refusal is history: the account of what was sent cannot be rewritten."""
    run = await handed()
    refused = client.post(
        f"/projects/{project.project_id}/lab/tasks/{run.node_id}/uploads",
        params={"output": "not_required"},
        content=b"refused bytes",
        headers=lab_headers,
    )
    assert refused.status_code == 409
    receipt = receipts_for(database, run.prepared, run.handover)[0]

    with (
        pytest.raises(IntegrityError, match="is append-only"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE lab_upload_receipts SET outcome = 'ACCEPTED' "
                "WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt.receipt_id},
        )

    with (
        pytest.raises(IntegrityError, match="is append-only"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "DELETE FROM lab_upload_receipts WHERE receipt_id = :receipt_id"
            ),
            {"receipt_id": receipt.receipt_id},
        )


async def test_the_package_binding_and_lead_confirmation_are_append_only(
    client,
    database: Database,
    project,
    owner_headers: dict[str, str],
    handed,
) -> None:
    """The terms a bench worked under and the lead's statement are history.

    Both records are written once, before or at the handover. A binding whose
    SOP could be edited after the run, or a confirmation whose site could be
    rewritten, would let the account of what the lead approved be made to agree
    with whatever the bench did.
    """
    run = await handed()
    confirmed = client.post(
        f"/projects/{project.project_id}/lab/tasks/{run.node_id}/lead-confirmation",
        headers=owner_headers,
        json={
            "protocol_version": run.prepared.contract.version,
            "sop_ref": SOP_REF,
            "site_ref": SITE,
            "device_identity": DEVICE,
            "statement": STATEMENT,
        },
    )
    assert confirmed.status_code == 201, confirmed.text

    with database.read_only() as session:
        binding = LabPackageRepository(
            session, project.project_id
        ).binding_for(run.handover.preparation_id)
        confirmation = LabLeadConfirmationRepository(
            session, project.project_id
        ).for_package(run.handover.preparation_id)
    assert binding is not None and confirmation is not None

    attempts = (
        (
            "UPDATE lab_package_bindings SET device_identity = 'another' "
            "WHERE binding_id = :identified",
            binding.binding_id,
        ),
        (
            "DELETE FROM lab_package_bindings WHERE binding_id = :identified",
            binding.binding_id,
        ),
        (
            "UPDATE lab_lead_confirmations SET site_ref = 'another' "
            "WHERE confirmation_id = :identified",
            confirmation.confirmation_id,
        ),
        (
            "DELETE FROM lab_lead_confirmations "
            "WHERE confirmation_id = :identified",
            confirmation.confirmation_id,
        ),
    )
    for statement, identifier in attempts:
        with (
            pytest.raises(IntegrityError, match="is append-only"),
            database.engine.begin() as connection,
        ):
            connection.execute(text(statement), {"identified": identifier})

    with database.read_only() as session:
        unchanged = LabPackageRepository(
            session, project.project_id
        ).binding_for(run.handover.preparation_id)
        still_there = LabLeadConfirmationRepository(
            session, project.project_id
        ).for_package(run.handover.preparation_id)
    assert unchanged is not None and unchanged.device_identity == DEVICE
    assert still_there is not None and still_there.site_ref == SITE


async def test_a_refused_upload_stored_no_artifact(
    client,
    database: Database,
    project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    """The receipt is history; the bytes it refused are not in the record."""
    run = await handed()
    client.post(
        f"/projects/{project.project_id}/lab/tasks/{run.node_id}/uploads",
        params={"output": OUTPUTS[0]},
        content=b"this one is accepted",
        headers=lab_headers,
    )
    client.post(
        f"/projects/{project.project_id}/lab/tasks/{run.node_id}/uploads",
        params={"output": "not_required"},
        content=b"this one is refused",
        headers=lab_headers,
    )

    with database.read_only() as session:
        artifacts = ArtifactRepository(session, run.prepared.project_id).all(
            node_id=run.node_id
        )
    assert [artifact.name for artifact in artifacts] == [OUTPUTS[0]]
    assert len(receipts_for(database, run.prepared, run.handover)) == 2


async def test_the_deviation_link_is_append_only(
    client,
    database: Database,
    project,
    lab_headers: dict[str, str],
    deliveries,
    handed,
) -> None:
    """The join between a deviation and its event cannot be rewritten.

    Inferred provenance could be re-derived differently tomorrow. An explicit
    link is useful only if it is immutable: changing which event answered a
    deviation would let the account of what the bench reported be reassigned.
    """
    run = await handed()
    reported = client.post(
        f"/projects/{project.project_id}/lab/tasks/{run.node_id}/deviations",
        headers=lab_headers,
        json={
            "requested_action": "run_at_pressure",
            "description": "It would not hold.",
            "sample_id": "S-1",
            "raw_output_sha256": "c" * 64,
        },
    )
    assert reported.status_code == 201, reported.text
    link_id = reported.json()["deviation_event_link"]["link_id"]

    with (
        pytest.raises(IntegrityError, match="is append-only"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text(
                "UPDATE lab_deviation_links SET event_id = 'another' "
                "WHERE link_id = :link_id"
            ),
            {"link_id": link_id},
        )
    with (
        pytest.raises(IntegrityError, match="is append-only"),
        database.engine.begin() as connection,
    ):
        connection.execute(
            text("DELETE FROM lab_deviation_links WHERE link_id = :link_id"),
            {"link_id": link_id},
        )
