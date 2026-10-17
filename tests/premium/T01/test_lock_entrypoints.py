"""T01-AC2: premium-task, release-gate and direct pytest share one lock.

The unit in ``test_lock.py`` proves the lock itself is exclusive. These tests
prove the three entry points actually reach it: while this process holds the
lock, each entry point in a child process waits and is then refused, rather
than truncating the database under the holder.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import premium_task
from ravel.state.db_lock import (
    DatabaseLock,
    database_lock_path,
    lock_dir,
)

REPO_ROOT = Path(__file__).resolve().parents[3]


def _environment(tmp_path: Path, *, timeout: float = 0.5) -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(
        {
            "RAVEL_PREMIUM_LOCK_DIR": str(tmp_path / "locks"),
            "RAVEL_PREMIUM_LOCK_TIMEOUT": str(timeout),
            "RAVEL_POSTGRES_HOST": "127.0.0.1",
            "RAVEL_POSTGRES_PORT": "55432",
            "RAVEL_POSTGRES_DB": "ravel_test",
            "PYTHONPATH": os.pathsep.join(
                [str(REPO_ROOT / "src"), str(REPO_ROOT / "scripts")]
            ),
        }
    )
    return environment


def _lock_path(tmp_path: Path) -> Path:
    return database_lock_path(_environment(tmp_path))


def _run(
    arguments: list[str], *, cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *arguments],
        cwd=str(cwd),
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )


def test_premium_task_entrypoint_is_refused_while_lock_held(
    tmp_path: Path,
) -> None:
    assert premium_task.DatabaseLock is DatabaseLock
    environment = _environment(tmp_path)
    with DatabaseLock(_lock_path(tmp_path), timeout=0.5):
        result = _run(
            [str(REPO_ROOT / "scripts" / "premium_task.py"), "T04", "--skip-preflight"],
            cwd=REPO_ROOT,
            environment=environment,
        )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "timed out waiting for database test lock" in output


def test_release_gate_entrypoint_is_refused_while_lock_held(
    tmp_path: Path,
) -> None:
    environment = _environment(tmp_path)
    with DatabaseLock(_lock_path(tmp_path), timeout=0.5):
        result = _run(
            [str(REPO_ROOT / "scripts" / "release_gate.py"), "--only", "unit"],
            cwd=REPO_ROOT,
            environment=environment,
        )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "timed out waiting for database test lock" in output


def test_direct_pytest_is_refused_while_lock_held(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    target = (
        "tests/integration/state/test_guards.py::"
        "test_install_creates_every_guard_the_module_declares"
    )
    with DatabaseLock(_lock_path(tmp_path), timeout=0.5):
        result = _run(
            [
                "-m",
                "pytest",
                target,
                "-q",
                "-p",
                "no:randomly",
            ],
            cwd=REPO_ROOT,
            environment=environment,
        )
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "timed out waiting for database test lock" in output


def test_a_blocked_runner_enters_after_the_holder_releases(tmp_path: Path) -> None:
    """The lock waits rather than failing the second process outright."""
    lock_path = _lock_path(tmp_path)
    environment = _environment(tmp_path, timeout=10.0)
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from ravel.state.db_lock import DatabaseLock\n"
        "with DatabaseLock(Path(sys.argv[1]), timeout=10):\n"
        "    pass\n"
    )
    with DatabaseLock(lock_path, timeout=0.5):
        process = subprocess.Popen(
            [sys.executable, "-c", script, str(lock_path)],
            cwd=str(REPO_ROOT),
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.75)
        assert process.poll() is None
    _stdout, stderr = process.communicate(timeout=10)
    assert process.returncode == 0, stderr


def test_lock_dir_is_the_shared_parent_for_a_worktree(tmp_path: Path) -> None:
    environment = _environment(tmp_path)
    assert lock_dir(environment) == tmp_path / "locks"
    assert _lock_path(tmp_path).parent == tmp_path / "locks"
