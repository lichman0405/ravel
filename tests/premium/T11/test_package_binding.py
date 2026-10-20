"""The package's terms, and the lab lead's confirmation.

Requirement 2: the package is bound to the protocol version, sample labels,
required raw files, device identity and existing SOP references, and it is not
ready for real execution until the lab lead confirms the SOP, site and device
suit it. Readiness is derived from a covering confirmation, never stored as a
flag a route could set.

The REAL handovers here are software proof only. T11-AC4 keeps real laboratory
acceptance in T26 and external; what these tests can show is that the backend
opens REAL only when both deployment permission and a covering confirmation
exist, and that an automated suite cannot manufacture either one's meaning.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from tests.support.lab import OUTPUTS, SAMPLES, Handed

from ravel.config import Settings
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.store import S3ArtifactStore

from .support import (
    DEVICE,
    SAMPLE_IDS,
    SITE,
    SOP_REF,
    STATEMENT,
    hand_over_live,
    terms_with_sop,
)


def task_url(project: Project, run: Handed) -> str:
    return f"/projects/{project.project_id}/lab/tasks/{run.node_id}"


def confirmation_body(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "protocol_version": 1,
        "sop_ref": SOP_REF,
        "site_ref": SITE,
        "device_identity": DEVICE,
        "statement": STATEMENT,
    }
    body.update(overrides)
    return body


async def test_the_package_is_bound_to_its_protocol_samples_raw_files_device_and_sop(
    client: TestClient,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    task = client.get(task_url(project, run), headers=lab_headers).json()

    readiness = task["package_readiness"]
    assert readiness["preparation_id"] == run.handover.preparation_id
    assert readiness["execution_contract_version"] == run.prepared.contract.version
    binding = readiness["binding"]
    assert binding["protocol_version"] == run.prepared.contract.version
    assert binding["sample_ids"] == list(SAMPLE_IDS)
    assert len(binding["sample_ids"]) == len(SAMPLES), (
        "the package bound a different number of samples than its manifest records"
    )
    assert binding["required_raw_files"] == list(OUTPUTS)
    assert binding["device_identity"] == DEVICE
    assert binding["sop_refs"] == [SOP_REF]
    assert binding["execution_contract_ref"] == run.prepared.contract.contract_id

    assert readiness["lead_confirmation"] is None
    assert readiness["confirmation_covers_package"] is False
    assert readiness["ready_for_real_execution"] is False
    assert task["handover"]["ready_for_real_execution"] is False
    assert task["handover"]["execution_mode"] == "SIMULATED"
    assert task["handover"]["evidence_execution_type"] == "SIMULATED"


async def test_a_package_without_a_covering_confirmation_is_not_ready_for_a_real_bench(
    client: TestClient,
    database: Database,
    project: Project,
    lab_headers: dict[str, str],
    prepare,
    gateway_settings: Settings,
    artifact_store: S3ArtifactStore,
) -> None:
    """Deployment permission alone must not open a real handover.

    `live_lab=True` is necessary and not sufficient: without a lead's covering
    confirmation the backend freezes the handover as SIMULATED, which is the
    only honest mode when nobody has confirmed the laboratory.
    """
    prepared = prepare(**terms_with_sop())
    run = await hand_over_live(
        database=database,
        settings=gateway_settings,
        store=artifact_store,
        prepared=prepared,
        confirm=False,
    )

    assert run.handover.execution_mode.value == "SIMULATED"
    assert run.handover.evidence_execution_type.value == "SIMULATED"
    assert run.handover.lead_confirmation_ref == ""

    task = client.get(task_url(project, run), headers=lab_headers).json()
    assert task["package_readiness"]["ready_for_real_execution"] is False
    assert task["handover"]["execution_mode"] == "SIMULATED"


async def test_the_lead_confirmation_is_recorded_and_readiness_is_derived(
    client: TestClient,
    database: Database,
    project: Project,
    owner_headers: dict[str, str],
    lab_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/lead-confirmation",
        headers=owner_headers,
        json=confirmation_body(protocol_version=run.prepared.contract.version),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    confirmation = body["confirmation"]
    assert confirmation["sop_ref"] == SOP_REF
    assert confirmation["site_ref"] == SITE
    assert confirmation["device_identity"] == DEVICE
    assert confirmation["statement"] == STATEMENT
    assert confirmation["protocol_version"] == run.prepared.contract.version
    assert body["covers_package"] is True
    assert body["ready_for_real_execution"] is True
    assert body["package_readiness"]["lead_confirmation"]["confirmation_id"] == (
        confirmation["confirmation_id"]
    )

    # The confirmation is about a package, not a handover: the SIMULATED
    # handover opened before it stays SIMULATED, because the mode is frozen at
    # the handover and is not a flag a later confirmation can rewrite.
    view = client.get(task_url(project, run), headers=lab_headers).json()["handover"]
    assert view["execution_mode"] == "SIMULATED"
    assert view["ready_for_real_execution"] is True

    again = client.post(
        f"{task_url(project, run)}/lead-confirmation",
        headers=owner_headers,
        json=confirmation_body(protocol_version=run.prepared.contract.version),
    )
    assert again.status_code == 201
    assert (
        again.json()["confirmation"]["confirmation_id"]
        == confirmation["confirmation_id"]
    ), "a retried confirmation created a second immutable record"


async def test_a_confirmation_for_another_device_sop_or_version_does_not_cover_the_package(
    client: TestClient,
    database: Database,
    project: Project,
    owner_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    url = f"{task_url(project, run)}/lead-confirmation"

    wrong_device = client.post(
        url,
        headers=owner_headers,
        json=confirmation_body(device_identity="a different instrument"),
    )
    assert wrong_device.status_code == 409
    assert "device" in wrong_device.json()["detail"]

    wrong_sop = client.post(
        url, headers=owner_headers, json=confirmation_body(sop_ref="SOP-SOMEWHERE-ELSE")
    )
    assert wrong_sop.status_code == 409
    assert "SOP" in wrong_sop.json()["detail"]

    wrong_version = client.post(
        url, headers=owner_headers, json=confirmation_body(protocol_version=99)
    )
    assert wrong_version.status_code == 409
    assert "prepared package" in wrong_version.json()["detail"]

    missing_preparation = client.post(
        url,
        headers=owner_headers,
        json=confirmation_body(preparation_id="no-such-preparation"),
    )
    assert missing_preparation.status_code == 404

    view = client.get(task_url(project, run), headers=owner_headers).json()
    assert view["package_readiness"]["lead_confirmation"] is None
    assert view["package_readiness"]["ready_for_real_execution"] is False
    assert view["handover"]["execution_mode"] == "SIMULATED"


async def test_only_the_project_owner_may_confirm_a_laboratory(
    client: TestClient,
    project: Project,
    lab_headers: dict[str, str],
    handed,
) -> None:
    run = await handed()
    refused = client.post(
        f"{task_url(project, run)}/lead-confirmation",
        headers=lab_headers,
        json=confirmation_body(protocol_version=run.prepared.contract.version),
    )
    assert refused.status_code == 403
    assert "project owner" in refused.json()["detail"]


async def test_a_live_deployment_with_a_covering_confirmation_opens_a_real_handover(
    database: Database,
    project: Project,
    prepare,
    gateway_settings: Settings,
    artifact_store: S3ArtifactStore,
) -> None:
    """The only path to REAL: deployment permission plus a covering confirmation.

    The evidence is still software proof. It says the backend's mode transition
    is derived from records, not that a laboratory exists.
    """
    prepared = prepare(**terms_with_sop())
    run = await hand_over_live(
        database=database,
        settings=gateway_settings,
        store=artifact_store,
        prepared=prepared,
        confirm=True,
    )

    assert run.handover.execution_mode.value == "REAL"
    assert run.handover.evidence_execution_type.value == "HUMAN_LAB"
    assert run.handover.lead_confirmation_ref, (
        "a REAL handover did not record which confirmation it was opened under"
    )
    assert run.handover.upload_provenance.startswith("lab-upload:")


async def test_automated_people_remain_software_proof_even_after_a_confirmation(
    client: TestClient,
    project: Project,
    owner_headers: dict[str, str],
    lab_headers: dict[str, str],
    handed,
) -> None:
    """T11-AC4: confirming a package does not turn an automated run into reality.

    The confirmation can make the *package* ready; it cannot make this test
    process a laboratory. The handover opened without a live bench stays
    SIMULATED, and every surface still reports real acceptance as
    BLOCKED_EXTERNAL in T26.
    """
    run = await handed()
    confirmed = client.post(
        f"{task_url(project, run)}/lead-confirmation",
        headers=owner_headers,
        json=confirmation_body(protocol_version=run.prepared.contract.version),
    )
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["ready_for_real_execution"] is True
    assert confirmed.json()["real_lab_acceptance"]["status"] == "BLOCKED_EXTERNAL"

    task = client.get(task_url(project, run), headers=lab_headers).json()
    assert task["handover"]["execution_mode"] == "SIMULATED"
    assert task["handover"]["evidence_execution_type"] == "SIMULATED"
    assert task["handover"]["real_lab_acceptance"] == {
        "status": "BLOCKED_EXTERNAL",
        "task": "T26",
        "reason": (
            "Real human-lab acceptance needs a real laboratory, a real operator "
            "and a real instrument. T11's automated people prove software handover "
            "only; a SIMULATED handover is not instrument evidence."
        ),
    }
    assert task["real_lab_acceptance"]["status"] == "BLOCKED_EXTERNAL"
