#!/usr/bin/env python3
"""The real-HPC half of T09: probe one configured target, or say what is missing.

This is deliberately **not** a pytest test. The acceptance suite uses a scripted
scheduler and certifies only the internal surface; a live cluster is a resource
this repository does not have. Running this file without an explicit opt-in
therefore prints a `BLOCKED_EXTERNAL` report with the conditions that would
unblock it, and exits 3. It never invents a host, a credential or a job.

With `RAVEL_REAL_HPC=1` and an operator-supplied target file, it does what a
person would do by hand:

1. resolve the named target in this process (the only place a secret is read),
2. connect with strict host-key verification against the target's known_hosts,
3. ask the scheduler for its version and check a named partition,
4. list which of the target's allowed software is on the login PATH,
5. and, only with `--submit`, submit a one-task probe job, follow it through
   `sacct`, fetch its output, and cancel it if it outlives the timeout.

Every string that leaves the script is redacted. Credential references are not
printed either: the report names the target and its endpoint, and no more.

Exit codes: 0 probe passed, 1 probe ran and failed, 2 usage/configuration
error, 3 `BLOCKED_EXTERNAL` (no opt-in). `--json` is the only output mode
because this report is meant to be attached to `evidence/T09/<run-id>/`.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ravel.backends.slurm import (  # noqa: E402 - import follows the path setup
    HostKeyVerificationError,
    HpcTarget,
    ParamikoTransport,
    Redactor,
    SlurmConfigurationError,
    TargetConfigurationError,
    TargetRegistry,
    TransportError,
    resolve_target,
)

TASK = "T09"
OPT_IN_VARIABLE = "RAVEL_REAL_HPC"
BLOCKED_EXTERNAL = "BLOCKED_EXTERNAL"
TERMINAL_SLURM_STATES = frozenset(
    {
        "BOOT_FAIL",
        "CANCELLED",
        "COMPLETED",
        "DEADLINE",
        "FAILED",
        "NODE_FAIL",
        "OUT_OF_MEMORY",
        "PREEMPTED",
        "TIMEOUT",
    }
)

UNLOCK_CONDITIONS = (
    "A reachable Slurm-over-SSH login endpoint for the cluster under test.",
    "An operator-verified known_hosts file the worker can read; an unknown "
    "host is refused, never accepted on first contact.",
    "A credential the execution process can resolve from env:, file: or key:, "
    "provisioned outside the model prompt and outside this repository.",
    "A target id in the deployment allowlist whose partition, account, remote "
    "root and software catalog match that cluster.",
    "Operator authorization to occupy one short allocation, needed only when "
    "the probe is run with --submit.",
)

HOW_TO_RUN = (
    "RAVEL_REAL_HPC=1 python tests/premium/T09/real_hpc_check.py "
    "--targets-file <hpc-targets.json> --target-id <uuid> [--submit]"
)


class ProbeError(RuntimeError):
    """A probe step failed for a reason this script can report as a fact."""


def _blocked(reason: str) -> dict[str, object]:
    return {
        "task": TASK,
        "status": BLOCKED_EXTERNAL,
        "real_hpc": False,
        "reason": reason,
        "unlock_conditions": list(UNLOCK_CONDITIONS),
        "how_to_run": HOW_TO_RUN,
        "internal_scope": (
            "The T09 acceptance suite substitutes a scripted scheduler. It "
            "proves idempotency, quota, cancellation states and credential "
            "containment; it does not certify any real cluster."
        ),
    }


def _selected(registry: TargetRegistry, target_id: str | None):
    """The one target the operator meant, refusing an ambiguous selection."""
    if target_id:
        return registry.target(target_id)
    enabled = [target for target in registry if target.enabled]
    if len(enabled) != 1:
        raise SlurmConfigurationError(
            "the target file has "
            f"{len(enabled)} enabled targets; pass --target-id to choose one"
        )
    return enabled[0]


def _scheduler_version(transport: ParamikoTransport, timeout: float) -> dict[str, object]:
    """Ask Slurm who it is, preferring sinfo and falling back to sbatch."""
    asked: list[str] = []
    for command in ("sinfo -V", "sbatch --version"):
        result = transport.run(command, timeout=timeout)
        asked.append(command)
        if result.ok and result.output:
            return {"ok": True, "command": command, "version": result.output}
    return {
        "ok": False,
        "command": asked[-1],
        "detail": "neither sinfo nor sbatch answered with a version",
    }


def _partition_check(
    transport: ParamikoTransport, partition: str | None, timeout: float
) -> dict[str, object] | None:
    """Whether the configured partition exists, when the target names one."""
    if not partition:
        return None
    result = transport.run(
        f"scontrol show partition {shlex.quote(partition)}", timeout=timeout
    )
    return {
        "ok": result.ok,
        "partition": partition,
        "detail": result.stderr.strip() or result.stdout.strip() or None,
    }


def _software_check(
    transport: ParamikoTransport, catalog: tuple[str, ...], timeout: float
) -> dict[str, object]:
    """Which catalog entries are on PATH. A module-only install is a warning."""
    found: dict[str, bool] = {}
    for software in catalog:
        result = transport.run(f"command -v {shlex.quote(software)}", timeout=timeout)
        found[software] = result.ok
    return {
        "ok": True,
        "on_login_path": found,
        "note": (
            "A false entry means the binary was not on the login PATH; the "
            "cluster may still provide it through a module."
        ),
    }


def _probe_script() -> str:
    return (
        "#!/bin/bash\n"
        "#SBATCH --job-name=ravel-t09-check\n"
        "#SBATCH --output=ravel-t09-check.out\n"
        "set -eu\n"
        "hostname\n"
        "echo RAVEL_T09_PROBE_OK\n"
    )


def _sbatch_command(target: HpcTarget, remote_dir: str, remote_script: str) -> str:
    """The probe submission, bounded by the target's own resource limits."""
    parts = [
        "sbatch",
        "--parsable",
        "--job-name=ravel-t09-check",
        f"--chdir={shlex.quote(remote_dir)}",
    ]
    if target.partition:
        parts.append(f"--partition={shlex.quote(target.partition)}")
    if target.account:
        parts.append(f"--account={shlex.quote(target.account)}")
    if target.cpu_limit:
        parts.append(f"--cpus-per-task={target.cpu_limit}")
    if target.gpu_limit:
        parts.append(f"--gres=gpu:{target.gpu_limit}")
    if target.hour_limit:
        parts.append(f"--time={max(1, int(target.hour_limit * 60))}")
    parts.append(shlex.quote(remote_script))
    return " ".join(parts)


def _parse_sacct(stdout: str, job_id: str) -> tuple[str, str, str] | None:
    """The job's line in `sacct -P` output, as (state, exit code, elapsed)."""
    for line in stdout.splitlines():
        fields = [field.strip() for field in line.split("|")]
        if len(fields) >= 3 and fields[0] == job_id:
            elapsed = fields[3] if len(fields) > 3 else "UNKNOWN"
            return fields[1], fields[2], elapsed
    return None


def _submission_probe(
    transport: ParamikoTransport,
    target: HpcTarget,
    timeout: float,
    poll_seconds: float,
) -> dict[str, object]:
    """Submit one tiny job, follow it, and cancel it if it outstays the timeout."""
    remote_dir = (
        f"{target.remote_root.rstrip('/')}/ravel-t09-check-{time.time_ns()}"
    )
    remote_script = f"{remote_dir}/probe.slurm"
    with tempfile.TemporaryDirectory() as directory:
        local_script = Path(directory) / "probe.slurm"
        local_script.write_text(_probe_script(), encoding="utf-8")
        transport.put(local_script, remote_script)

    submitted = transport.run(
        _sbatch_command(target, remote_dir, remote_script), timeout=timeout
    )
    if not submitted.ok:
        raise ProbeError(
            f"sbatch refused the probe: {submitted.stderr.strip() or 'no output'}"
        )
    job_id = submitted.output.split(";", 1)[0].strip()
    if not job_id or not job_id.isdigit():
        raise ProbeError(f"sbatch did not return a job id: {submitted.output!r}")

    deadline = time.monotonic() + timeout
    state = "UNKNOWN"
    exit_code = "UNKNOWN"
    elapsed = "UNKNOWN"
    cancelled = False
    while True:
        accounting = transport.run(
            "sacct -n -P -X -j "
            f"{job_id} -o JobID,State,ExitCode,Elapsed",
            timeout=timeout,
        )
        parsed = _parse_sacct(accounting.stdout, job_id)
        if parsed is not None:
            state, exit_code, elapsed = parsed
            if state.split()[0] in TERMINAL_SLURM_STATES:
                break
        if time.monotonic() >= deadline:
            cancel = transport.run(f"scancel {job_id}", timeout=timeout)
            cancelled = cancel.ok
            break
        time.sleep(poll_seconds)

    output = transport.run(
        f"cat {shlex.quote(remote_dir + '/ravel-t09-check.out')}", timeout=timeout
    )
    terminal = state.split()[0] in TERMINAL_SLURM_STATES
    return {
        "ok": terminal and state.split()[0] == "COMPLETED" and exit_code == "0:0",
        "job_id": job_id,
        "state": state,
        "exit_code": exit_code,
        "elapsed": elapsed,
        "cancelled_after_timeout": cancelled,
        "output": output.output[:2000] if output.ok else None,
        "remote_dir": remote_dir,
    }


def _probe(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    registry = TargetRegistry.from_path(args.targets_file)
    configured = _selected(registry, args.target_id)
    resolved = resolve_target(configured, environ=os.environ)
    redactor = Redactor((resolved.password or "",))

    report: dict[str, object] = {
        "task": TASK,
        "status": "FAILED",
        "real_hpc": True,
        "target_id": configured.target_id,
        "endpoint": f"{resolved.username}@{resolved.host}:{resolved.port}",
        "host_key_verified": False,
        "checks": {},
    }
    checks: dict[str, dict[str, object]] = {}
    report["checks"] = checks

    transport: ParamikoTransport | None = None
    try:
        transport = ParamikoTransport.connect(
            host=resolved.host,
            port=resolved.port,
            username=resolved.username,
            password=resolved.password,
            key_filename=resolved.key_filename,
            known_hosts_filename=resolved.known_hosts_filename,
            strict_host_keys=True,
            timeout=args.timeout,
        )
        # Reaching this line means the host key matched the configured file:
        # `connect` raises before opening a session for an unknown or changed
        # key, and it never loads a policy that would add one.
        report["host_key_verified"] = True
        checks["scheduler"] = _scheduler_version(transport, args.timeout)
        partition = _partition_check(transport, resolved.partition, args.timeout)
        if partition is not None:
            checks["partition"] = partition
        checks["software"] = _software_check(
            transport, resolved.allowed_software, args.timeout
        )
        if args.submit:
            checks["submission"] = _submission_probe(
                transport, configured, args.timeout, args.poll_seconds
            )
    except HostKeyVerificationError as error:
        report["state"] = getattr(error, "state", "HOST_KEY_UNKNOWN")
        report["detail"] = redactor(str(error))
        return report, 1
    except (ProbeError, TransportError, SlurmConfigurationError) as error:
        report["detail"] = redactor(str(error))
        return report, 1
    except Exception as error:
        report["detail"] = redactor(
            f"{error.__class__.__name__}: {error}"
        )
        return report, 1
    finally:
        if transport is not None:
            transport.close()

    checks = report["checks"]
    passed = (
        report["host_key_verified"] is True
        and isinstance(checks, dict)
        and checks.get("scheduler", {}).get("ok") is True
        and (not args.submit or checks.get("submission", {}).get("ok") is True)
    )
    report["status"] = "OK" if passed else "FAILED"
    return report, 0 if passed else 1


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Probe a real Slurm target for T09. Without RAVEL_REAL_HPC=1 this "
            "prints a BLOCKED_EXTERNAL report and contacts nothing."
        )
    )
    parser.add_argument(
        "--targets-file",
        help="the deployment's HPC target file (same shape as the worker's)",
    )
    parser.add_argument("--target-id", help="which allowlisted target to probe")
    parser.add_argument(
        "--submit",
        action="store_true",
        help="submit one short probe job; spends a real allocation",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30.0,
        help="seconds for connection and each scheduler command (default 30)",
    )
    parser.add_argument(
        "--poll-seconds",
        type=float,
        default=5.0,
        help="seconds between accounting polls (default 5)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if os.environ.get(OPT_IN_VARIABLE) != "1":
        print(
            json.dumps(
                _blocked(
                    f"no real cluster was supplied: set {OPT_IN_VARIABLE}=1 to "
                    "authorize contacting the target named by --target-id"
                ),
                indent=2,
                sort_keys=True,
            )
        )
        return 3
    if not args.targets_file:
        print(
            json.dumps(
                _blocked("--targets-file is required after opting in"),
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    try:
        report, code = _probe(args)
    except (TargetConfigurationError, SlurmConfigurationError, OSError) as error:
        report = _blocked(f"the target configuration could not be used: {error}")
        report["status"] = "CONFIGURATION_ERROR"
        code = 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
