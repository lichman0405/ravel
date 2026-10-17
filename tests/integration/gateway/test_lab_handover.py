"""The half of the laboratory surface that only exists once work is handed over.

`test_lab.py` covers the screen a lab user reads before anything has been given
to a bench: the task, the contract, and the one report they can file. This file
covers what P11-06 added — the *package*, the upload that answers an output, and
the delivery that tells the run something happened — and it is the surface the
directive's chain names between the Worker and RAVEL's record:

    Master → Experiment Contract → LabPreparation → Experimental Worker →
    HumanLabBackend → LAB_USER → result / deviation → Review → Master

The lab user's own steps are the ones here. `tests/support/lab.py` builds the
package by the production path, so what these routes serve is what preparation
really wrote, not a directory the fixture made up.

Four claims, and the third is the one the directive names explicitly:

1. **A document is served from the package, by a name the manifest listed.** The
   protocol a bench worked from is the bytes RAVEL wrote and hashed, and a name
   the package does not hold is refused with the list of what it does.
2. **An upload answers an output, and only one the handover owes.** A file that
   answers nothing is refused *before a byte is stored*, with the owed list in
   the message — "不能上传一个任意文件就自动完成。必须根据 required_outputs 检查。"
3. **Uploading does not complete anything.** After every owed output has been
   recorded, the handover is still waiting: what finishes a run is the backend
   comparing what is recorded against what was owed, and that comparison is not
   this door's to make.
4. **Everything is recorded before anything is delivered, and a delivery that
   fails is reported rather than raised.** A lab user whose upload reached the
   database must not be told their upload failed because a scheduler was down.
"""

from __future__ import annotations

import hashlib
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests.integration.conftest import Prepared
from tests.integration.gateway.conftest import ScriptedDeliveries, account, bearer, sign_in
from tests.support.lab import (
    LOG_BYTES,
    OUTPUTS,
    PROCEDURE,
    RAW_BYTES,
    Handed,
    hand_over,
    terms,
)

from ravel.backends.lab import HumanLabBackend
from ravel.config import Settings
from ravel.domain.enums import JobState, UserRole
from ravel.domain.project import Project
from ravel.state.database import Database
from ravel.state.repositories.lab import LabHandoverRepository
from ravel.state.repositories.research import ArtifactRepository
from ravel.state.store import S3ArtifactStore

pytestmark = pytest.mark.integration

LAB = "lab"


@pytest.fixture
def handed(
    database: Database,
    gateway_settings: Settings,
    artifact_store: S3ArtifactStore,
    prepare: Callable[..., Prepared],
) -> Callable[..., Awaitable[Handed]]:
    """A prepared, handed-over, waiting laboratory run in the gateway's project.

    Built through the same production path the backend suite uses, in the same
    project the HTTP client is a member of, so an upload here is an upload
    against work that is really in somebody's hands.
    """

    async def make(**overrides: object) -> Handed:
        prepared = prepare(**terms(**overrides))
        return await hand_over(
            database=database,
            settings=gateway_settings,
            store=artifact_store,
            bench=HumanLabBackend(database=database),
            prepared=prepared,
        )

    return make


@pytest.fixture
def lab_user(database: Database, project: Project) -> str:
    """The person at the bench, as RAVEL knows them.

    A lab user rather than the project's owner, because that is who these routes
    exist for. Membership is all the route requires — the tests below assert no
    role check, since there is not one, and that is the design.
    """
    return account(database, username=LAB, role=UserRole.LAB_USER, project=project)


@pytest.fixture
def headers(client: TestClient, lab_user: str) -> dict[str, str]:
    """That person's token, as a client would send it."""
    return bearer(sign_in(client, LAB)["access_token"])


def task_url(project: Project, handed: Handed) -> str:
    return f"/projects/{project.project_id}/lab/tasks/{handed.node_id}"


def stored_handover(database: Database, handed: Handed):
    with database.read_only() as session:
        return LabHandoverRepository(session, handed.prepared.project_id).get(
            handover_id=handed.handover.handover_id
        )


# ── The package ─────────────────────────────────────────────────────────────


async def test_the_task_shows_the_package_the_bench_was_handed(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """The documents, the materializer that wrote them, and what is still owed.

    The package is read back from the preparation the handover names rather than
    re-rendered from the contract, so a materializer that changed tomorrow
    cannot change what a bench is shown today. What the view carries is what a
    person needs to work: the files, where they are, and the list of what is
    outstanding — with no second listing of versions and hashes, which is the
    artifact routes' business.
    """
    run = await handed()
    view = client.get(task_url(project, run), headers=headers).json()["handover"]

    assert view["handover"]["handover_id"] == run.handover.handover_id
    assert view["handover"]["state"] == JobState.WAITING_EXTERNAL.value
    assert view["handover"]["required_outputs"] == list(OUTPUTS)
    assert view["handover"]["protocol"] == run.handover.protocol

    package = view["package"]
    assert package["preparation_id"] == run.handover.preparation_id
    assert package["materializer"] == "bench-chemistry"
    assert package["workspace_path"] == str(run.workspace)

    # The manifest is what preparation recorded: which contract version it built
    # from, what it was asked to produce, and every file it wrote with its hash.
    # It carries no entrypoint — the file a bench starts from is a decision the
    # materializer made and the handover recorded, and the manifest's business is
    # what was built rather than what to do first.
    manifest = package["manifest"]
    assert manifest["execution_contract_version"] == run.prepared.contract.version
    assert manifest["required_outputs"] == list(OUTPUTS)
    assert run.handover.protocol in {
        entry["name"] for entry in manifest["generated_files"]
    }

    named = {document["name"] for document in package["documents"]}
    assert run.handover.protocol in named
    assert {"sample_manifest.csv", "measurement_plan.json"} <= named

    assert view["answered_outputs"] == []
    assert view["artifacts"] == {}
    assert view["missing_outputs"] == list(OUTPUTS)


async def test_the_names_the_package_lists_are_plain_file_names(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """The property that makes it safe for the document route to build a path.

    The route refuses a name the manifest does not hold and then joins the
    *manifest's* name to the workspace directory — which is only safe while no
    manifest name can mean anything but a file in that directory. That is
    enforced where the files are written (`Workspace._target` refuses a
    separator, a `..`, and a symlink at the target) and the names themselves are
    literals in `LabMaterializer`, never anything a contract supplied.

    Asserted here rather than assumed, because the document route's safety rests
    on it and the route cannot check it: by the time a name reaches the route it
    is a key into a stored manifest, and a manifest is bytes in a column. If a
    future materializer ever derived a file name from contract text, this is the
    test that fails — before the route that trusts it does.
    """
    run = await handed()
    package = client.get(task_url(project, run), headers=headers).json()["handover"]["package"]

    for document in package["documents"]:
        name = document["name"]
        assert Path(name).name == name, f"{name!r} is not a plain file name"
        assert name not in {".", ".."}, f"{name!r} is a directory, not a document"

    # And the workspace really holds them, so the above is about files that
    # exist rather than about strings that agree with themselves.
    for document in package["documents"]:
        assert (run.workspace / document["name"]).is_file()


async def test_a_document_is_served_from_the_package_the_manifest_listed(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """The protocol a bench worked from, as the bytes RAVEL wrote and hashed.

    Served with the hash the manifest recorded, so a lab user reading it can
    tell that what arrived is what was prepared — and as `inline`, because this
    is a document a person reads at a bench rather than a file to save.
    """
    run = await handed()
    response = client.get(
        f"{task_url(project, run)}/handover/documents/{run.handover.protocol}",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert PROCEDURE in response.text
    assert response.headers["content-type"].startswith("text/markdown")
    assert response.headers["content-disposition"] == (
        f'inline; filename="{run.handover.protocol}"'
    )

    on_disk = (run.workspace / run.handover.protocol).read_bytes()
    assert response.content == on_disk
    assert response.headers["x-ravel-content-hash"].startswith("sha256:")
    assert response.headers["x-ravel-content-hash"][len("sha256:") :] in {
        entry["sha256"]
        for entry in client.get(task_url(project, run), headers=headers).json()["handover"][
            "package"
        ]["documents"]
        if entry["name"] == run.handover.protocol
    }


async def test_a_document_the_package_does_not_hold_is_refused_with_what_it_does(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """404, and the message lists the package rather than saying "not found".

    A lab user who has been told to read a file the package does not hold needs
    to know which files it does hold; "not found" sends them looking for a typo.
    """
    run = await handed()
    refused = client.get(
        f"{task_url(project, run)}/handover/documents/secrets.txt", headers=headers
    )

    assert refused.status_code == 404
    assert "secrets.txt" in refused.json()["detail"]
    assert run.handover.protocol in refused.json()["detail"]


async def test_a_document_a_task_has_no_handover_for_is_refused(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """Nothing was handed over, so there is no package to read out of.

    Distinguished from "the package holds no such document", because the two
    send a reader to different places: one to the file name, the other to the
    run.
    """
    prepared = prepare(**terms())
    refused = client.get(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}"
        "/handover/documents/experimental_protocol.md",
        headers=headers,
    )

    assert refused.status_code == 404
    assert "handed to a bench" in refused.json()["detail"]


async def test_a_package_directory_that_is_gone_is_reported_as_gone(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """410 rather than 404, and the record is what makes that answerable.

    The manifest survives the directory: it is a row, and it still says which
    file was prepared and what it hashed to. So "this was prepared and the
    directory is no longer there" is a fact RAVEL can state, and it is a
    different fact from "no such document" — one is a lost workspace, the other
    is a wrong name.
    """
    run = await handed()
    # Hashed while the file is still there, so the claim below is about bytes
    # this test really held rather than about a value copied out of the response
    # it is checking.
    digest = hashlib.sha256((run.workspace / run.handover.protocol).read_bytes()).hexdigest()

    shutil.rmtree(run.workspace)

    gone = client.get(
        f"{task_url(project, run)}/handover/documents/{run.handover.protocol}",
        headers=headers,
    )
    assert gone.status_code == 410
    assert str(run.workspace) in gone.json()["detail"]

    # And the manifest is still readable, which is what the message promises:
    # the file's name and digest survive in a row even though its bytes are gone.
    view = client.get(task_url(project, run), headers=headers).json()["handover"]
    hashed = {
        entry["name"]: entry["sha256"]
        for entry in view["package"]["manifest"]["generated_files"]
    }
    assert hashed[run.handover.protocol] == digest


# ── The upload ──────────────────────────────────────────────────────────────


async def test_an_upload_answering_an_owed_output_is_recorded_and_tells_the_run(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    database: Database,
    lab_user: str,
    deliveries: ScriptedDeliveries,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """Who uploaded what, against which contract, and the signal that follows.

    The version is attributed to the signed-in person rather than to a role,
    because the person at the bench is the one holding the file. The signal goes
    to the node under the contract version in force — a node Master has revised
    runs again, and the run waiting is the one under the newest terms.
    """
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": "experiment_log", "filename": "run-17-log.csv"},
        content=LOG_BYTES,
        headers=headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["output"] == "experiment_log"
    assert body["answered_outputs"] == ["experiment_log"]
    assert body["missing_outputs"] == ["raw_data"]
    assert body["delivered_to_run"] is True
    assert body["delivery_note"] == ""

    assert body["artifact"]["node_id"] == run.node_id
    assert body["version"]["filename"] == "run-17-log.csv"
    assert body["version"]["media_type"] == "text/csv; charset=utf-8"
    assert body["version"]["created_by"] == lab_user, (
        "the upload is attributed to whoever sent it; the person at the bench is "
        "the one holding the file, and requiring the owner to relay it would "
        "mean recording that somebody else uploaded what they never saw"
    )
    assert body["version"]["execution_ref"] == (
        f"{run.prepared.contract.contract_id}.v{run.prepared.contract.version}"
    )

    assert len(deliveries.deliveries) == 1
    node_id, version, result = deliveries.deliveries[0]
    assert node_id == run.node_id
    assert version == run.prepared.contract.version
    assert result.payload == {"handover_id": run.handover.handover_id}
    assert result.delivered_outputs == ("experiment_log",)

    # What the caller was told is what the database holds.
    with database.read_only() as session:
        artifacts = ArtifactRepository(session, run.prepared.project_id).all(node_id=run.node_id)
    assert [artifact.artifact_id for artifact in artifacts] == [body["artifact"]["artifact_id"]]


async def test_an_upload_that_answers_nothing_is_refused_before_a_byte_is_stored(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    database: Database,
    deliveries: ScriptedDeliveries,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """The directive's rule, and the half of it that is easy to get wrong.

    "不能上传一个任意文件就自动完成。必须根据 required_outputs 检查。" Checking
    *after* storing would mean an arbitrary file is in the record and only its
    effect was prevented; checking first means the record never holds it. So two
    things are asserted: the refusal lists what is owed, and nothing was
    written.

    Nothing was delivered either, which is the other half of the same rule — a
    refused upload must not be able to move a run by the side door.
    """
    run = await handed()
    refused = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": "something_i_felt_like_sending"},
        content=b"not part of any contract",
        headers=headers,
    )

    assert refused.status_code == 409, refused.text
    detail = refused.json()["detail"]
    assert "something_i_felt_like_sending" in detail
    for owed in OUTPUTS:
        assert owed in detail, "the refusal has to say what is owed instead"

    with database.read_only() as session:
        artifacts = ArtifactRepository(session, run.prepared.project_id).all(node_id=run.node_id)
    assert artifacts == [], "a refused upload stored bytes anyway"

    assert deliveries.deliveries == [], "a refused upload told the run something"
    assert stored_handover(database, run).state is JobState.WAITING_EXTERNAL


async def test_every_owed_output_recorded_still_leaves_the_run_waiting(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    database: Database,
    deliveries: ScriptedDeliveries,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """Uploading is not completing, even when the last output arrives.

    This is the separation the directive's chain rests on. The route's job is to
    make the file a fact; whether the run is finished is the backend comparing
    the *recorded* names against what the handover owed, and a route that
    completed a run would be a route that decided a delivery was complete —
    which is not a claim a person's upload can make.

    Two deliveries reach the scripted port, and the handover stays where it is.
    """
    run = await handed()
    answers: list[dict[str, object]] = []
    for output, body in zip(OUTPUTS, (LOG_BYTES, RAW_BYTES), strict=True):
        response = client.post(
            f"{task_url(project, run)}/uploads",
            params={"output": output},
            content=body,
            headers=headers,
        )
        assert response.status_code == 201, response.text
        answers.append(response.json())

    # The first upload leaves the run owing the second, so the claim is about a
    # route that reports the outstanding list honestly as it shortens.
    assert answers[0]["missing_outputs"] == ["raw_data"]

    final = answers[-1]
    assert final["answered_outputs"] == list(OUTPUTS)
    assert final["missing_outputs"] == []
    assert len(deliveries.deliveries) == 2

    assert stored_handover(database, run).state is JobState.WAITING_EXTERNAL, (
        "the upload route completed a run; whether a delivery is complete is the "
        "backend's comparison, not this door's"
    )


async def test_a_corrected_file_is_a_new_version_of_the_same_output(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """A bench that sends a corrected file is doing the ordinary thing.

    Version two of the same output, with the first still readable — what a
    report or a decision may have cited is not erased by a later upload. The
    same bytes twice is one version, so a client that retries after a dropped
    response does not manufacture a correction.
    """
    run = await handed()
    url = f"{task_url(project, run)}/uploads"
    params = {"output": "experiment_log"}

    first = client.post(url, params=params, content=LOG_BYTES, headers=headers).json()
    retried = client.post(url, params=params, content=LOG_BYTES, headers=headers).json()
    assert retried["version"]["version"] == first["version"]["version"]

    corrected = client.post(
        url, params=params, content=LOG_BYTES + b"batch-19,2.1\n", headers=headers
    ).json()
    assert corrected["artifact"]["artifact_id"] == first["artifact"]["artifact_id"]
    assert corrected["version"]["version"] == first["version"]["version"] + 1


async def test_an_upload_with_no_handover_is_refused_with_why(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    prepare: Callable[..., Prepared],
) -> None:
    """Two refusals that are the same status and different sentences.

    A task nobody has been given is not a task whose handover ended, and a lab
    user reading the refusal has to be able to tell which they are looking at:
    one means "not yet", the other means "ask Master for a new run".
    """
    prepared = prepare(**terms())
    not_yet = client.post(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/uploads",
        params={"output": OUTPUTS[0]},
        content=LOG_BYTES,
        headers=headers,
    )
    assert not_yet.status_code == 409
    assert "has not been handed to a bench" in not_yet.json()["detail"]


async def test_an_upload_to_a_handover_that_has_ended_is_refused(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    database: Database,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """Withdrawal ends the door, and says which ending it was.

    A handover that was given up on still owes what it owed, so a file sent
    afterwards is not "an output this does not owe" — it is an answer to a
    question nobody is waiting for, and the refusal names the state it ended in.
    """
    run = await handed()
    HumanLabBackend(database=database).cancel(run.ref)
    assert stored_handover(database, run).state is JobState.CANCELLED

    refused = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": OUTPUTS[0]},
        content=LOG_BYTES,
        headers=headers,
    )
    assert refused.status_code == 409
    assert JobState.CANCELLED.value in refused.json()["detail"]


async def test_an_upload_the_run_could_not_be_told_about_is_still_recorded(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    database: Database,
    deliveries: ScriptedDeliveries,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """The ordering, which is the whole reason this is not a 5xx.

    Everything is recorded before anything is delivered, so by the time a
    delivery can fail the upload is already a fact. Telling the lab user their
    upload failed would be false; what is true is that it is on the record and
    the run has not heard — and the note says what that means for the wait.
    """
    run = await handed()
    deliveries.failure = OSError("the workflow service is not up")

    response = client.post(
        f"{task_url(project, run)}/uploads",
        params={"output": "experiment_log"},
        content=LOG_BYTES,
        headers=headers,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["delivered_to_run"] is False
    assert "the run was not told" in body["delivery_note"]
    assert body["answered_outputs"] == ["experiment_log"], (
        "an unreachable scheduler changed what the record says has arrived"
    )

    with database.read_only() as session:
        accepted = ArtifactRepository(session, run.prepared.project_id).all(node_id=run.node_id)
    assert len(accepted) == 1, "the upload was lost with the delivery"


# ── The report ──────────────────────────────────────────────────────────────


async def test_a_report_is_recorded_and_goes_to_the_run_as_a_question(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    deliveries: ScriptedDeliveries,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """The deviation travels, and what travels is its identifier.

    The record is already written and attributed to the person who wrote it; the
    Worker stops the run *against that record* rather than raising a second one
    for the same sentence. So the payload is an identifier and not the words,
    and the route still decides nothing: it neither permits the action nor moves
    the handover.
    """
    run = await handed()
    response = client.post(
        f"{task_url(project, run)}/deviations",
        headers=headers,
        json={
            "requested_action": "run_at_pressure",
            "description": "The furnace would not hold 900C, so the run was done at 850C.",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["delivered_to_run"] is True
    assert body["permitted"] is False
    assert body["resolved_by_decision_ref"] is None

    assert len(deliveries.deliveries) == 1
    node_id, version, result = deliveries.deliveries[0]
    assert node_id == run.node_id
    assert version == run.prepared.contract.version
    assert result.payload == {"deviation_id": body["deviation_id"]}
    assert body["description"] in result.detail


async def test_a_report_nothing_is_waiting_for_is_recorded_and_says_so(
    client: TestClient,
    headers: dict[str, str],
    project: Project,
    deliveries: ScriptedDeliveries,
    prepare: Callable[..., Prepared],
) -> None:
    """A report against a task nobody is running is still a report.

    Master reads open deviations on the next turn whether or not a bench is
    holding the work, so refusing to record it would lose the observation. What
    the caller is told is that nobody was told — and no delivery was attempted,
    which is the difference between "there was nothing to tell" and "telling
    failed".
    """
    prepared = prepare(**terms())
    response = client.post(
        f"/projects/{project.project_id}/lab/tasks/{prepared.node_id}/deviations",
        headers=headers,
        json={"requested_action": "run_at_pressure", "description": "It would not hold."},
    )

    assert response.status_code == 201, response.text
    assert response.json()["delivered_to_run"] is False
    assert "nothing to tell" in response.json()["delivery_note"]
    assert deliveries.deliveries == []


# ── Who may do it ───────────────────────────────────────────────────────────


async def test_a_member_of_another_project_cannot_read_or_upload(
    client: TestClient,
    database: Database,
    project: Project,
    other_project: Project,
    handed: Callable[..., Awaitable[Handed]],
) -> None:
    """404 rather than 403, on all three doors P11-06 added.

    The project a caller may not see must not be distinguishable from one that
    does not exist, and that has to hold for the document route and the upload
    door as well as for the task itself — a route added later that answered 403
    would leak the existence of a project to anybody who guessed an identifier.
    """
    run = await handed()
    account(database, username="bob", role=UserRole.PROJECT_OWNER, project=other_project)
    headers = bearer(sign_in(client, "bob")["access_token"])

    assert client.get(task_url(project, run), headers=headers).status_code == 404
    assert (
        client.get(
            f"{task_url(project, run)}/handover/documents/{run.handover.protocol}",
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{task_url(project, run)}/uploads",
            params={"output": OUTPUTS[0]},
            content=LOG_BYTES,
            headers=headers,
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"{task_url(project, run)}/deviations",
            headers=headers,
            json={"requested_action": "run_at_pressure", "description": "It would not hold."},
        ).status_code
        == 404
    )


async def test_no_token_is_refused(
    client: TestClient, project: Project, handed: Callable[..., Awaitable[Handed]]
) -> None:
    """The upload door is guarded like every other, and the store is not opened.

    Asserted on the upload rather than only on a read, because the door that
    writes is the one a missing guard would cost something at.
    """
    run = await handed()
    assert (
        client.post(
            f"{task_url(project, run)}/uploads",
            params={"output": OUTPUTS[0]},
            content=LOG_BYTES,
        ).status_code
        == 401
    )
