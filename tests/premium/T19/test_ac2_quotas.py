"""T19-AC2: the quotas and the wall clock are enforced by the kernel, not the argv.

Each case reads the limit Docker actually applied from `docker inspect`, then
makes a process inside the container reach it. A limit that is merely present in
the create arguments but not enforced would let the process finish; these cases
would then fail on the observed result, not on a string comparison.

The lifecycle cases use the same running supervisor over HTTP. Cancellation is
observed from a second request while the command is blocked, which is only
possible because the execute endpoint runs off the event loop.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
from tests.premium.T19.support import (
    Deployment,
    container_of,
    container_process_args,
    execute_command,
    handle_record,
    python_argv,
    read_json_line,
    run_record,
    run_records_for_handle,
    sleep_seconds,
    wait_until,
)

from ravel.domain.contracts import AuthorityEnvelope, BudgetLimits
from ravel.sandbox.docker_engine import DockerEngine
from ravel.sandbox.models import RunState, SandboxLimits, SandboxValidationError

pytestmark = pytest.mark.integration

MIB = 1024 * 1024
GIB = 1024 * MIB


def _status_of(provider: Any, run: Any) -> Any:
    status = provider.status(run)
    return status


def _execute(
    deployment: Deployment,
    handle: Any,
    authority: Any,
    code: str,
    *,
    timeout: float = 60.0,
) -> tuple[Any, Any]:
    provider, run = execute_command(
        deployment, handle, authority, python_argv(code), timeout=timeout
    )
    return provider, _status_of(provider, run)


def test_cpu_quota_actually_throttles_four_busy_children(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """With 2 CPUs of quota, four runnable children cannot consume four CPUs."""
    project_id = deployment.project_id("ac2-cpu")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    container = container_of(deployment, handle)
    host = docker_engine.inspect(container)["HostConfig"]
    assert host["NanoCpus"] == 2_000_000_000

    code = r"""
import json, os, time

SECONDS = 3.0
children = []
started = time.monotonic()
for _ in range(4):
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        child_start = time.monotonic()
        child_cpu = time.process_time()
        while time.monotonic() - child_start < SECONDS:
            pass
        os.write(write_fd, repr(time.process_time() - child_cpu).encode("ascii"))
        os.close(write_fd)
        os._exit(0)
    os.close(write_fd)
    children.append((pid, read_fd))

cpu_seconds = 0.0
for pid, read_fd in children:
    data = b""
    while True:
        chunk = os.read(read_fd, 4096)
        if not chunk:
            break
        data += chunk
    os.close(read_fd)
    os.waitpid(pid, 0)
    cpu_seconds += float(data)
elapsed = time.monotonic() - started
print(json.dumps({"cpu_seconds": cpu_seconds, "elapsed": elapsed}))
"""
    _, status = _execute(deployment, handle, authority, code, timeout=30)
    assert status.state is RunState.SUCCEEDED, (status.detail, status.stderr)
    payload = read_json_line(status.stdout)
    elapsed = payload["elapsed"]
    cpu_seconds = payload["cpu_seconds"]
    assert elapsed >= 2.0, payload
    # Four unthrottled children would accumulate ~4 * elapsed CPU-seconds.
    # Two CPUs of quota permit ~2 * elapsed, with a margin for accounting and
    # startup; the bound is what makes this an enforcement observation.
    assert cpu_seconds <= elapsed * 2.75, payload
    assert cpu_seconds >= elapsed * 0.75, payload


def test_memory_limit_kills_or_refuses_an_allocation_over_the_limit(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """A 64 MiB container does not successfully allocate 256 MiB."""
    project_id = deployment.project_id("ac2-memory")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(
        project_id,
        authority,
        SandboxLimits(memory_bytes=64 * MIB),
    )
    container = container_of(deployment, handle)
    assert docker_engine.inspect(container)["HostConfig"]["Memory"] == 64 * MIB

    code = r"""
import json
blocks = []
result = {"failed": False, "allocated": 0}
try:
    for _ in range(32):
        block = bytearray(8 * 1024 * 1024)
        for offset in range(0, len(block), 4096):
            block[offset] = 1
        blocks.append(block)
        result["allocated"] += len(block)
except MemoryError:
    result["failed"] = True
    result["error"] = "MemoryError"
print(json.dumps(result))
"""
    _, status = _execute(deployment, handle, authority, code, timeout=60)
    if status.state is RunState.SUCCEEDED:
        payload = read_json_line(status.stdout)
        assert payload["failed"] is True, payload
    else:
        assert status.state is RunState.FAILED, status.state
        assert status.exit_code in (1, 137), status.exit_code

    # The container itself survived its child: a limit, not a crash.
    _, echo = _execute(
        deployment,
        handle,
        authority,
        "print('container-still-alive')",
        timeout=20,
    )
    assert echo.state is RunState.SUCCEEDED
    assert "container-still-alive" in echo.stdout


def test_pid_limit_stops_forking_inside_the_container(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """A 32-PID container cannot fork 100 children."""
    project_id = deployment.project_id("ac2-pids")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(
        project_id,
        authority,
        SandboxLimits(pids=32),
    )
    container = container_of(deployment, handle)
    assert docker_engine.inspect(container)["HostConfig"]["PidsLimit"] == 32

    code = r"""
import errno, json, os, time

pids = []
failure = None
for _ in range(100):
    try:
        pid = os.fork()
    except OSError as exc:
        failure = {"errno": exc.errno, "type": type(exc).__name__}
        break
    if pid == 0:
        time.sleep(5)
        os._exit(0)
    pids.append(pid)

for pid in pids:
    try:
        os.kill(pid, 9)
    except OSError:
        pass
for pid in pids:
    try:
        os.waitpid(pid, 0)
    except OSError:
        pass
print(json.dumps({"children": len(pids), "failure": failure}))
"""
    _, status = _execute(deployment, handle, authority, code, timeout=30)
    assert status.state is RunState.SUCCEEDED, (status.detail, status.stderr)
    payload = read_json_line(status.stdout)
    assert payload["failure"] is not None, payload
    assert payload["children"] <= 40, payload
    assert payload["failure"]["type"] in ("BlockingIOError", "OSError"), payload
    assert payload["failure"]["errno"] in (11, 12), payload


def test_scratch_tmpfs_reaches_enospc_without_touching_the_workspace(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """A 16 MiB scratch tmpfs returns ENOSPC; the project volume is separate."""
    project_id = deployment.project_id("ac2-disk")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(
        project_id,
        authority,
        SandboxLimits(scratch_bytes=16 * MIB),
    )
    container = container_of(deployment, handle)
    tmpfs = docker_engine.inspect(container)["HostConfig"]["Tmpfs"]["/tmp"]
    assert f"size={16 * MIB}" in tmpfs

    code = r"""
import json, os
fd = os.open("/tmp/fill.bin", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
block = b"x" * (1024 * 1024)
written = 0
error = None
try:
    for _ in range(64):
        written += os.write(fd, block)
except OSError as exc:
    error = exc.errno
finally:
    os.close(fd)
    try:
        os.unlink("/tmp/fill.bin")
    except OSError:
        pass
print(json.dumps({"bytes": written, "errno": error}))
"""
    _, status = _execute(deployment, handle, authority, code, timeout=30)
    assert status.state is RunState.SUCCEEDED, (status.detail, status.stderr)
    payload = read_json_line(status.stdout)
    assert payload["errno"] == 28, payload
    assert payload["bytes"] < 24 * MIB, payload


def test_command_timeout_ends_the_run_and_leaves_no_process(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """A 1-second bound ends a 30-second sleep, and the process is gone."""
    project_id = deployment.project_id("ac2-timeout")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    container = container_of(deployment, handle)

    provider, run = execute_command(
        deployment, handle, authority, sleep_seconds(30), timeout=1.0
    )
    status = provider.status(run)
    assert status.state is RunState.TIMED_OUT, (status.state, status.detail)
    assert status.timed_out is True
    assert status.duration_seconds < 15, status.duration_seconds

    wait_until(
        lambda: not any(
            "sleep 30" in line.lower()
            for line in container_process_args(docker_engine, container)
        ),
        timeout=15,
        message="the timed-out sleep was still running in the container",
    )
    record = run_record(deployment, run.run_id)
    assert record["run"]["state"] == "TIMED_OUT"
    assert record["timed_out"] is True


def test_stop_cancels_a_blocked_command_and_reconnect_reuses_the_project_binding(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """A concurrent stop ends the run; reconnect starts the same project sandbox."""
    project_id = deployment.project_id("ac2-stop")
    authority = deployment.authority(project_id, binding="stop-binding")
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    container = container_of(deployment, handle)

    outcome: dict[str, Any] = {}
    failures: list[BaseException] = []

    def run_sleep() -> None:
        try:
            runner, run = execute_command(
                deployment, handle, authority, sleep_seconds(60), timeout=60.0
            )
            outcome["run"] = run
            outcome["status"] = runner.status(run)
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=run_sleep, name="t19-stop-run", daemon=True)
    worker.start()
    wait_until(
        lambda: bool(run_records_for_handle(deployment, handle.handle_id)),
        timeout=15,
        message="the supervisor never recorded the running command",
    )
    wait_until(
        lambda: any(
            "sleep 60" in line.lower()
            for line in container_process_args(docker_engine, container)
        ),
        timeout=15,
        message="the sleep command never started in the sandbox",
    )

    stopped = provider.stop(handle, authority)
    assert stopped.state.value == "STOPPED"
    worker.join(timeout=30)
    assert not worker.is_alive(), "the execute request did not return after stop"
    assert not failures, failures
    run = outcome["run"]
    status = outcome["status"]
    assert status.state is RunState.CANCELLED, (status.state, status.detail)
    assert status.cancelled is True
    assert docker_engine.container_running(container) is False
    record = run_record(deployment, run.run_id)
    assert record["run"]["state"] == "CANCELLED"
    assert handle_record(deployment, handle.handle_id)["handle"]["state"] == "STOPPED"

    reconnected = provider.reconnect(handle, authority)
    assert reconnected.state.value == "READY"
    assert reconnected.project_id == project_id
    _, echo = _execute(
        deployment,
        reconnected,
        authority,
        "print('after-reconnect')",
        timeout=20,
    )
    assert echo.state is RunState.SUCCEEDED
    assert "after-reconnect" in echo.stdout
    assert handle_record(deployment, handle.handle_id)["binding_id"] == "stop-binding"


def test_destroy_removes_container_and_volume_but_keeps_queryable_records(
    deployment: Deployment,
    docker_engine: DockerEngine,
) -> None:
    """Destroy is a cleanup, not a record deletion."""
    project_id = deployment.project_id("ac2-destroy")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    record = handle_record(deployment, handle.handle_id)
    container = str(record["container"])
    volume = str(record["volume"])
    assert docker_engine.container_running(container)
    assert docker_engine.volume_present(volume)

    provider.files_write(handle, "kept.txt", b"workspace data")
    _, status = _execute(
        deployment,
        handle,
        authority,
        "from pathlib import Path; Path('/workspace/result.txt').write_text('done')",
        timeout=20,
    )
    assert status.state is RunState.SUCCEEDED
    run_id = status.run.run_id

    destroyed = provider.destroy(handle, authority)
    assert destroyed.state.value == "DESTROYED"
    assert docker_engine.container_present(container) is False
    assert docker_engine.volume_present(volume) is False
    assert handle_record(deployment, handle.handle_id)["handle"]["state"] == "DESTROYED"
    stored = run_record(deployment, run_id)
    assert stored["run"]["state"] == "SUCCEEDED"


def test_a_request_wider_than_the_deployment_ceiling_is_refused(
    deployment: Deployment,
) -> None:
    """The ceiling is a refusal, not a silent reduction."""
    project_id = deployment.project_id("ac2-ceiling")
    authority = deployment.authority(project_id)
    provider = deployment.provider(project_id)
    for limits in (
        SandboxLimits(cpu_cores=4.0),
        SandboxLimits(memory_bytes=8 * GIB),
        SandboxLimits(pids=1024),
        SandboxLimits(scratch_bytes=20 * GIB),
        SandboxLimits(timeout_seconds=121.0),
    ):
        with pytest.raises(SandboxValidationError, match="ceiling"):
            provider.provision(project_id, authority, limits)

    narrowed = provider.provision(project_id, authority, SandboxLimits(cpu_cores=1.0))
    record = handle_record(deployment, narrowed.handle_id)
    assert record["limits"]["cpu_cores"] == 1.0
    provider.destroy(narrowed, authority)


def test_authority_envelope_narrows_the_timeout_and_uses_it(
    deployment: Deployment,
) -> None:
    """A 0.001-hour wall clock makes a 3.6-second command ceiling real."""
    project_id = deployment.project_id("ac2-envelope")
    envelope = AuthorityEnvelope(
        project_id=project_id,
        budget_time_limits=BudgetLimits(max_wall_clock_hours=0.001),
    )
    authority = deployment.authority(project_id, envelope=envelope)
    provider = deployment.provider(project_id)
    handle = provider.provision(project_id, authority)
    limits = handle_record(deployment, handle.handle_id)["limits"]
    assert abs(limits["timeout_seconds"] - 3.6) < 0.01, limits

    with pytest.raises(SandboxValidationError, match="exceeds"):
        execute_command(
            deployment, handle, authority, sleep_seconds(10), timeout=10.0
        )

    runner, run = execute_command(
        deployment, handle, authority, sleep_seconds(10), timeout=3.6
    )
    status = runner.status(run)
    assert status.state is RunState.TIMED_OUT
    assert status.duration_seconds < 10
