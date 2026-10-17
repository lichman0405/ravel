#!/usr/bin/env python3
"""Premium task runner: preflight, source digest, evidence and one task gate.

Commands
--------
    python scripts/premium_task.py digest
    python scripts/premium_task.py inventory
    python scripts/premium_task.py TASK --skip-preflight
    python scripts/premium_task.py TASK --profile internal

The runner reads ``docs/premium/acceptance-map.json``.  Each
``execution_commands`` entry is either a string (run through the shell) or an
object::

    {"argv": [".venv/bin/python", "-m", "pytest", "tests/premium/T01", "-q"],
     "allow_skips": false,
     "needs_database": false,
     "external": false,
     "env": {"NAME": "value"}}

It never calls ``make premium-task`` recursively.  A command that collects no
tests, a task with an unmapped AC, or a pytest run that skips anything fails the
run.  The manifest is written under
``docs/premium/evidence/<task>/<run-id>/manifest.json``.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ravel.state.db_lock import DatabaseLock, database_lock_path, lock_dir

REPO_ROOT = Path(os.environ.get("RAVEL_PREMIUM_REPO_ROOT", Path(__file__).resolve().parents[1]))
PREMIUM_ROOT = REPO_ROOT / "docs" / "premium"
ACCEPTANCE_MAP = PREMIUM_ROOT / "acceptance-map.json"
PROGRESS = PREMIUM_ROOT / "PROGRESS.json"
LEGACY_INVENTORY = PREMIUM_ROOT / "legacy-inventory.json"
#: Tests that exercise the runner itself must not leave probe manifests in the
#: task evidence tree; production runs use the fixed repository path.
EVIDENCE_ROOT = Path(
    os.environ.get(
        "RAVEL_PREMIUM_EVIDENCE_ROOT", str(PREMIUM_ROOT / "evidence")
    )
)

SHANGHAI = timezone(timedelta(hours=8))

#: Files that record development progress rather than product/spec content.
#: source_digest is defined over the remaining tracked tree.
EXCLUDED_PREFIXES = (
    "docs/premium/PROGRESS.json",
    "docs/premium/PROGRESS.md",
    "docs/premium/DEVLOG.md",
    "docs/premium/FILE_LOCKS.json",
    "docs/premium/BLOCKERS.md",
    "docs/premium/tasklogs/",
    "docs/premium/reviews/",
    "docs/premium/evidence/",
    "docs/premium/BASELINE.md",
    "docs/premium/ROADMAP_STATUS.md",
    "docs/premium/INTERNAL_ACCEPTANCE.md",
    "docs/premium/FINAL_REPORT.md",
    "docs/premium/RELEASE_MANIFEST.json",
    "docs/premium/PACKAGE_CHECK.json",
    "docs/premium/PACKAGE_MANIFEST.json",
    "docs/premium/完整任务书_汇总.md",
)

#: Untracked paths a task run may legitimately leave behind.
EVIDENCE_PREFIXES = ("docs/premium/evidence/", "docs/premium/reviews/")


def now_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def now_shanghai() -> str:
    return datetime.now(SHANGHAI).strftime("%Y-%m-%d %H:%M:%S %z")


def run_id() -> str:
    return datetime.now(SHANGHAI).strftime("run-%Y%m%d-%H%M%S")


def git(
    *args: str, cwd: Path | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd or REPO_ROOT),
        capture_output=True,
        text=True,
        check=check,
    )


def git_head(cwd: Path | None = None) -> str:
    return git("rev-parse", "HEAD", cwd=cwd).stdout.strip()


def source_digest(rev: str = "HEAD", cwd: Path | None = None) -> str:
    """SHA256 over (mode, type, blob, path) for every non-record tracked path."""
    cwd = cwd or REPO_ROOT
    listing = git("ls-tree", "-r", rev, cwd=cwd).stdout.splitlines()
    rows: list[str] = []
    for line in listing:
        meta, path = line.split("\t", 1)
        mode, kind, blob = meta.split()
        if any(path == prefix or path.startswith(prefix) for prefix in EXCLUDED_PREFIXES):
            continue
        rows.append(f"{mode} {kind} {blob} {path}")
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def dirty_paths(cwd: Path | None = None) -> list[str]:
    cwd = cwd or REPO_ROOT
    output = git("status", "--porcelain=v1", "--untracked-files=all", cwd=cwd).stdout
    paths: list[str] = []
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if any(path.startswith(prefix) for prefix in EVIDENCE_PREFIXES):
            continue
        paths.append(path)
    return paths


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def task_spec(task_id: str) -> dict[str, Any]:
    mapping = load_json(ACCEPTANCE_MAP)
    try:
        return mapping["tasks"][task_id]
    except KeyError as error:
        raise SystemExit(f"unknown task {task_id!r}; not in {ACCEPTANCE_MAP}") from error


def normalize_command(spec: str | dict[str, Any]) -> dict[str, Any]:
    if isinstance(spec, str):
        command = {
            "command": spec,
            "shell": True,
            "allow_skips": False,
            "needs_database": False,
            "external": False,
            "env": {},
        }
    elif isinstance(spec, dict):
        command = {
            "argv": spec.get("argv"),
            "command": spec.get("command"),
            "shell": bool(spec.get("shell", spec.get("command") is not None)),
            "allow_skips": bool(spec.get("allow_skips", False)),
            "needs_database": bool(spec.get("needs_database", False)),
            "external": bool(spec.get("external", False)),
            "env": dict(spec.get("env", {})),
        }
        if command["argv"] is None and command["command"] is None:
            raise SystemExit(f"command entry has neither argv nor command: {spec!r}")
        if isinstance(command["argv"], str):
            command["argv"] = [command["argv"]]
    else:
        raise SystemExit(f"invalid command entry: {spec!r}")
    return command


def _is_pytest(command: dict[str, Any]) -> bool:
    if command.get("argv"):
        return any(Path(part).name in {"pytest", "py.test"} for part in command["argv"][:3])
    return bool(re.search(r"(^|[\s/])pytest(\s|$)", command.get("command", "")))


def _append_pytest_options(command: dict[str, Any], junit_path: Path) -> dict[str, Any]:
    command = dict(command)
    if command.get("argv") is not None:
        argv = list(command["argv"])
        if not any(arg.startswith("--junitxml") for arg in argv):
            argv.append(f"--junitxml={junit_path}")
        if not any(arg in {"-r", "-rs", "-rxXs"} or arg.startswith("-r") for arg in argv):
            argv.append("-rs")
        command["argv"] = argv
    else:
        suffix = f" --junitxml={junit_path} -rs"
        if "--junitxml" not in command["command"]:
            command["command"] += suffix
    return command


def _pytest_counts(log: str) -> tuple[int, int, int]:
    passed = failed = skipped = 0
    match = re.search(r"(\d+) passed", log)
    if match:
        passed = int(match.group(1))
    match = re.search(r"(\d+) failed", log)
    if match:
        failed = int(match.group(1))
    match = re.search(r"(\d+) skipped", log)
    if match:
        skipped = int(match.group(1))
    return passed, failed, skipped


def _empty_selection(log: str, exit_code: int) -> bool:
    if exit_code == 5:  # pytest: no tests collected
        return True
    patterns = (
        r"collected 0 items",
        r"no tests ran",
        r"no tests collected",
    )
    return any(re.search(pattern, log, re.IGNORECASE) for pattern in patterns)


#: A command naming one of these trees can truncate the shared test database.
#: The runner takes the system-wide lock even if the command author forgot
#: ``needs_database``; a declaration is a hint, not the safety mechanism.
DATABASE_PATH_MARKERS = ("tests/integration", "tests/e2e", "tests/acceptance")


#: pytest options that consume the following token. If an option is not here,
#: it is treated as a flag and only its own token is skipped.
PYTEST_VALUE_OPTIONS = {
    "-c",
    "-k",
    "-m",
    "-o",
    "-p",
    "--basetemp",
    "--capture",
    "--color",
    "--confcutdir",
    "--cov",
    "--cov-config",
    "--cov-fail-under",
    "--cov-report",
    "--deselect",
    "--ignore",
    "--ignore-glob",
    "--import-mode",
    "--junitxml",
    "--log-date-format",
    "--log-file",
    "--log-format",
    "--log-level",
    "--maxfail",
    "--rootdir",
    "--tb",
    "--timeout",
}


def _command_tokens(command: dict[str, Any]) -> list[str]:
    if command.get("argv"):
        return [str(part) for part in command["argv"]]
    text = str(command.get("command") or "")
    try:
        return shlex.split(text)
    except ValueError:
        return text.split()


def _pytest_positionals(tokens: list[str]) -> list[str]:
    start: int | None = None
    for index, token in enumerate(tokens):
        if Path(token).name in {"pytest", "py.test"}:
            start = index + 1
            break
        if (
            token == "-m"
            and index + 1 < len(tokens)
            and Path(tokens[index + 1]).name in {"pytest", "py.test"}
        ):
            start = index + 2
            break
    if start is None:
        return []

    positionals: list[str] = []
    index = start
    while index < len(tokens):
        token = tokens[index]
        if token == "--":
            positionals.extend(tokens[index + 1 :])
            break
        if token.startswith("-"):
            option = token.split("=", 1)[0]
            index += 2 if "=" not in token and option in PYTEST_VALUE_OPTIONS else 1
            continue
        positionals.append(token)
        index += 1
    return positionals


def _database_target(target: str) -> bool:
    path = target.replace("\\", "/").strip().strip("'\"").rstrip("/")
    path = path.split("::", 1)[0]
    parts = [part for part in path.split("/") if part not in {"", "."}]
    if not parts:
        return False
    if parts == ["tests"]:
        return True
    normalized = "/".join(parts)
    for marker in DATABASE_PATH_MARKERS:
        if normalized == marker or normalized.startswith(marker + "/"):
            return True
        if marker.startswith(normalized + "/"):
            return True
    return False


def command_uses_database(command: dict[str, Any]) -> bool:
    tokens = _command_tokens(command)
    if any(_database_target(target) for target in _pytest_positionals(tokens)):
        return True
    # A path marker may also appear inside a shell string or a `python -c`
    # argument; the structural parser above only sees real argv tokens.
    text = " ".join(tokens).replace("\\", "/").lower()
    return any(
        re.search(rf"(^|[\s'\"=/]){re.escape(marker)}(/|[\s'\"]|$)", text)
        for marker in DATABASE_PATH_MARKERS
    )



def run_preflight() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import premium_preflight

    return premium_preflight.main(["--repo-root", str(REPO_ROOT)])


@dataclass
class CommandResult:
    index: int
    command: str
    exit_code: int
    log_path: str
    junit_path: str | None
    empty_selection: bool
    skipped: int
    ok: bool
    failure_reason: str | None


def run_command(
    command: dict[str, Any],
    index: int,
    run_dir: Path,
    *,
    task_id: str,
    run_identifier: str,
) -> CommandResult:
    junit_path = run_dir / f"cmd-{index:02d}-junit.xml"
    command = _append_pytest_options(command, junit_path) if _is_pytest(command) else command
    log_path = run_dir / f"cmd-{index:02d}.txt"
    env = os.environ.copy()
    env.update({str(key): str(value) for key, value in command.get("env", {}).items()})
    env["RAVEL_PREMIUM_TASK"] = task_id
    env["RAVEL_PREMIUM_RUN_ID"] = run_identifier

    if command.get("argv") is not None:
        argv = [str(part) for part in command["argv"]]
        display = " ".join(argv)
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {display}\n\n")
            log.flush()
            result = subprocess.run(
                argv,
                cwd=str(REPO_ROOT),
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
    else:
        display = str(command["command"])
        with log_path.open("w", encoding="utf-8") as log:
            log.write(f"$ {display}\n\n")
            log.flush()
            result = subprocess.run(
                display,
                cwd=str(REPO_ROOT),
                env=env,
                shell=True,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )

    log_text = log_path.read_text(encoding="utf-8", errors="replace")
    empty = _empty_selection(log_text, result.returncode) if _is_pytest(command) else False
    _, _, skipped = _pytest_counts(log_text) if _is_pytest(command) else (0, 0, 0)
    reason: str | None = None
    if empty:
        reason = "empty test selection"
    elif result.returncode != 0:
        reason = f"exit code {result.returncode}"
    elif skipped and not command.get("allow_skips") and not command.get("external"):
        reason = f"{skipped} skipped test(s) are not a PASS"
    ok = reason is None
    return CommandResult(
        index=index,
        command=display,
        exit_code=result.returncode,
        log_path=str(log_path.relative_to(REPO_ROOT)),
        junit_path=str(junit_path.relative_to(REPO_ROOT)) if junit_path.is_file() else None,
        empty_selection=empty,
        skipped=skipped,
        ok=ok,
        failure_reason=reason,
    )


def write_manifest(
    *,
    task_id: str,
    identifier: str,
    run_dir: Path,
    task: dict[str, Any],
    results: list[CommandResult],
    result: str,
    blocked_reason: str | None,
    preflight_exit: int | None,
    database_lock: bool,
) -> Path:
    artifacts = []
    for path in sorted(run_dir.iterdir()):
        if not path.is_file() or path.name == "manifest.json":
            continue
        data = path.read_bytes()
        artifacts.append(
            {
                "ref": str(path.relative_to(REPO_ROOT)),
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        )
    manifest = {
        "run_id": identifier,
        "task_id": task_id,
        "acceptance_map": {
            acceptance_id: {
                "test_nodeids": task.get("test_nodeids", []),
                "manual_evidence_refs": task.get("manual_evidence_refs", []),
            }
            for acceptance_id in task.get("acceptance_ids", [])
        },
        "started_at": started_at,
        "completed_at": now_shanghai(),
        "source_commit": git_head(),
        "source_digest": source_digest(),
        "platform": sys.platform,
        "dependencies": {
            "python": sys.version.split()[0],
            "docker": _tool_version("docker", "--version"),
            "compose": _tool_version("docker", "compose", "version"),
            "git": _tool_version("git", "--version"),
        },
        "commands": [
            {
                "command": item.command,
                "exit_code": item.exit_code,
                "empty_selection": item.empty_selection,
                "skipped": item.skipped,
                "failure_reason": item.failure_reason,
            }
            for item in results
        ],
        "cwd": str(REPO_ROOT),
        "exit_code": 0 if result == "PASS" else 1,
        "junit_path": next((item.junit_path for item in results if item.junit_path), None),
        "log_path": str((run_dir / "commands.txt").relative_to(REPO_ROOT)),
        "artifacts": artifacts,
        "data_mode": "REAL",
        "external_resources": [],
        "operator_authorization_ref": None,
        "result": result,
        "blocked_reason": blocked_reason,
        "preflight_exit": preflight_exit,
        "database_lock": database_lock,
    }
    path = run_dir / "manifest.json"
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _tool_version(*argv: str) -> str | None:
    binary = shutil.which(argv[0])
    if binary is None:
        return None
    result = subprocess.run(
        [binary, *argv[1:]], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return None
    output = (result.stdout or result.stderr).strip()
    return output.splitlines()[0] if output else None


def _write_combined_log(run_dir: Path, results: list[CommandResult]) -> None:
    with (run_dir / "commands.txt").open("w", encoding="utf-8") as combined:
        for item in results:
            combined.write(f"===== command {item.index}: {item.command} =====\n")
            combined.write(
                f"exit_code={item.exit_code} ok={item.ok} "
                f"reason={item.failure_reason}\n"
            )
            log = REPO_ROOT / item.log_path
            combined.write(log.read_text(encoding="utf-8", errors="replace"))
            combined.write("\n")


def run_task(
    task_id: str,
    *,
    skip_preflight: bool = False,
    allow_dirty: bool = False,
    force_database_lock: bool = False,
    profile: str = "internal",
) -> int:
    global started_at
    started_at = now_shanghai()
    task = task_spec(task_id)
    acceptance_ids = task.get("acceptance_ids", [])
    commands = [normalize_command(spec) for spec in task.get("execution_commands", [])]

    if not acceptance_ids:
        print(f"{task_id}: no acceptance_ids mapped", file=sys.stderr)
        return 2
    if not task.get("test_nodeids") and not task.get("manual_evidence_refs"):
        print(f"{task_id}: ACs are not mapped to test nodeids or manual evidence", file=sys.stderr)
        return 2
    if not commands:
        print(f"{task_id}: no execution_commands mapped", file=sys.stderr)
        return 2
    if not allow_dirty:
        dirty = dirty_paths()
        if dirty:
            print(f"{task_id}: working tree is not clean: {', '.join(dirty[:10])}", file=sys.stderr)
            return 2

    identifier = run_id()
    run_dir = EVIDENCE_ROOT / task_id / identifier
    run_dir.mkdir(parents=True, exist_ok=False)

    preflight_exit: int | None = None
    if not skip_preflight:
        preflight_exit = run_preflight()
        if preflight_exit != 0:
            _write_combined_log(run_dir, [])
            manifest = write_manifest(
                task_id=task_id,
                identifier=identifier,
                run_dir=run_dir,
                task=task,
                results=[],
                result="FAIL",
                blocked_reason="preflight failed",
                preflight_exit=preflight_exit,
                database_lock=False,
            )
            print(f"{task_id}: preflight failed; manifest {manifest}", file=sys.stderr)
            return 2

    # Fail safe: an undeclared task is treated as database-touching. A task
    # must explicitly declare database_suite=false in acceptance-map.json to
    # opt out, and marker detection still overrides that declaration.
    task_needs_database = task.get("database_suite", True) is not False
    needs_database = force_database_lock or task_needs_database or any(
        c["needs_database"] or command_uses_database(c) for c in commands
    )
    lock_context: Iterator[None] = (
        DatabaseLock() if needs_database else contextlib.nullcontext()
    )

    results: list[CommandResult] = []
    try:
        with lock_context:
            for index, command in enumerate(commands, start=1):
                print(f"{task_id}: running command {index}/{len(commands)}")
                results.append(
                    run_command(
                        command,
                        index,
                        run_dir,
                        task_id=task_id,
                        run_identifier=identifier,
                    )
                )
    except TimeoutError as error:
        _write_combined_log(run_dir, results)
        manifest = write_manifest(
            task_id=task_id,
            identifier=identifier,
            run_dir=run_dir,
            task=task,
            results=results,
            result="FAIL",
            blocked_reason=str(error),
            preflight_exit=preflight_exit,
            database_lock=True,
        )
        print(f"{task_id}: {error}; manifest {manifest}", file=sys.stderr)
        return 2

    _write_combined_log(run_dir, results)
    failed = [item for item in results if not item.ok]
    result = "PASS" if not failed else "FAIL"
    manifest = write_manifest(
        task_id=task_id,
        identifier=identifier,
        run_dir=run_dir,
        task=task,
        results=results,
        result=result,
        blocked_reason=None if not failed else "; ".join(
            f"cmd {item.index}: {item.failure_reason}" for item in failed
        ),
        preflight_exit=preflight_exit,
        database_lock=needs_database,
    )
    print(
        json.dumps(
            {
                "task_id": task_id,
                "result": result,
                "run_id": identifier,
                "manifest": str(manifest.relative_to(REPO_ROOT)),
                "failed_commands": [item.index for item in failed],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result == "PASS" else 1


class _Collector:
    """pytest plugin that records nodeids and their markers."""

    def __init__(self) -> None:
        self.nodeids: list[str] = []
        self.markers: dict[str, set[str]] = {}

    def pytest_collection_finish(self, session: Any) -> None:
        for item in session.items:
            self.nodeids.append(item.nodeid)
            self.markers[item.nodeid] = {marker.name for marker in item.iter_markers()}


def collect_inventory() -> int:
    """Collect every pytest nodeid and classify it internal/external."""
    import pytest

    collector = _Collector()
    cmd = [
        "--collect-only",
        "-q",
        "--import-mode=importlib",
        "-p",
        "no:cacheprovider",
    ]
    exit_code = pytest.main(cmd, plugins=[collector])
    nodeids = sorted(collector.nodeids)
    if exit_code != 0 and not nodeids:
        print("pytest collection failed; no nodeids were collected", file=sys.stderr)
        return 1

    external_prefixes = ("tests/live_research/", "tests/dsh/")
    external_exact = {
        "tests/acceptance/test_phase10_live.py",
        "tests/acceptance/test_phase10_dsh.py",
    }
    tests = []
    for nodeid in nodeids:
        path = nodeid.split("::", 1)[0]
        markers = collector.markers.get(nodeid, set())
        marker = "live" if "live" in markers else "dsh" if "dsh" in markers else None
        if marker is not None or path.startswith(external_prefixes):
            classification = "external"
            reason = "requires a real model, real Internet, or a pinned live DSH runtime"
            resources = ["DEEPSEEK_API_KEY", "internet", "DSH runtime"]
        elif path in external_exact or path == "tests/acceptance/test_research.py":
            classification = "external"
            reason = "live research or live harness acceptance"
            resources = ["DEEPSEEK_API_KEY", "internet"]
        else:
            classification = "internal"
            reason = "deterministic local test"
            resources = []
            if path.startswith("tests/integration/"):
                resources = ["postgres", "temporal", "minio"]
            elif path.startswith("tests/e2e/"):
                resources = ["postgres", "temporal", "minio", "playwright"]
        tests.append(
            {
                "nodeid": nodeid,
                "classification": classification,
                "reason": reason,
                "marker": marker,
                "external_resources": resources,
            }
        )
    inventory = {
        "schema_version": 1,
        "status": "COLLECTED",
        "collected_at": now_shanghai(),
        "source_sha": git_head(),
        "source_digest": source_digest(),
        "collection_command": "python -m pytest " + " ".join(cmd),
        "collection_exit": exit_code,
        "tests": tests,
    }
    LEGACY_INVENTORY.write_text(
        json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "collected": len(tests),
                "internal": sum(t["classification"] == "internal" for t in tests),
                "external": sum(t["classification"] == "external" for t in tests),
                "path": str(LEGACY_INVENTORY.relative_to(REPO_ROOT)),
            },
            ensure_ascii=False,
        )
    )
    return 0 if exit_code == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task", nargs="?", help="T00..T29, or 'digest'/'inventory'")
    parser.add_argument("--profile", default="internal", choices=["internal", "certified"])
    parser.add_argument("--skip-preflight", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--allow-dirty", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--database-lock", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    if args.task == "digest":
        digest = source_digest()
        if args.json:
            print(json.dumps({"source_commit": git_head(), "source_digest": digest}))
        else:
            print(digest)
        return 0
    if args.task == "inventory":
        return collect_inventory()
    if not args.task:
        parser.error("task id required (or 'digest'/'inventory')")
    return run_task(
        args.task,
        skip_preflight=args.skip_preflight,
        allow_dirty=args.allow_dirty,
        force_database_lock=args.database_lock,
        profile=args.profile,
    )


if __name__ == "__main__":
    raise SystemExit(main())
