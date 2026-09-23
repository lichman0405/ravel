"""P11-07: the release gate, and what its verdict is allowed to mean.

Phase 11's acceptance matrix answers "does every item have a case, and did the
cases pass". A release asks a different question — is the *system* certifiable —
and the difference is where this item's whole risk lives. A matrix can be all
green while a suite nobody runs is broken, while lint is red, or while a live
certification was never attempted; and the failure that matters most is the
quiet one, where a gate that could not run is counted as a gate that passed.
`scripts/release_gate.py` is the answer, and these cases are about the three
things it must not do.

**It must not report `CERTIFIED` for a row that did not run.** A missing
credential, an unreachable cluster and a person who has not answered are all
facts about the world rather than about RAVEL, and the honest verdict for them
is `PARTIALLY_CERTIFIED` — with the dependency named, because the phrase without
its list gets quoted as though it were a pass.

**It must not fail the release for one.** A gate that treats a missing
credential as a defect is a gate that gets disabled on the first machine that
does not have one, and after that nothing is certified at all.

**It must not wave through a skip it cannot explain.** A skip nobody attributed
is a row where nothing is known and nobody has said why, which is the one case
the certification must refuse to absorb.

The last case is about the gate's *contents* rather than its arithmetic: every
area Phase 11 promises has to be a row, because a gate that quietly stopped
covering the Slurm backend would certify exactly as loudly as one that covers
it. That is the same failure the acceptance matrix exists to catch one level up.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scripts.release_gate import MISSING_DEPENDENCIES, Row, rows, verdict

pytestmark = [pytest.mark.phase11, pytest.mark.acceptance]


def a_row(name: str, *, returncode: int | None, skips: tuple[str, ...] = ()) -> Row:
    """One row in the state a run could leave it in."""
    row = Row(name=name, command=["true"], covers="nothing; a row for a test")
    row.returncode = returncode
    row.skip_reasons = list(skips)
    return row


def test_p11_07_a_row_that_did_not_run_is_not_a_row_that_passed() -> None:
    """The verdict the item exists for: a gap is not a result.

    `PARTIALLY_CERTIFIED` is asserted as its own answer rather than as a shade
    of `CERTIFIED`, and the exit code is part of the claim — a release that
    could not tell the two apart would be a release that stands on the rows it
    could not run.
    """
    assert verdict([a_row("unit", returncode=0)]) == "CERTIFIED"
    assert (
        verdict(
            [
                a_row("unit", returncode=0),
                a_row("dsh", returncode=0, skips=("DEEPSEEK_API_KEY is unset",)),
            ]
        )
        == "PARTIALLY_CERTIFIED"
    )
    # A row that never ran at all — the script was asked for a subset, or a
    # previous row took the process down — is a gap for the same reason.
    assert verdict([a_row("unit", returncode=0), a_row("e2e", returncode=None)]) == (
        "PARTIALLY_CERTIFIED"
    )


def test_p11_07_one_failed_row_is_not_certified_whatever_the_others_say() -> None:
    """And a failure outranks a gap, because it is a fact about the system.

    The ordering matters: a run that both skipped a live row and failed a unit
    test is not "partially" anything. Reporting `PARTIALLY_CERTIFIED` there
    would describe a system with a known defect as one with an unknown gap.
    """
    assert (
        verdict(
            [
                a_row("unit", returncode=1),
                a_row("e2e", returncode=0),
                a_row("dsh", returncode=0, skips=("DEEPSEEK_API_KEY is unset",)),
            ]
        )
        == "NOT_CERTIFIED"
    )


def test_p11_07_a_skip_nobody_can_attribute_is_reported_as_one() -> None:
    """Every skip a row reports is matched against a dependency this repo knows.

    The matching is the gate's, and what it must not do is assume: a skip with
    no entry in the table is a row nothing is known about, so it is printed as
    unattributed rather than filed under "external". The case asserts both
    directions — a credential is recognised, and a sentence naming nothing is
    not — because an attribution rule that matched everything would pass a
    check on the first half alone.
    """
    known = next(iter(MISSING_DEPENDENCIES))
    assert any(known in reason for reason in [f"{known} is unset, so nothing ran"])
    unrelated = "the cluster is not reachable from this machine"
    assert not any(known in unrelated for known in MISSING_DEPENDENCIES), (
        "this case is only meaningful while the table does not match everything"
    )


def test_p11_07_every_area_phase_11_promises_is_a_row() -> None:
    """The gate covers the system, so its row list is part of the claim.

    Named rather than counted: a count would pass if a row were swapped for a
    different one, and what a reader needs to know is that the Slurm backend,
    the bench channel, the reconciliation sweep and the five-agent run are in
    the run rather than that there are seventeen of something.
    """
    names = {row.name for row in rows()}
    required = {
        "ruff",
        "pyright",
        "unit",
        "integration",
        "e2e",
        "v0-acceptance",
        "phase10-acceptance",
        "phase11-acceptance",
        "dsh",
        "live-research",
        "research-readback",
        "temporal-reconciliation",
        "compute-preparation",
        "lab-preparation",
        "slurm-integration",
        "humanlab-integration",
        "five-agent-e2e",
    }
    assert required <= names, (
        f"the release gate no longer runs {sorted(required - names)}; a gate "
        "that stopped covering an area would certify exactly as loudly"
    )

    # And each row runs something. A row with an empty command would report a
    # passing exit code it never earned.
    for row in rows():
        assert row.command, f"{row.name} runs nothing"
        assert row.covers, f"{row.name} does not say what it is evidence for"


def test_p11_07_a_row_that_names_a_file_that_does_not_exist_is_caught() -> None:
    """The names above are not enough on their own, and this is why.

    A row is a name, a sentence and an argument list, and the argument list is
    the one part nothing checked: `compute-preparation` named
    `tests/acceptance/test_phase11_preparation.py` for a whole item while the
    module was called something else, and the row would have exited 4 —
    "file or directory not found" — on a run where the work it was evidence for
    had passed. The verdict would have been `NOT_CERTIFIED` for a reason no
    reader could act on, which is the failure this file's docstring calls the
    quiet one, one level down.

    Only `tests/...` tokens are checked, and they are checked in both
    directions: the walk has to find something, or a renamed directory would
    turn this into a test of nothing.
    """
    named = [
        token
        for row in rows()
        for token in row.command
        if token.startswith(("tests/", "scripts/"))
    ]
    assert named, "no row names a path, so this case is checking nothing"
    missing = sorted({token for token in named if not Path(token).exists()})
    assert not missing, (
        f"the release gate names {missing}, which do not exist — pytest exits 4 "
        "on those, so the row fails for a reason that has nothing to do with "
        "what it is evidence for"
    )
