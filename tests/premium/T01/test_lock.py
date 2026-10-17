"""T01-AC2: one exclusive database lock shared across runners."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import premium_task

TRY_LOCK = r"""
import sys
from pathlib import Path
from premium_task import DatabaseLock

try:
    with DatabaseLock(Path(sys.argv[1]), timeout=0.5):
        pass
except TimeoutError:
    raise SystemExit(7)
raise SystemExit(0)
"""


def _try_lock(lock: Path, scripts: Path) -> int:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(scripts)
    result = subprocess.run(
        [sys.executable, "-c", TRY_LOCK, str(lock)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    return result.returncode


def test_second_runner_cannot_enter_while_lock_held(tmp_path: Path) -> None:
    lock = tmp_path / "database.lock"
    assert premium_task.__file__ is not None
    scripts = Path(premium_task.__file__).resolve().parent

    with premium_task.DatabaseLock(lock, timeout=0.5):
        assert _try_lock(lock, scripts) == 7

    assert _try_lock(lock, scripts) == 0


def test_lock_dir_honors_injected_environment(tmp_path: Path) -> None:
    env = {
        "RAVEL_PREMIUM_LOCK_DIR": str(tmp_path),
        "RAVEL_POSTGRES_HOST": "127.0.0.1",
        "RAVEL_POSTGRES_PORT": "55432",
        "RAVEL_POSTGRES_DB": "ravel_test",
    }
    assert premium_task.lock_dir(env) == tmp_path
    assert premium_task.database_lock_path(env).parent == tmp_path


def test_lock_path_is_stable_and_database_specific() -> None:
    first = premium_task.database_lock_path(
        {
            "RAVEL_POSTGRES_HOST": "127.0.0.1",
            "RAVEL_POSTGRES_PORT": "55432",
            "RAVEL_POSTGRES_DB": "ravel_test",
        }
    )
    second = premium_task.database_lock_path(
        {
            "RAVEL_POSTGRES_HOST": "127.0.0.1",
            "RAVEL_POSTGRES_PORT": "55432",
            "RAVEL_POSTGRES_DB": "ravel_test",
        }
    )
    other = premium_task.database_lock_path(
        {
            "RAVEL_POSTGRES_HOST": "127.0.0.1",
            "RAVEL_POSTGRES_PORT": "55432",
            "RAVEL_POSTGRES_DB": "other_test",
        }
    )
    assert first == second
    assert first != other
