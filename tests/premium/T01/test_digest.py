"""T01: source digest is deterministic and excludes only development records."""

from __future__ import annotations

import subprocess
from pathlib import Path

import premium_task


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _commit(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t01@example.invalid")
    _git(repo, "config", "user.name", "T01")
    (repo / "src").mkdir()
    (repo / "src" / "product.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repo / "docs" / "premium").mkdir(parents=True)
    (repo / "docs" / "premium" / "PROGRESS.json").write_text("{}\n", encoding="utf-8")
    (repo / "docs" / "premium" / "TASKS.json").write_text("{}\n", encoding="utf-8")
    _commit(repo, "initial")
    return repo


def test_digest_ignores_record_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = premium_task.source_digest(cwd=repo)

    (repo / "docs" / "premium" / "PROGRESS.json").write_text(
        '{"status": "RUNNING"}\n', encoding="utf-8"
    )
    _commit(repo, "record only")
    after = premium_task.source_digest(cwd=repo)

    assert before == after


def test_digest_changes_when_product_changes(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    before = premium_task.source_digest(cwd=repo)

    (repo / "src" / "product.py").write_text("VALUE = 2\n", encoding="utf-8")
    _commit(repo, "product change")
    after = premium_task.source_digest(cwd=repo)

    assert before != after


def test_digest_is_sha256_hex(tmp_path: Path) -> None:
    digest = premium_task.source_digest(cwd=_repo(tmp_path))
    assert len(digest) == 64
    int(digest, 16)


def test_dirty_paths_ignores_evidence_but_not_product(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "docs" / "premium" / "evidence" / "T01").mkdir(parents=True)
    (repo / "docs" / "premium" / "evidence" / "T01" / "log.txt").write_text(
        "evidence\n", encoding="utf-8"
    )
    assert premium_task.dirty_paths(cwd=repo) == []

    (repo / "src" / "new.py").write_text("X = 1\n", encoding="utf-8")
    assert "src/new.py" in premium_task.dirty_paths(cwd=repo)
