"""T19-AC3: no authoritative route, redacted records, a bound Action, a bound project.

The command sandbox has no network at all, so the authoritative database and a
cluster are unreachable by construction. This module makes that concrete with
the PostgreSQL port the test suite is actually using, and checks the other half
of the claim: the sandbox does not carry a database or cluster credential and
its records never contain one.

The Action cases run against the real PostgreSQL. A sandbox command is only
allowed under a claimed `SANDBOX_EXEC` Action whose digest, binding, actor, and
project agree with the caller; each disagreement is exercised separately, and
none of them reaches the supervisor.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import pytest
from tests.premium.T19.support import (
    SECRET_ENV_NAME,
    Deployment,
    execute_command,
    handle_record,
    python_argv,
    read_json_line,
    run_record,
    run_records_for_handle,
)

from ravel.actions.service import ActionService
from ravel.domain.enums import ActionStatus
from ravel.sandbox.action_guard import DatabaseActionGuard, sandbox_command_payload
from ravel.sandbox.models import SandboxForbidden, SandboxValidationError

pytestmark = pytest.mark.integration

ARGV = ["/bin/echo", "guarded-sandbox"]
ACTOR = "experimental-worker"


def _claimed_action(
    database: Any,
    settings: Any,
    project_id: str,
    *,
    argv: list[str] | tuple[str, ...] = ARGV,
    actor: str = ACTOR,
    binding: str = "db-binding",
    claim: bool = True,
) -> Any:
    payload = sandbox_command_payload(argv=argv, cwd="", timeout_seconds=30)
    with database.transaction() as session:
        service = ActionService(session, project_id, settings=settings)
        action = service.create(
            kind="SANDBOX_EXEC",
            payload=payload,
            actor=actor,
            requesting_binding=binding,
            target_ref="sandbox:docker",
            estimated_cny="0",
            idempotency_key=f"t19-{uuid4().hex}",
        )
        assert action.status is ActionStatus.READY
        if claim:
            claimed, won = service.claim(action.action_id, claimed_by=actor)
            assert won is True
            assert claimed.status is ActionStatus.CLAIMED
        return action.action_id


def test_command_sandbox_has_no_route_or_credential_for_database_or_cluster(
    deployment: Deployment,
    integration_settings: Any,
) -> None:
    """The real PostgreSQL endpoint and a cluster endpoint are both unreachable."""
    project_id = deployment.project_id("ac3-no-route")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    host = str(integration_settings.postgres_host)
    port = int(integration_settings.postgres_port)

    code = r"""
import json, os, socket

targets = {
    "authoritative_pg": (os.environ["T19_PG_HOST"], int(os.environ["T19_PG_PORT"])),
    "docker_gateway_pg": ("172.17.0.1", int(os.environ["T19_PG_PORT"])),
    "cluster_ssh": ("172.17.0.1", 22),
    "slurmctld": ("172.17.0.1", 6817),
    "metadata": ("169.254.169.254", 80),
}
results = {}
for name, (host, port) in targets.items():
    try:
        connection = socket.create_connection((host, port), timeout=1)
        connection.close()
        results[name] = "connected"
    except OSError as exc:
        results[name] = type(exc).__name__

environment = dict(os.environ)
ssh_paths = {
    "/root/.ssh": os.path.exists("/root/.ssh"),
    "/home/nobody/.ssh": os.path.exists("/home/nobody/.ssh"),
}
print(json.dumps({
    "results": results,
    "env_names": sorted(environment),
    "ssh_paths": ssh_paths,
    "routes": open("/proc/net/route").read().splitlines()[1:],
}))
"""
    # The addresses are compiled into the helper: the supervisor's per-exec
    # environment accepts only its allowlisted names, and the sandbox must not
    # receive the database DSN or credentials at all.
    code_with_target = code.replace(
        'os.environ["T19_PG_HOST"]', json.dumps(host)
    ).replace('os.environ["T19_PG_PORT"]', str(port))
    provider, run = execute_command(
        deployment, handle, authority, python_argv(code_with_target), timeout=30
    )
    status = provider.status(run)
    assert status.state.value == "SUCCEEDED", (status.detail, status.stderr)
    payload = read_json_line(status.stdout)
    for name, result in payload["results"].items():
        assert result != "connected", (name, payload)
    assert payload["routes"] == []
    assert payload["ssh_paths"] == {"/root/.ssh": False, "/home/nobody/.ssh": False}
    names = set(payload["env_names"])
    forbidden_prefixes = ("RAVEL_POSTGRES", "PG", "SLURM", "HPC", "SSH")
    assert not {name for name in names if name.startswith(forbidden_prefixes)}


def test_secrets_are_redacted_from_argv_output_records_and_logs(
    deployment: Deployment,
) -> None:
    """The deployment token, injected secret, URL password, and bearer do not persist."""
    project_id = deployment.project_id("ac3-redaction")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)

    code = r"""
import sys
for value in sys.argv[1:]:
    print("value=" + value)
print("env=" + __import__("os").environ["T19_TEST_SECRET"])
"""
    argv = [
        *python_argv(code),
        deployment.env_secret,
        deployment.token,
        "postgresql://alice:hunter2@db.example.test/ravel",
        "Authorization: Bearer abcdef1234567890",
    ]
    runner, run = execute_command(
        deployment,
        handle,
        authority,
        argv,
        env_refs=(SECRET_ENV_NAME,),
        timeout=30,
    )
    status = runner.status(run)
    assert status.state.value == "SUCCEEDED", (status.detail, status.stderr)
    for forbidden in (
        deployment.token,
        deployment.env_secret,
        "hunter2",
        "abcdef1234567890",
    ):
        assert forbidden not in status.stdout, forbidden
    assert "[REDACTED]" in status.stdout

    raw = (deployment.state_dir / "runs" / f"{run.run_id}.json").read_text("utf-8")
    for forbidden in (
        deployment.token,
        deployment.env_secret,
        "hunter2",
        "abcdef1234567890",
    ):
        assert forbidden not in raw, forbidden
    stored = run_record(deployment, run.run_id)
    assert any("[REDACTED]" in part for part in stored["command"])
    assert deployment.token not in json.dumps(stored)
    assert deployment.env_secret not in json.dumps(stored)

    logs = runner.logs(handle)
    assert deployment.token not in logs
    assert deployment.env_secret not in logs
    handle_text = json.dumps(handle_record(deployment, handle.handle_id))
    assert deployment.token not in handle_text
    assert deployment.env_secret not in handle_text


def test_reconnect_keeps_the_project_binding_and_refuses_a_wrong_epoch(
    deployment: Deployment,
) -> None:
    """A handle is reused only by its project under the lease it was made for."""
    project_id = deployment.project_id("ac3-reconnect")
    authority = deployment.authority(project_id, binding="lease-binding", epoch=5)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    assert handle.epoch == 5

    wrong_epoch = deployment.authority(
        project_id, binding="lease-binding", epoch=6
    )
    with pytest.raises(SandboxValidationError, match="epoch"):
        execute_command(
            deployment, handle, wrong_epoch, ["/bin/echo", "wrong-epoch"], timeout=20
        )

    other_project = deployment.project_id("ac3-other")
    other_authority = deployment.authority(other_project, binding="other-binding")
    other_provider = deployment.provider(other_project)
    with pytest.raises(SandboxForbidden):
        other_provider.reconnect(handle, other_authority)

    stopped = provider.stop(handle, authority)
    assert stopped.state.value == "STOPPED"
    with pytest.raises(SandboxValidationError, match="epoch"):
        provider.reconnect(handle, wrong_epoch)
    reconnected = provider.reconnect(handle, authority)
    assert reconnected.state.value == "READY"
    assert reconnected.project_id == project_id
    record = handle_record(deployment, handle.handle_id)
    assert record["binding_id"] == "lease-binding"
    assert record["handle"]["epoch"] == 5
    provider.destroy(reconnected, authority)


def test_database_action_guard_runs_only_the_claimed_digest(
    deployment: Deployment,
    database: Any,
    project: Any,
    integration_settings: Any,
) -> None:
    """A claimed, matching SANDBOX_EXEC is the one way in; the Action completes."""
    project_id = project.project_id
    authority = deployment.authority(project_id, binding="db-binding")
    action_id = _claimed_action(database, integration_settings, project_id)
    guard = DatabaseActionGuard(database, project_id)
    provider = deployment.provider(project_id, guard=guard)
    handle = provider.provision(project_id, authority)

    run = provider.execute(
        handle,
        ARGV,
        timeout=30,
        action_id=action_id,
        authority=authority,
    )
    assert run.state.value == "SUCCEEDED"
    status = provider.status(run)
    assert status.state.value == "SUCCEEDED"
    assert "guarded-sandbox" in status.stdout

    with database.read_only() as session:
        stored = ActionService(
            session, project_id, settings=integration_settings
        ).get(action_id)
    assert stored.status is ActionStatus.COMPLETED
    assert stored.claimed_by == ACTOR
    assert stored.external_ref == run.run_id
    provider.destroy(handle, authority)


def test_database_action_guard_refuses_every_mismatch_before_the_supervisor(
    deployment: Deployment,
    database: Any,
    project: Any,
    integration_settings: Any,
) -> None:
    """Unclaimed, wrong digest, wrong binding, wrong actor, and wrong project all fail."""
    project_id = project.project_id
    authority = deployment.authority(project_id, binding="db-binding")
    guard = DatabaseActionGuard(database, project_id)
    provider = deployment.provider(project_id, guard=guard)
    handle = provider.provision(project_id, authority)

    def refusals_before(action_id: str, argv: list[str], caller: Any) -> None:
        before = len(run_records_for_handle(deployment, handle.handle_id))
        with pytest.raises(SandboxForbidden):
            provider.execute(
                handle,
                argv,
                timeout=30,
                action_id=action_id,
                authority=caller,
            )
        after = len(run_records_for_handle(deployment, handle.handle_id))
        assert after == before, "a refused command reached the supervisor"

    unclaimed = _claimed_action(
        database, integration_settings, project_id, claim=False
    )
    refusals_before(unclaimed, ARGV, authority)

    wrong_command = _claimed_action(database, integration_settings, project_id)
    refusals_before(wrong_command, ["/bin/echo", "different-command"], authority)

    wrong_binding = _claimed_action(
        database, integration_settings, project_id, binding="some-other-binding"
    )
    refusals_before(wrong_binding, ARGV, authority)

    wrong_actor = _claimed_action(
        database, integration_settings, project_id, actor="compute-worker"
    )
    refusals_before(wrong_actor, ARGV, authority)

    # An Action from this project, presented through a guard bound to another
    # project, finds nothing and is refused as a permission error.
    other_project = deployment.project_id("ac3-other-guard")
    other_guard = DatabaseActionGuard(database, other_project)
    other_provider = deployment.provider(other_project, guard=other_guard)
    other_authority = deployment.authority(other_project, binding="db-binding")
    other_handle = other_provider.provision(other_project, other_authority)
    cross_project = _claimed_action(database, integration_settings, project_id)
    with pytest.raises(SandboxForbidden):
        other_provider.execute(
            other_handle,
            ARGV,
            timeout=30,
            action_id=cross_project,
            authority=other_authority,
        )

    with database.read_only() as session:
        service = ActionService(session, project_id, settings=integration_settings)
        # The unclaimed Action was never claimed; the refusal must not have
        # moved it. The other four were claimed and must remain claimed, because
        # a guard rejection happens before any execution record or status change.
        assert service.get(unclaimed).status is ActionStatus.READY
        for action_id in (wrong_command, wrong_binding, wrong_actor, cross_project):
            assert service.get(action_id).status is ActionStatus.CLAIMED
    provider.destroy(handle, authority)
    other_provider.destroy(other_handle, other_authority)
