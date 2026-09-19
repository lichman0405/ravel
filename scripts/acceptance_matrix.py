#!/usr/bin/env python3
"""Run the acceptance suite and print the A01-A20 pass/fail matrix.

    .venv/bin/python scripts/acceptance_matrix.py

`acceptance/V0_ACCEPTANCE.md` opens with "V0 is not complete until all 20 items
pass", and the file's other half is a list of extra gates that "cannot be
skipped". A run of pytest answers a different question from the one that
sentence asks: it says how many tests passed, not which items they were about,
and it says nothing at all about an item nobody wrote a test for. This script
asks the item-level question.

How each row is decided. Every case in `tests/acceptance` is named for the item
it demonstrates — `test_a14_...`, `test_gate_3_...` — so the node id is the
mapping, and there is no second table to keep in step with the test names. An
item's row is FAIL if any of its cases failed or errored, SKIP if every one of
them skipped, and PASS otherwise. An item with no cases at all is a FAIL rather
than a blank: an acceptance item nobody tests is the one failure this whole file
exists to make visible, and it looks exactly like success from every other
angle.

Skipped is reported as its own outcome, not folded into pass. A03 and A04 skip
without `RAVEL_RESEARCH_CONTACT_EMAIL`, because RAVEL refuses to fetch
anonymously — a real gap in coverage, and one that has to be legible as a gap.

The suite is run with `--junitxml` because that is a format pytest already
writes and nothing here has to be kept in step with a plugin's internals. Its
output streams to the terminal as it goes: a run that fails takes minutes to
find out about, and a script that swallowed the failure output until the end
would make the matrix the only thing anybody could read.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_DOC = REPO_ROOT / "acceptance" / "V0_ACCEPTANCE.md"
SUITE = REPO_ROOT / "tests" / "acceptance"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

#: The seven gates, in the order the acceptance file lists them. The names are
#: the file's, shortened; the numbering is this script's, and matches the
#: docstrings in `tests/acceptance/test_gates.py`.
GATES = (
    "live Research reaches real original sources",
    "fabricated DOI/source is refused",
    "mock compute/lab data is marked simulated",
    "DSH is pinned",
    "all 5 roles use the intended preset and tool scope",
    "no direct public DSH endpoint",
    "Postgres remains authoritative after restarts",
)


@dataclass
class Item:
    """One acceptance item, and the cases that demonstrate it."""

    item_id: str
    title: str
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    #: Which test files the cases came from, so a FAIL row says where to look
    #: without a second command.
    where: set[str] = field(default_factory=set)
    #: What the skipped cases said they were waiting for, in pytest's own
    #: words. Printed under the matrix, so a SKIP row reads as "set this and it
    #: runs" rather than as a result nobody can act on.
    waiting_on: set[str] = field(default_factory=set)

    @property
    def outcome(self) -> str:
        if self.failed:
            return "FAIL"
        if self.passed:
            return "PASS"
        if self.skipped:
            return "SKIP"
        return "MISSING"

    @property
    def colour(self) -> str:
        return {"PASS": GREEN, "FAIL": RED, "SKIP": YELLOW}.get(self.outcome, RED)


def items_from_doc() -> list[Item]:
    """The twenty items, read off the acceptance file rather than restated.

    So that a document that grew a twenty-first item, or renamed one, is
    reflected here without anybody remembering to edit this script — and so
    that an item the file dropped stops being reported as passing.
    """
    items: list[Item] = []
    for line in ACCEPTANCE_DOC.read_text(encoding="utf-8").splitlines():
        if line.startswith("## A"):
            item_id, _, title = line.removeprefix("## ").partition(" ")
            items.append(Item(item_id=item_id, title=title))
    assert len(items) == 20, f"{ACCEPTANCE_DOC} describes {len(items)} items, not 20"
    return items


def run_suite() -> tuple[int, Path]:
    """Run the acceptance suite, streaming its output, and return its XML."""
    handle = tempfile.NamedTemporaryFile(suffix=".xml", delete=False)  # noqa: SIM115
    handle.close()
    report = Path(handle.name)
    completed = subprocess.run(
        [
            str(REPO_ROOT / ".venv" / "bin" / "python"),
            "-m",
            "pytest",
            str(SUITE),
            "-m",
            "acceptance",
            f"--junitxml={report}",
            "-q",
            "--strict-markers",
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    return completed.returncode, report


def record(items: dict[str, Item], report: Path) -> None:
    """Fold one JUnit report into the items."""
    for case in ElementTree.parse(report).iter("testcase"):
        item_id = _item_of(case.get("name", ""))
        if item_id is None:
            continue
        item = items.get(item_id)
        if item is None:
            # A case named for an item the acceptance file does not describe.
            # Kept rather than dropped: a test demonstrating an item nobody
            # wrote down is a test whose subject has been forgotten.
            item = items.setdefault(item_id, Item(item_id=item_id, title="(not in the matrix)"))
        item.where.add(Path(case.get("classname", "").replace(".", "/")).name)
        if case.find("failure") is not None or case.find("error") is not None:
            item.failed += 1
            continue
        skip = case.find("skipped")
        if skip is not None:
            item.skipped += 1
            message = (skip.get("message") or "").strip().splitlines()
            if message:
                item.waiting_on.add(" ".join(message[0].split()))
            continue
        item.passed += 1


def _item_of(name: str) -> str | None:
    """The acceptance item a test function is named for, if it names one."""
    if not name.startswith("test_"):
        return None
    head = name.removeprefix("test_").split("_", 1)[0]
    if head.startswith("a") and head[1:].isdigit():
        return head.upper()
    if head == "gate":
        number = name.removeprefix("test_gate_").split("_", 1)[0]
        return f"gate {number}" if number.isdigit() else "gate"
    return None


def _case_count(item: Item) -> str:
    """How many cases an item has, by outcome."""
    parts = []
    if item.passed:
        parts.append(f"{item.passed} passed")
    if item.failed:
        parts.append(f"{item.failed} failed")
    if item.skipped:
        parts.append(f"{item.skipped} skipped")
    return ", ".join(parts) or "no tests"


def print_matrix(items: list[Item], gates: list[Item], code: int) -> None:
    """The table, then the sentences that say what to do about it."""
    width = max(len(item.title) for item in items)
    print("\n\033[1m── RAVEL V0 acceptance ──\033[0m\n")
    for item in items:
        print(
            f"  {item.colour}{item.outcome:<7}{RESET} {item.item_id}  "
            f"{item.title:<{width}}  {DIM}{_case_count(item)}{RESET}"
        )

    print("\n\033[1m── extra gates ──\033[0m\n")
    for index, (gate, item) in enumerate(zip(GATES, gates, strict=True), start=1):
        print(
            f"  {item.colour}{item.outcome:<7}{RESET} gate {index}  "
            f"{gate:<{width}}  {DIM}{_case_count(item)}{RESET}"
        )

    skipped = [item for item in (*items, *gates) if item.outcome == "SKIP"]
    missing = [item for item in (*items, *gates) if item.outcome == "MISSING"]
    failed = [item for item in (*items, *gates) if item.outcome == "FAIL"]

    if skipped:
        print(f"\n{YELLOW}Skipped, in the suite's own words:{RESET}")
        for waiting in sorted({reason for item in skipped for reason in item.waiting_on}):
            names = ", ".join(item.item_id for item in skipped if waiting in item.waiting_on)
            print(f"  {names}: {waiting}")
        print(f"  {DIM}cover these by setting what they name in .env{RESET}")

    if missing:
        names = ", ".join(item.item_id for item in missing)
        print(
            f"\n{RED}No test demonstrates {names}.{RESET} An acceptance item nobody "
            "exercises is not a passing item."
        )

    total = len(items) + len(gates)
    covering = total - len(missing)
    print(
        f"\n\033[1m{covering}/{total}\033[0m demonstrated, "
        f"{len(failed)} failed, {len(skipped)} skipped, {len(missing)} missing "
        f"{DIM}(pytest exit {code}){RESET}\n"
    )


def main() -> int:
    code, report = run_suite()
    try:
        items = items_from_doc()
        by_id = {item.item_id: item for item in items}
        record(by_id, report)

        # Every gate case is named `test_gate_N_...`, so the number in the name
        # is the row, and a gate is decided exactly the way an item is.
        gates = [Item(item_id=f"gate {index}", title=gate) for index, gate in enumerate(GATES, 1)]
        record({gate.item_id: gate for gate in gates}, report)
    finally:
        report.unlink(missing_ok=True)

    print_matrix(items, gates, code)

    # `--junitxml` can be written by a run that crashed before any case ran, so
    # the exit code is the authority on whether pytest was happy and the matrix
    # is the authority on what it covered. Either one failing fails this script.
    covered = {item.outcome for item in (*items, *gates)}
    return 1 if code or "MISSING" in covered or "FAIL" in covered else 0


if __name__ == "__main__":
    sys.exit(main())
