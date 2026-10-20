"""Production-path helpers for the T11 suite.

`hand_over_live` is the only thing here that is not a constant. It builds the
same package `hand_over` builds, then records the lab lead's confirmation
before submitting with `HumanLabBackend(live_lab=True)` — the one ordering that
can produce a REAL handover. The confirmation has to exist before submission
because the backend freezes the execution mode at the handover, which is the
property the suite is asserting rather than an inconvenience to route around.

Everything is prepared by `NodeRunActivities`, the same production path, so a
handover here is made from a package RAVEL really built and hashed.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from tests.integration.conftest import Prepared
from tests.integration.gateway.conftest import PASSWORD, account, bearer, sign_in
from tests.support.lab import (
    ACTOR,
    LIMITS,
    SAMPLES,
    Handed,
    hand_over,
    lab_materializers,
    registry_for,
    terms,
)

from ravel.backends.lab import HumanLabBackend
from ravel.config import Settings
from ravel.domain.enums import LabEventType, UserRole
from ravel.domain.lab import (
    LabEvent,
    LabHandover,
    LabLeadConfirmation,
    LabUploadReceipt,
)
from ravel.domain.project import Project
from ravel.execution.temporal.activities import NodeRunActivities, _request
from ravel.execution.temporal.contracts import RunInput
from ravel.gateway.auth.passwords import hash_password
from ravel.state.database import Database
from ravel.state.repositories.identity import (
    MembershipRepository,
    UserRepository,
)
from ravel.state.repositories.lab import (
    LabEventRepository,
    LabHandoverRepository,
    LabLeadConfirmationRepository,
    LabPackageRepository,
    LabUploadReceiptRepository,
)
from ravel.state.repositories.preparations import PreparationRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.store import S3ArtifactStore

#: The SOP the contract names, the instrument it names, and the site the lead
#: confirms. The instrument is already in `LIMITS`; the SOP is added here so
#: the binding has an existing reference a confirmation can be about.
SOP_REF = "SOP-TGA-014"
DEVICE = LIMITS["instrument"]
SITE = "lab-site-1"
STATEMENT = (
    "I confirm the TGA-2 at lab-site-1 is covered by SOP-TGA-014 for this "
    "package, and that the conditions and sample handling it states are the "
    "ones this laboratory will follow."
)

LIMITS_WITH_SOP = {**LIMITS, "sop_ref": SOP_REF}

#: The labels the laboratory materializer assigns by position; the manifest
#: records the samples in that order, so these are the package's sample IDs.
SAMPLE_IDS = tuple(f"S-{position}" for position in range(1, len(SAMPLES) + 1))


def terms_with_sop(**overrides: object) -> dict[str, object]:
    """The laboratory contract terms, with an SOP reference the lead can confirm."""
    return terms(resource_limits=LIMITS_WITH_SOP, **overrides)


@pytest.fixture
def project(database: Database) -> Project:
    """A project whose owner can sign in, which the lead-confirmation tests need.

    The integration `project` fixture creates its owner with a placeholder hash
    and never signs them in. T11 has to sign the owner in: the lab lead is the
    project owner, and a suite that could not authenticate as them could not
    exercise the confirmation or its refusals.
    """
    with database.transaction() as session:
        owner = UserRepository(session).create(
            username="owner", password_hash=hash_password(PASSWORD)
        )
        project = ProjectRegistry(session).create(
            title="Catalyst screen",
            objective="Find a better dopant.",
            created_by=owner.user_id,
        )
        MembershipRepository(session, project.project_id).grant(
            user_id=owner.user_id, role=UserRole.PROJECT_OWNER
        )
        return project


@pytest.fixture
def lab_user(database: Database, project: Project) -> str:
    """The person at the bench, as a real account in the project."""
    return account(database, username="lab", role=UserRole.LAB_USER, project=project)


@pytest.fixture
def lab_headers(client, lab_user: str) -> dict[str, str]:
    return bearer(sign_in(client, "lab")["access_token"])


@pytest.fixture
def owner_headers(client, project: Project) -> dict[str, str]:
    """The project owner, who is the lab lead in V0."""
    return bearer(sign_in(client, "owner")["access_token"])


@pytest.fixture
def handed(
    database: Database,
    gateway_settings: Settings,
    artifact_store: S3ArtifactStore,
    prepare,
    lab_user: str,
):
    """A prepared, handed-over, waiting run whose package names an SOP and device.

    SIMULATED, because that is what an automated suite can honestly open: the
    backend's `live_lab` defaults to false and there is no real laboratory in
    this test process. Tests that need a REAL handover build one explicitly and
    are labelled as software proof, not as instrument evidence.
    """

    async def make(**overrides: object) -> Handed:
        prepared = prepare(**terms_with_sop(**overrides))
        return await hand_over(
            database=database,
            settings=gateway_settings,
            store=artifact_store,
            bench=HumanLabBackend(database=database),
            prepared=prepared,
        )

    return make


async def hand_over_live(
    *,
    database: Database,
    settings: Settings,
    store: S3ArtifactStore,
    prepared: Prepared,
    confirm: bool = True,
) -> Handed:
    """Prepare, confirm and hand over a package to a deployment allowed a real bench.

    `confirm=False` is the refusal case: `live_lab` is true but nobody has
    confirmed the SOP, site and device, so the backend must open SIMULATED.
    """
    bench = HumanLabBackend(database=database, live_lab=True)
    activities = NodeRunActivities(
        settings=settings,
        database=database,
        registry=registry_for(bench),
        materializers=lab_materializers(),
        store=store,
    )
    plan = await activities.begin_node_run(
        RunInput(
            project_id=prepared.project_id,
            node_id=prepared.node_id,
            attempt=1,
            execution_contract_version=prepared.contract.version,
            actor_id=ACTOR,
        )
    )
    report = await activities.prepare_execution(plan)
    assert report.outcome.value == "PREPARED", (
        f"the package could not be built: {report.refusal} — {report.reason}"
    )
    with database.read_only() as session:
        package = PreparationRepository(
            session, prepared.project_id
        ).prepared_for_run(prepared.node_id, prepared.contract.version)
    assert package is not None

    with database.transaction() as session:
        binding = LabPackageRepository(
            session, prepared.project_id
        ).ensure_bound(package)
        if confirm:
            assert binding.sop_refs, "the package names no SOP for the lead to confirm"
            LabLeadConfirmationRepository(
                session, prepared.project_id
            ).record_once(
                LabLeadConfirmation(
                    project_id=prepared.project_id,
                    node_id=prepared.node_id,
                    preparation_id=package.preparation_id,
                    execution_contract_ref=package.execution_contract_ref,
                    execution_contract_version=package.execution_contract_version,
                    protocol_version=binding.protocol_version,
                    sop_ref=binding.sop_refs[0],
                    site_ref=SITE,
                    device_identity=binding.device_identity,
                    statement=STATEMENT,
                    confirmed_by="owner",
                )
            )

    request = _request(plan, attempt=1, prepared=package)
    handle = bench.submit(request)
    with database.read_only() as session:
        handover = LabHandoverRepository(session, prepared.project_id).for_attempt(
            prepared.node_id, 1, prepared.contract.version
        )
    assert handover is not None, "the submission recorded no handover"
    return Handed(
        prepared=prepared,
        report=report,
        plan=plan,
        request=request,
        handover=handover,
        ref=handle.backend_job_ref,
    )


def events_for(
    database: Database, prepared: Prepared, handover: LabHandover
) -> Sequence[LabEvent]:
    """Every person-reported event for one handover, in recorded order."""
    with database.read_only() as session:
        return tuple(
            LabEventRepository(
                session, prepared.project_id
            ).for_handover(handover.handover_id)
        )


def receipts_for(
    database: Database, prepared: Prepared, handover: LabHandover
) -> Sequence[LabUploadReceipt]:
    """Every upload attempt for one handover, accepted or refused."""
    with database.read_only() as session:
        return tuple(
            LabUploadReceiptRepository(
                session, prepared.project_id
            ).for_handover(handover.handover_id)
        )


def event_types(database: Database, prepared: Prepared, handover: LabHandover) -> list[str]:
    return [event.event_type.value for event in events_for(database, prepared, handover)]


def stop_confirmed(
    database: Database, prepared: Prepared, handover: LabHandover
) -> bool:
    return event_types(database, prepared, handover).count(
        LabEventType.STOP_CONFIRMED.value
    ) == 1


__all__ = [
    "DEVICE",
    "LIMITS_WITH_SOP",
    "SAMPLE_IDS",
    "SITE",
    "SOP_REF",
    "STATEMENT",
    "event_types",
    "events_for",
    "hand_over_live",
    "receipts_for",
    "stop_confirmed",
    "terms_with_sop",
]
