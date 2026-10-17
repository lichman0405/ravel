"""T01-AC3/AC4: the task runner fails closed and leaves real evidence."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import premium_task

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"
RUNNER = SCRIPTS / "premium_task.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True
    ).stdout.strip()


def _task(
    repo: Path,
    *,
    commands: list[dict[str, object]],
    acceptance_ids: list[str] | None = None,
    test_nodeids: list[str] | None = None,
    manual_evidence_refs: list[str] | None = None,
) -> None:
    spec = {
        "schema_version": 1,
        "tasks": {
            "T99": {
                "acceptance_ids": acceptance_ids if acceptance_ids is not None else ["T99-AC1"],
                "gate_entrypoints": ["python scripts/premium_task.py T99"],
                "classification": "internal",
                "status": "NOT_RUN",
                "test_nodeids": (
                    test_nodeids
                    if test_nodeids is not None
                    else ["tests/x.py::test_x"]
                ),
                "manual_evidence_refs": manual_evidence_refs or [],
                "execution_commands": commands,
            }
        },
    }
    (repo / "docs" / "premium").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "premium" / "acceptance-map.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "acceptance map")


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t01@example.invalid")
    _git(repo, "config", "user.name", "T01")
    (repo / "README.md").write_text("# temporary\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["RAVEL_PREMIUM_REPO_ROOT"] = str(repo)
    return subprocess.run(
        [sys.executable, str(RUNNER), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def _manifest(repo: Path) -> dict[str, Any]:
    paths = sorted((repo / "docs" / "premium" / "evidence" / "T99").glob("*/manifest.json"))
    assert len(paths) == 1, paths
    return json.loads(paths[0].read_text(encoding="utf-8"))


def test_failing_command_fails_the_task_and_is_recorded(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _task(
        repo,
        commands=[
            {
                "argv": [sys.executable, "-c", "import sys; sys.exit(3)"],
                "allow_skips": False,
            }
        ],
    )

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 1, result.stderr
    manifest = _manifest(repo)
    assert manifest["result"] == "FAIL"
    assert manifest["commands"][0]["exit_code"] == 3
    assert manifest["junit_path"] is None


def test_empty_pytest_selection_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "tests" / "empty").mkdir(parents=True)
    _task(
        repo,
        commands=[
            {
                "argv": [sys.executable, "-m", "pytest", "tests/empty", "-q"],
                "allow_skips": False,
            }
        ],
    )

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 1
    manifest = _manifest(repo)
    assert manifest["commands"][0]["empty_selection"] is True
    assert "empty test selection" in manifest["commands"][0]["failure_reason"]


def test_pytest_skip_is_not_a_pass(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tests = repo / "tests" / "skipped"
    tests.mkdir(parents=True)
    (tests / "test_skip.py").write_text(
        "import pytest\n\n@pytest.mark.skip(reason='not run')\ndef test_x():\n    assert True\n",
        encoding="utf-8",
    )
    _task(
        repo,
        commands=[
            {
                "argv": [sys.executable, "-m", "pytest", "tests/skipped", "-q"],
                "allow_skips": False,
            }
        ],
    )

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 1
    manifest = _manifest(repo)
    assert manifest["commands"][0]["skipped"] == 1
    assert "skipped" in manifest["commands"][0]["failure_reason"]


def test_unmapped_acceptance_criteria_fails_before_running(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _task(
        repo,
        commands=[{"argv": [sys.executable, "-c", "print('must not run')"]}],
        test_nodeids=[],
        manual_evidence_refs=[],
    )

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 2
    assert "not mapped" in result.stderr


def test_no_commands_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _task(repo, commands=[])

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 2
    assert "no execution_commands" in result.stderr


def test_dirty_tree_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _task(
        repo,
        commands=[{"argv": [sys.executable, "-c", "print('ok')"]}],
    )
    (repo / "src.py").write_text("X = 1\n", encoding="utf-8")

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 2
    assert "not clean" in result.stderr


def test_manifest_binds_commit_digest_and_artifacts(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _task(
        repo,
        commands=[
            {"argv": [sys.executable, "-c", "print('hello')"]},
        ],
    )

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 0, result.stderr
    manifest = _manifest(repo)
    assert manifest["source_commit"] == _git(repo, "rev-parse", "HEAD")
    assert len(str(manifest["source_digest"])) == 64
    assert manifest["result"] == "PASS"
    assert manifest["artifacts"]
    assert manifest["log_path"].endswith("commands.txt")


def test_database_suite_is_auto_locked(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _task(
        repo,
        commands=[
            {
                "argv": [sys.executable, "-c", "print('tests/integration/probe')"],
                "needs_database": False,
            }
        ],
    )
    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 0, result.stderr
    manifest = _manifest(repo)
    assert manifest["database_lock"] is True


def test_preflight_failure_is_not_pass(tmp_path: Path, monkeypatch) -> None:
    repo = _repo(tmp_path)
    _task(
        repo,
        commands=[{"argv": [sys.executable, "-c", "print('must not run')"]}],
    )
    premium_root = repo / "docs" / "premium"
    monkeypatch.setattr(premium_task, "REPO_ROOT", repo)
    monkeypatch.setattr(premium_task, "PREMIUM_ROOT", premium_root)
    monkeypatch.setattr(premium_task, "ACCEPTANCE_MAP", premium_root / "acceptance-map.json")
    monkeypatch.setattr(premium_task, "EVIDENCE_ROOT", premium_root / "evidence")
    monkeypatch.setattr(premium_task, "run_preflight", lambda: 1)

    assert premium_task.run_task("T99") == 2
    manifest = _manifest(repo)
    assert manifest["result"] == "FAIL"
    assert manifest["blocked_reason"] == "preflight failed"
    assert manifest["source_commit"] == _git(repo, "rev-parse", "HEAD")


def test_digest_subcommand_prints_sha256(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    result = _run(repo, "digest")
    assert result.returncode == 0
    assert len(result.stdout.strip()) == 64
    int(result.stdout.strip(), 16)


def test_unknown_task_fails(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    _task(repo, commands=[{"argv": [sys.executable, "-c", "print('ok')"]}])
    result = _run(repo, "T98", "--skip-preflight")
    assert result.returncode != 0
    assert "unknown task" in (result.stderr + result.stdout)


def test_pytest_junit_is_created(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    tests = repo / "tests" / "passing"
    tests.mkdir(parents=True)
    (tests / "test_ok.py").write_text("def test_ok():\n    assert 1 == 1\n", encoding="utf-8")
    _task(
        repo,
        commands=[
            {
                "argv": [sys.executable, "-m", "pytest", "tests/passing", "-q"],
                "allow_skips": False,
            }
        ],
    )

    result = _run(repo, "T99", "--skip-preflight")
    assert result.returncode == 0, result.stderr
    manifest = _manifest(repo)
    assert manifest["junit_path"] is not None
    assert (repo / str(manifest["junit_path"])).is_file()
