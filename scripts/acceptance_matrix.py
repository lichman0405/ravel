#!/usr/bin/env python3
"""Run an acceptance suite and print its item-level pass/fail matrix.

    .venv/bin/python scripts/acceptance_matrix.py                 # A01-A20
    .venv/bin/python scripts/acceptance_matrix.py --phase phase10 # P10-01..20

`acceptance/V0_ACCEPTANCE.md` opens with "V0 is not complete until all 20 items
pass", and `acceptance/PHASE10_ACCEPTANCE.md` opens with "It is not complete
while any item below is SKIP, MISSING, or FAIL". A run of pytest answers a
different question from the one those sentences ask: it says how many tests
passed, not which items they were about, and it says nothing at all about an
item nobody wrote a test for. This script asks the item-level question, for
either phase.

How each row is decided. Every case in `tests/acceptance` is named for the item
it demonstrates — `test_a14_...`, `test_gate_3_...`, `test_p10_09_...`,
`test_p10_w06_...` — so the node id is the mapping, and there is no second table
to keep in step with the test names. An item's row is FAIL if any of its cases
failed or errored, SKIP if every one of them skipped, and PASS otherwise. An
item with no cases at all is reported as MISSING rather than left blank: an
acceptance item nobody tests is the one failure this whole file exists to make
visible, and it looks exactly like success from every other angle.

Skipped is reported as its own outcome, not folded into pass. A03 and A04 skip
without `RAVEL_RESEARCH_CONTACT_EMAIL`, because RAVEL refuses to fetch
anonymously; the two Phase 10 items that need a live model skip without
`DEEPSEEK_API_KEY`, because a live turn is what they are about. Both are real
gaps in coverage, and both have to be legible as gaps.

The suite is run with `--junitxml` because that is a format pytest already
writes and nothing here has to be kept in step with a plugin's internals. Its
output streams to the terminal as it goes: a run that fails takes minutes to
find out about, and a script that swallowed the failure output until the end
would make the matrix the only thing anybody could read.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ElementTree
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE_DIR = REPO_ROOT / "acceptance"
SUITE = REPO_ROOT / "tests" / "acceptance"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

#: The seven gates of Phase 9's acceptance, in the order the file lists them.
#: The names are the file's, shortened; the numbering is this script's, and
#: matches the docstrings in `tests/acceptance/test_gates.py`.
GATES = (
    "live Research reaches real original sources",
    "fabricated DOI/source is refused",
    "mock compute/lab data is marked simulated",
    "DSH is pinned",
    "all 5 roles use the intended preset and tool scope",
    "no direct public DSH endpoint",
    "Postgres remains authoritative after restarts",
)


@dataclass(frozen=True)
class Phase:
    """One acceptance phase: its document, its marker, and its two lists."""

    name: str
    title: str
    doc: Path
    marker: str
    #: What a section heading that names an item starts with.
    heading: str
    #: How many items the document claims, so a document that lost one is
    #: caught here rather than by a reader noticing a short table.
    items_claimed: int
    #: The second list: A01-A20 has the extra gates, Phase 10 the worker-level
    #: items. `second_prefix` is set when the second list is written as a table
    #: in the document rather than restated here.
    second_title: str
    second_restated: tuple[str, ...] = ()
    second_prefix: str | None = None

    def item_of(self, test_name: str) -> str | None:
        """The item a test function is named for, if it names one."""
        return _ITEM_READERS.get(self.name, _v0_item_of)(test_name)


V0 = Phase(
    name="v0",
    title="RAVEL V0 acceptance",
    doc=ACCEPTANCE_DIR / "V0_ACCEPTANCE.md",
    marker="acceptance",
    heading="## A",
    items_claimed=20,
    second_title="extra gates",
    second_restated=GATES,
)

PHASE10 = Phase(
    name="phase10",
    title="RAVEL Phase 10 acceptance",
    doc=ACCEPTANCE_DIR / "PHASE10_ACCEPTANCE.md",
    marker="phase10",
    heading="## P10-",
    items_claimed=20,
    second_title="worker-level items",
    second_prefix="P10-W",
)

# Phase 11 is a sequence of tasks rather than a parallel set, so its document
# grows as they land: the item count is asserted against the file, which means
# an item added to the plan and not to the file is a failing assertion here
# rather than a task nobody notices went unverified. It has no second list —
# every Phase 11 item is one of the tasks.
PHASE11 = Phase(
    name="phase11",
    title="RAVEL Phase 11 acceptance",
    doc=ACCEPTANCE_DIR / "PHASE11_ACCEPTANCE.md",
    marker="phase11",
    heading="## P11-",
    items_claimed=5,
    second_title="",
)

PHASES = {phase.name: phase for phase in (V0, PHASE10, PHASE11)}


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


def items_from_doc(phase: Phase) -> list[Item]:
    """The phase's items, read off its acceptance file rather than restated.

    So that a document that grew an item, or renamed one, is reflected here
    without anybody remembering to edit this script — and so that an item the
    file dropped stops being reported as passing.
    """
    items: list[Item] = []
    for line in phase.doc.read_text(encoding="utf-8").splitlines():
        if line.startswith(phase.heading):
            item_id, _, title = line.removeprefix("## ").partition(" ")
            items.append(Item(item_id=item_id, title=title))
    assert len(items) == phase.items_claimed, (
        f"{phase.doc} describes {len(items)} items, not {phase.items_claimed}"
    )
    return items


def second_list(phase: Phase) -> list[Item]:
    """The phase's other list: the gates, or the worker-level items.

    Phase 10's twenty worker items are rows of a markdown table rather than
    sections, because they are the detail behind the items above them; they are
    read the same way and are decided by the same rule, which is the point —
    a worker item nobody wrote a case for is as visible as an item nobody wrote
    a case for.
    """
    if phase.second_prefix is None:
        return [
            Item(item_id=f"gate {index}", title=title)
            for index, title in enumerate(phase.second_restated, start=1)
        ]
    items: list[Item] = []
    for line in phase.doc.read_text(encoding="utf-8").splitlines():
        if not line.startswith(f"| {phase.second_prefix}"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        items.append(Item(item_id=cells[0], title=cells[1]))
    return items


def run_suite(phase: Phase) -> tuple[int, Path]:
    """Run the phase's suite, streaming its output, and return its XML."""
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
            phase.marker,
            f"--junitxml={report}",
            "-q",
            "--strict-markers",
        ],
        cwd=REPO_ROOT,
        check=False,
    )
    return completed.returncode, report


def record(phase: Phase, items: dict[str, Item], report: Path) -> None:
    """Fold one JUnit report into the items."""
    for case in ElementTree.parse(report).iter("testcase"):
        item_id = phase.item_of(case.get("name", ""))
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


def _v0_item_of(name: str) -> str | None:
    """The A-item a test function is named for, if it names one."""
    if not name.startswith("test_"):
        return None
    head = name.removeprefix("test_").split("_", 1)[0]
    if head.startswith("a") and head[1:].isdigit():
        return head.upper()
    if head == "gate":
        number = name.removeprefix("test_gate_").split("_", 1)[0]
        return f"gate {number}" if number.isdigit() else "gate"
    return None


def _phase10_item_of(name: str) -> str | None:
    """The P10 item a test function is named for, if it names one.

    Two shapes, and the longer one is checked first: `test_p10_w06_...` is a
    worker-level item and `test_p10_09_...` is not, so a parser that took the
    first token after `p10` would file every worker case under an item number
    that does not exist.
    """
    if not name.startswith("test_p10_"):
        return None
    parts = name.removeprefix("test_p10_").split("_")
    head = parts[0]
    if head.startswith("w") and head[1:].isdigit():
        return f"P10-W{int(head[1:]):02d}"
    if head.isdigit():
        return f"P10-{int(head):02d}"
    return None


def _phase11_item_of(name: str) -> str | None:
    """The P11 item a test function is named for, if it names one."""
    if not name.startswith("test_p11_"):
        return None
    head = name.removeprefix("test_p11_").split("_", 1)[0]
    return f"P11-{int(head):02d}" if head.isdigit() else None


#: Which reader knows a phase's test names. A phase whose naming is the V0
#: `test_a14_...` shape does not need an entry.
_ITEM_READERS = {"phase10": _phase10_item_of, "phase11": _phase11_item_of}


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


def print_rows(rows: list[Item], width: int) -> None:
    """One group's rows, the item id first so the eye can scan down it."""
    for item in rows:
        print(
            f"  {item.colour}{item.outcome:<7}{RESET} {item.item_id:<8}  "
            f"{item.title:<{width}}  {DIM}{_case_count(item)}{RESET}"
        )


def print_matrix(phase: Phase, items: list[Item], second: list[Item], code: int) -> None:
    """The tables, then the sentences that say what to do about it."""
    width = max(len(item.title) for item in (*items, *second))
    print(f"\n\033[1m── {phase.title} ──\033[0m\n")
    print_rows(items, width)
    if second:
        print(f"\n\033[1m── {phase.second_title} ──\033[0m\n")
        print_rows(second, width)

    everything = [*items, *second]
    skipped = [item for item in everything if item.outcome == "SKIP"]
    missing = [item for item in everything if item.outcome == "MISSING"]
    failed = [item for item in everything if item.outcome == "FAIL"]

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

    total = len(everything)
    covering = total - len(missing)
    print(
        f"\n\033[1m{covering}/{total}\033[0m demonstrated, "
        f"{len(failed)} failed, {len(skipped)} skipped, {len(missing)} missing "
        f"{DIM}(pytest exit {code}){RESET}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run an acceptance suite and print its item-level matrix."
    )
    parser.add_argument(
        "--phase",
        choices=sorted(PHASES),
        default="v0",
        help="which acceptance phase to run (default: v0, the A01-A20 matrix)",
    )
    phase = PHASES[parser.parse_args(argv).phase]

    code, report = run_suite(phase)
    try:
        items = items_from_doc(phase)
        second = second_list(phase)
        record(phase, {item.item_id: item for item in items}, report)
        record(phase, {item.item_id: item for item in second}, report)
    finally:
        report.unlink(missing_ok=True)

    print_matrix(phase, items, second, code)

    # `--junitxml` can be written by a run that crashed before any case ran, so
    # the exit code is the authority on whether pytest was happy and the matrix
    # is the authority on what it covered. Either one failing fails this script.
    covered = {item.outcome for item in (*items, *second)}
    return 1 if code or "MISSING" in covered or "FAIL" in covered else 0


if __name__ == "__main__":
    sys.exit(main())
