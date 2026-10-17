#!/usr/bin/env python3
"""Canonical backend preflight for the RAVEL premium development plan.

The premium plan fixes the backend to Ubuntu 24.04 x86_64, Python 3.12 and
Docker Engine + Compose v2.  This script reports every missing dependency and
exits non-zero if any *required* one is absent.  It never starts services and
never reads a secret.

    .venv/bin/python scripts/premium_preflight.py
    .venv/bin/python scripts/premium_preflight.py --json
    .venv/bin/python scripts/premium_preflight.py --services

``--services`` additionally checks that PostgreSQL, Temporal and the
S3-compatible endpoint accept TCP connections.  The ports are read from the
same RAVEL_* variables the application uses; the check is reachability only.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
WARN = "\033[33m!\033[0m"

EXPECTED_OS = "24.04"
EXPECTED_ARCH = "x86_64"
EXPECTED_PYTHON = (3, 12)


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True

    @property
    def status(self) -> str:
        if self.ok:
            return "PASS"
        return "FAIL" if self.required else "WARN"


def _os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


def check_os(
    release: dict[str, str] | None = None,
    machine: str | None = None,
    *,
    allow_noncanonical: bool = False,
) -> Check:
    release = _os_release() if release is None else release
    machine = platform.machine() if machine is None else machine
    distro = release.get("ID", "unknown")
    version = release.get("VERSION_ID", "unknown")
    ok = distro == "ubuntu" and version == EXPECTED_OS and machine == EXPECTED_ARCH
    detail = f"{distro} {version} {machine}"
    if not ok and allow_noncanonical:
        return Check(
            "Ubuntu 24.04 x86_64",
            False,
            detail + " (noncanonical allowed)",
            required=False,
        )
    return Check("Ubuntu 24.04 x86_64", ok, detail)


def check_python(version_info: tuple[int, int] | None = None) -> Check:
    version = tuple(sys.version_info[:2]) if version_info is None else version_info
    return Check(
        "Python 3.12",
        version == EXPECTED_PYTHON,
        f"{version[0]}.{version[1]}",
    )


def check_git() -> Check:
    binary = shutil.which("git")
    if binary is None:
        return Check("git", False, "git not found")
    result = subprocess.run(
        [binary, "--version"], capture_output=True, text=True, check=False
    )
    return Check("git", result.returncode == 0, result.stdout.strip() or result.stderr.strip())


def check_docker(
    *,
    which=shutil.which,
    runner=subprocess.run,
) -> Check:
    binary = which("docker")
    if binary is None:
        return Check("Docker CLI", False, "docker not found")
    version = runner([binary, "--version"], capture_output=True, text=True, check=False)
    if version.returncode != 0:
        return Check("Docker CLI", False, (version.stderr or version.stdout).strip())
    info = runner([binary, "info"], capture_output=True, text=True, check=False)
    if info.returncode != 0:
        detail = (info.stderr or info.stdout).strip().splitlines()
        return Check(
            "Docker daemon",
            False,
            detail[-1] if detail else "docker info failed",
        )
    return Check("Docker CLI + daemon", True, version.stdout.strip())


def check_compose(*, which=shutil.which, runner=subprocess.run) -> Check:
    binary = which("docker")
    if binary is None:
        return Check("Docker Compose v2", False, "docker not found")
    result = runner([binary, "compose", "version"], capture_output=True, text=True, check=False)
    ok = result.returncode == 0
    detail = (result.stdout or result.stderr).strip()
    return Check("Docker Compose v2", ok, detail)


def _tcp(host: str, port: int, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"{host}:{port} reachable"
    except OSError as error:
        return False, f"{host}:{port} unreachable ({error})"


def _host_port(value: str, default_port: str) -> tuple[str, int]:
    host, _, port = value.partition(":")
    return host or "127.0.0.1", int(port or default_port)


def service_checks(*, env: dict[str, str] | None = None) -> list[Check]:
    env = os.environ if env is None else env
    checks: list[Check] = []

    postgres_host = env.get("RAVEL_POSTGRES_HOST", "127.0.0.1")
    postgres_port = int(env.get("RAVEL_POSTGRES_PORT", "55432"))
    checks.append(Check("PostgreSQL", *_tcp(postgres_host, postgres_port)))

    temporal_host, temporal_port = _host_port(
        env.get("RAVEL_TEMPORAL_HOST", "127.0.0.1:7233"), "7233"
    )
    checks.append(Check("Temporal", *_tcp(temporal_host, temporal_port)))

    endpoint = env.get("RAVEL_S3_ENDPOINT", "http://127.0.0.1:9100")
    without_scheme = endpoint.split("://", 1)[-1].rstrip("/")
    s3_host, s3_port = _host_port(without_scheme, "80")
    checks.append(Check("S3 endpoint", *_tcp(s3_host, s3_port)))
    return checks


def check_repo(repo_root: Path) -> Check:
    required = [
        repo_root / "pyproject.toml",
        repo_root / "Makefile",
        repo_root / "docs" / "premium" / "acceptance-map.json",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        return Check("Repository layout", False, "missing: " + ", ".join(missing))
    return Check("Repository layout", True, str(repo_root))


def run_checks(
    *,
    repo_root: Path,
    services: bool = False,
    allow_noncanonical: bool = False,
    release: dict[str, str] | None = None,
    machine: str | None = None,
    python_version: tuple[int, int] | None = None,
    which=shutil.which,
    runner=subprocess.run,
) -> list[Check]:
    checks = [
        check_os(release, machine, allow_noncanonical=allow_noncanonical),
        check_python(python_version),
        check_git(),
        check_docker(which=which, runner=runner),
        check_compose(which=which, runner=runner),
        check_repo(repo_root),
    ]
    if services:
        checks.extend(service_checks())
    return checks


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--services", action="store_true", help="also check service ports")
    parser.add_argument(
        "--allow-noncanonical",
        action="store_true",
        help="downgrade a non-24.04 host to a warning (development only)",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help=argparse.SUPPRESS,
    )
    args = parser.parse_args(argv)

    checks = run_checks(
        repo_root=args.repo_root,
        services=args.services,
        allow_noncanonical=args.allow_noncanonical,
    )
    required_failed = [check for check in checks if check.required and not check.ok]

    if args.json:
        print(
            json.dumps(
                {
                    "ok": not required_failed,
                    "checks": [asdict(check) for check in checks],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print("\nRAVEL premium — backend preflight\n")
        for check in checks:
            icon = PASS if check.ok else (FAIL if check.required else WARN)
            print(f"  {icon} {check.name} — {check.detail}")
        if required_failed:
            print(f"\n{len(required_failed)} required check(s) failed:")
            for check in required_failed:
                print(f"  - {check.name}: {check.detail}")
        else:
            print("\nCanonical backend ready.")

    return 0 if not required_failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
