"""T01-AC1: missing backend dependencies fail loudly and precisely."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import premium_preflight
import pytest


def test_canonical_os_passes() -> None:
    check = premium_preflight.check_os(
        {"ID": "ubuntu", "VERSION_ID": "24.04"}, "x86_64"
    )
    assert check.ok is True
    assert check.required is True


@pytest.mark.parametrize(
    "release,machine",
    [
        ({"ID": "ubuntu", "VERSION_ID": "26.04"}, "x86_64"),
        ({"ID": "debian", "VERSION_ID": "24.04"}, "x86_64"),
        ({"ID": "ubuntu", "VERSION_ID": "24.04"}, "aarch64"),
    ],
)
def test_noncanonical_os_fails(release: dict[str, str], machine: str) -> None:
    check = premium_preflight.check_os(release, machine)
    assert check.ok is False
    assert check.required is True


def test_python_312_is_required() -> None:
    assert premium_preflight.check_python((3, 12)).ok is True
    assert premium_preflight.check_python((3, 11)).ok is False
    assert premium_preflight.check_python((3, 14)).ok is False


def test_missing_git_fails() -> None:
    check = premium_preflight.check_git(which=lambda _name: None, runner=None)
    assert check.ok is False
    assert "not found" in check.detail


def test_missing_docker_fails() -> None:
    check = premium_preflight.check_docker(which=lambda _name: None, runner=None)
    assert check.ok is False
    assert "not found" in check.detail


def test_missing_compose_fails() -> None:
    check = premium_preflight.check_compose(which=lambda _name: None, runner=None)
    assert check.ok is False
    assert "not found" in check.detail


def test_cli_is_nonzero_when_docker_and_git_are_absent(tmp_path) -> None:
    env = os.environ.copy()
    env["PATH"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(premium_preflight.__file__), "--json"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    names = {check["name"] for check in payload["checks"] if not check["ok"]}
    assert "Docker CLI" in names
    assert "git" in names or "Docker daemon" in names


def test_main_returns_nonzero_for_failed_os(monkeypatch) -> None:
    monkeypatch.setattr(
        premium_preflight,
        "run_checks",
        lambda **_kwargs: [
            premium_preflight.Check(
                "Ubuntu 24.04 x86_64", False, "ubuntu 26.04 x86_64", required=True
            )
        ],
    )
    assert premium_preflight.main(["--json"]) == 1


def test_main_returns_nonzero_for_failed_python(monkeypatch) -> None:
    monkeypatch.setattr(
        premium_preflight,
        "run_checks",
        lambda **_kwargs: [
            premium_preflight.Check("Python 3.12", False, "3.11", required=True)
        ],
    )
    assert premium_preflight.main(["--json"]) == 1
