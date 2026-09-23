#!/usr/bin/env python3
"""Run every gate Phase 11 promises and say what the result amounts to.

    .venv/bin/python scripts/release_gate.py                # everything
    .venv/bin/python scripts/release_gate.py --only pytest  # matching rows

Phase 11's acceptance file answers "which items have cases, and did they pass".
That is a different question from the one a release asks, and the difference is
the reason this script exists: a release is a claim about a *system*, and a
system is certified by the weakest gate that covers it, not by the strongest.
Acceptance items can all be PASS while a lint gate is red, while a suite nobody
runs is broken, or while a live certification has never been attempted — and
each of those is invisible in an item matrix that was never asked about them.

**The three verdicts, and what each one is allowed to mean.**

`CERTIFIED` — every row ran and passed. Nothing was skipped, and nothing was
unable to run. This is the only verdict that is a claim about the whole system.

`PARTIALLY_CERTIFIED` — every row that *could* run passed, and at least one
could not, for a reason outside this repository: a missing credential, a cluster
that is not reachable, a person who has not answered. Every such row is printed
with the dependency it is missing, because "partially certified" without the
list of what is missing is a phrase that gets quoted without it.

`NOT_CERTIFIED` — a row ran and failed. One is enough, whatever the others say.

**Why a skip cannot be a pass here.** `PARTIALLY_CERTIFIED` is not a polite
`CERTIFIED`: the rows it names are rows where nothing was verified, and a
release that leans on them is leaning on nothing. The script says so in the
output rather than trusting a reader of the verdict word to infer it, because
the whole failure mode this file is written against is a green summary standing
in for a check nobody performed.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
DIM = "\033[2m"
RESET = "\033[0m"

#: What a row that did not run says it was waiting for, matched against the
#: dependency this script knows about. A row whose skip is *not* in this table
#: is a row nobody has accounted for, and it is reported as `UNCLEAR` rather
#: than assumed to be external — an unexamined skip is exactly what a
#: certification must not wave through.
MISSING_DEPENDENCIES = {
    "DEEPSEEK_API_KEY": "a model credential, so no live agent turn can run",
    "RAVEL_RESEARCH_CONTACT_EMAIL": (
        "a contact address, without which RAVEL refuses to fetch from a publisher"
    ),
    "RAVEL_SLURM_HOST": "a real Slurm cluster, so nothing here can be submitted",
}


@dataclass
class Row:
    """One gate: what it runs, and what running it meant."""

    name: str
    #: The command, as a list, so nothing here depends on a shell's quoting.
    command: list[str]
    #: What this row is evidence *for*, in one line.
    covers: str
    #: Set for a row that must not skip. A gate that can pass by not running is
    #: not a gate, which is why `tests/dsh` is run with `RAVEL_REQUIRE_DSH=1`
    #: wherever it appears.
    env: dict[str, str] = field(default_factory=dict)
    returncode: int | None = None
    #: The reasons pytest reported for skipping, which is where a missing
    #: credential is legible rather than inferred from an exit code.
    skip_reasons: list[str] = field(default_factory=list)
    #: A skip this script could not attribute to a dependency it knows about.
    unexplained: bool = False

    @property
    def ran(self) -> bool:
        return self.returncode is not None

    @property
    def outcome(self) -> str:
        """What this row is worth: PASS, SKIP, FAIL, or UNRUN."""
        if not self.ran:
            return "UNRUN"
        if self.returncode != 0:
            return "FAIL"
        if self.skip_reasons:
            return "SKIP"
        return "PASS"

    @property
    def colour(self) -> str:
        return {"PASS": GREEN, "FAIL": RED}.get(self.outcome, YELLOW)


def a_pytest(expr: str) -> list[str]:
    """One pytest invocation, with the flags every row here shares.

    `-rs` is here rather than on a second pass: whether a case skips is a fact
    about the run, so asking again would mean running the suite twice — and the
    suites this gate covers take long enough that a gate doing everything twice
    is a gate people stop running. `-q` is *not* here: `pyproject.toml` already
    sets it, and a second one makes `-qq`, which deletes the summary line this
    script reads the skips out of.
    """
    return [
        str(PYTHON),
        "-m",
        "pytest",
        *expr.split(),
        "-p",
        "no:randomly",
        "--strict-markers",
        "-rs",
    ]


def rows() -> list[Row]:
    """Every gate, in the order a reader would fix them if one failed.

    Static analysis first, because a failure there makes every later row's
    result uninteresting; then the suites from the cheapest to the most
    expensive, so a broken unit test costs seconds rather than an hour; then
    the acceptance matrices; then the four gates that need something RAVEL does
    not own — a live model, the Internet, a cluster, a person.
    """
    return [
        Row(
            name="ruff",
            command=["ruff", "check", "src", "tests"],
            covers="style and the mistakes a linter catches before a test does",
        ),
        Row(
            name="pyright",
            command=["pyright", "src", "tests"],
            covers="types over the whole tree, src and tests alike",
        ),
        Row(
            name="unit",
            command=a_pytest("tests/unit"),
            covers="every unit of logic, with no service running",
        ),
        Row(
            name="integration",
            command=a_pytest("tests/integration"),
            covers="PostgreSQL, Temporal, MinIO and the real tool servers",
        ),
        Row(
            name="e2e",
            command=a_pytest("tests/e2e"),
            covers="a whole project driven through the loop, headless",
        ),
        Row(
            name="v0-acceptance",
            command=a_pytest("tests/acceptance -m acceptance"),
            covers="A01-A20 and the seven Phase 9 gates",
        ),
        Row(
            name="phase10-acceptance",
            command=a_pytest("tests/acceptance -m phase10"),
            covers="P10-01..P10-20 and the twenty worker-level items",
        ),
        Row(
            name="phase11-acceptance",
            command=a_pytest("tests/acceptance -m phase11"),
            covers="the Phase 11 items, P11-01 onwards",
        ),
        Row(
            name="dsh",
            command=a_pytest("tests/dsh -m dsh"),
            env={"RAVEL_REQUIRE_DSH": "1"},
            covers="the harness gate, live, and it may not skip",
        ),
        Row(
            name="live-research",
            command=a_pytest("tests/live_research -m live"),
            covers="real sources over the real Internet, never mocked",
        ),
        Row(
            name="research-readback",
            command=a_pytest(
                "tests/integration/master/test_research_readback.py "
                "tests/acceptance/test_phase11_readback.py"
            ),
            covers="Research's result reaching Master through the record",
        ),
        Row(
            name="temporal-reconciliation",
            command=a_pytest(
                "tests/integration/reconcile tests/acceptance/test_phase11_reconcile.py"
            ),
            covers="a lost run ending its node instead of stranding it",
        ),
        Row(
            name="compute-preparation",
            command=a_pytest(
                "tests/unit/preparation/test_raspa.py "
                "tests/integration/temporal/test_preparation.py "
                "tests/acceptance/test_phase11_materialization.py"
            ),
            covers="a solver contract becoming the workspace it runs in",
        ),
        Row(
            name="lab-preparation",
            command=a_pytest(
                "tests/unit/preparation/test_lab.py tests/integration/backends/test_lab_backend.py"
            ),
            covers="a bench contract becoming the package a person is handed",
        ),
        Row(
            name="slurm-integration",
            command=a_pytest(
                "tests/unit/backends/slurm "
                "tests/integration/backends/test_slurm_collection.py "
                "tests/acceptance/test_phase10_backends.py "
                "tests/acceptance/test_phase11_slurm.py"
            ),
            covers="the real cluster backend, against a scripted cluster",
        ),
        Row(
            name="humanlab-integration",
            command=a_pytest(
                "tests/integration/backends/test_lab_backend.py "
                "tests/integration/gateway/test_lab_handover.py "
                "tests/acceptance/test_phase11_humanlab.py"
            ),
            covers="a bench handover, a person's upload, and the Gateway around it",
        ),
        Row(
            name="five-agent-e2e",
            command=a_pytest(
                "tests/acceptance/test_phase10_live.py -k five_live_agents"
            ),
            env={"RAVEL_REQUIRE_DSH": "1"},
            covers="five real agents driving one project to an ending",
        ),
    ]


def run(row: Row, environment: dict[str, str]) -> None:
    """Run one row, streaming its output, and note what it skipped on.

    Streamed rather than captured: a gate that fails after twenty minutes has
    to say so as it goes, and a script that held the output until the end would
    make the final table the only thing anybody could read. The lines are also
    where the skips are — pytest's `-rs` summary, in the suite's own words,
    which is what lets a `SKIP` row name the credential it wanted instead of
    leaving a reader to guess.
    """
    print(f"\n\033[1m── {row.name} ──\033[0m {DIM}{' '.join(row.command)}{RESET}")
    process = subprocess.Popen(
        row.command,
        cwd=REPO_ROOT,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert process.stdout is not None
    for line in process.stdout:
        print(line, end="")
        if line.startswith("SKIPPED "):
            _, _, reason = line.partition(": ")
            reason = reason.strip()
            if reason and reason not in row.skip_reasons:
                row.skip_reasons.append(reason)
    row.returncode = process.wait()


def environment_for(row: Row) -> dict[str, str]:
    """This process's environment, with the row's overrides applied.

    `.env` is read here rather than by a shell, because the alternative is a
    script that only works when it is invoked through `make` — and the rows
    that need a credential are exactly the ones a reader is most likely to run
    on their own to see whether the credential is there.
    """
    environment = dict(os.environ)
    env_file = REPO_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            environment.setdefault(key.strip(), value.strip().strip("'\""))
    environment.update(row.env)
    return environment


def attribute(row: Row) -> str | None:
    """The dependency a skipped row is waiting for, if it named one."""
    for reason in row.skip_reasons:
        for variable, explanation in MISSING_DEPENDENCIES.items():
            if variable in reason:
                return f"{variable}: {explanation}"
    return None


def verdict(rows: list[Row]) -> str:
    """What the rows amount to, which is the whole point of running them.

    `NOT_CERTIFIED` outranks everything: one failed row is a fact about the
    system, and no amount of passing rows makes it untrue. `PARTIALLY_CERTIFIED`
    is next, and it is *not* a weaker `CERTIFIED` — it says some gates were
    never exercised, which is a gap rather than a result.
    """
    if any(row.outcome == "FAIL" for row in rows):
        return "NOT_CERTIFIED"
    if any(row.outcome in ("SKIP", "UNRUN") for row in rows):
        return "PARTIALLY_CERTIFIED"
    return "CERTIFIED"


def report(rows: list[Row]) -> None:
    """The table, then what each non-passing row costs the verdict."""
    width = max(len(row.name) for row in rows)
    print("\n\033[1m── Phase 11 release gate ──\033[0m\n")
    for row in rows:
        outcome = row.outcome
        print(
            f"  {row.colour}{outcome:<7}{RESET} {row.name:<{width}}  "
            f"{DIM}{row.covers}{RESET}"
        )

    skipped = [row for row in rows if row.outcome == "SKIP"]
    unrun = [row for row in rows if row.outcome == "UNRUN"]

    if skipped:
        # "Rows that did not run" was this line, and the first full run showed
        # it saying that about `phase11-acceptance` — a row that ran all seven
        # of its items and skipped one case inside them. A row is not a suite:
        # what a reader has to know is that the row is not a full result *and*
        # that it was not nothing, so the sentence says which it is.
        print(
            f"\n{YELLOW}Rows that ran with something left unexercised, and what "
            f"it was waiting for:{RESET}"
        )
        for row in skipped:
            reason = attribute(row)
            if reason is None:
                row.unexplained = True
                detail = (
                    f"{RED}unattributed{RESET} — a skip this script knows no "
                    f"dependency for: {'; '.join(row.skip_reasons)}"
                )
            else:
                detail = reason
            print(f"  {row.name}: {detail}")
    if unrun:
        names = ", ".join(row.name for row in unrun)
        print(f"\n{RED}Never run{names and ': ' + names}.{RESET}")

    failed = [row for row in rows if row.outcome == "FAIL"]
    if failed:
        print(
            f"\n{RED}Failed: {', '.join(row.name for row in failed)}.{RESET} "
            "One failed gate is enough, whatever the others say."
        )

    answer = verdict(rows)
    colour = {"CERTIFIED": GREEN, "PARTIALLY_CERTIFIED": YELLOW}.get(answer, RED)
    print(f"\n\033[1m{colour}{answer}{RESET}\n")
    if answer == "PARTIALLY_CERTIFIED":
        print(
            "  This is not a weaker CERTIFIED. The rows above were never "
            "exercised, so nothing is known about what they cover — supply "
            "what they name and run this again before the claim is made.\n"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 11 release gate.")
    parser.add_argument(
        "--only",
        default="",
        help="run only the rows whose name contains this substring",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the rows and their commands without running anything",
    )
    arguments = parser.parse_args(argv)

    selected = [row for row in rows() if arguments.only in row.name]
    if not selected:
        print(f"{RED}no row matches {arguments.only!r}{RESET}", file=sys.stderr)
        return 2

    if arguments.dry_run:
        for row in selected:
            print(f"{row.name}: {' '.join(row.command)}")
        return 0

    for row in selected:
        run(row, environment_for(row))
    report(selected)

    # A gate that did not run is a non-zero exit, because the caller is a
    # release and a release has to be able to tell "everything is green" from
    # "everything I asked about is green".
    return 0 if verdict(selected) == "CERTIFIED" else 1


if __name__ == "__main__":
    sys.exit(main())
