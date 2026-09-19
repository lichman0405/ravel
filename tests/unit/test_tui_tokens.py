"""`tokens.py` promises that every status the domain produces has a token.

That promise is in its module docstring and nothing checked it, which is the
shape of claim that survives longest without being true. What it costs when it
breaks is specific and quiet: `token_for` falls back to the ordinary foreground,
so a node in a status this program has never heard of is drawn exactly like a
node in a status it deliberately renders as unremarkable. Nothing crashes, no
test turns red, and the one row somebody needed to notice is the one row that
looks like every other.

The vocabularies are read from the domain rather than listed here, so a status
added to `NodeStatus` next year is covered without anybody remembering this file
exists — which is the only reason the assertion is worth having.
"""

from __future__ import annotations

from enum import StrEnum

import pytest

from ravel.domain.enums import JobState, NodeStatus, ProjectStatus, ReviewOutcome
from ravel.tui.tokens import (
    STATUS_SYMBOLS,
    STATUS_TOKENS,
    TOKENS,
    UNKNOWN_SYMBOL,
    colour_for,
    symbol_for,
    token_for,
)

#: The four status vocabularies a screen can be asked to draw.
VOCABULARIES: tuple[type[StrEnum], ...] = (
    NodeStatus,
    ProjectStatus,
    ReviewOutcome,
    JobState,
)

#: Words the screens pass by hand, and the reason the check above is not enough.
#: The lab's reported panel draws a deviation as WAITING_DECISION or as PASSED
#: depending on whether Master has ruled, and the review panel draws a criterion
#: as PASSED or FAILED from a `satisfied` boolean. Neither is a `NodeStatus`; the
#: first is a node status borrowed for a different record and the second is a
#: review outcome written as a word. A check that read only the enums would miss
#: both, and both are on a screen.
LITERALS = ("WAITING_DECISION", "PASSED", "FAILED")


@pytest.mark.parametrize("vocabulary", VOCABULARIES, ids=lambda v: v.__name__)
def test_every_status_has_a_colour_and_a_glyph(vocabulary: type[StrEnum]) -> None:
    """Both tables, because the second is the one that survives `| cat`."""
    named = vocabulary.__name__
    missing_colour = [m.value for m in vocabulary if m.value not in STATUS_TOKENS]
    missing_glyph = [m.value for m in vocabulary if m.value not in STATUS_SYMBOLS]
    assert not missing_colour, f"{named} has statuses with no colour: {missing_colour}"
    assert not missing_glyph, f"{named} has statuses with no glyph: {missing_glyph}"


@pytest.mark.parametrize("literal", LITERALS)
def test_the_statuses_the_screens_pass_by_hand_are_known(literal: str) -> None:
    """A screen that passes a word no table knows draws it in the default colour.

    `symbol_for` at least gives that row a `?`, which is visible. The colour
    does not, and a panel of review criteria that rendered satisfied and
    unsatisfied ones identically is a panel that says nothing.
    """
    assert literal in STATUS_TOKENS
    assert literal in STATUS_SYMBOLS


def test_the_two_tables_describe_the_same_statuses() -> None:
    """A status with a colour and no glyph, or the reverse, is one table's bug.

    The docstring says the two are decided together, and this is what "together"
    means when written down. It is a symmetric-difference check rather than two
    subset checks so that the failure message names the status either way round.
    """
    assert set(STATUS_TOKENS) == set(STATUS_SYMBOLS)


def test_every_colour_names_one_of_the_twelve_tokens() -> None:
    """`docs/08` §4: semantic colour only, and exactly twelve of them.

    A hex code reaching `STATUS_TOKENS` would be a colour chosen by nothing in
    particular, in the one table every screen reads — and `colour_for` indexes
    `TOKENS` with whatever it finds, so a misspelled token name is a `KeyError`
    from inside a `DataTable` cell rather than a visible mistake.
    """
    assert len(TOKENS) == 12
    unknown = sorted({token for token in STATUS_TOKENS.values() if token not in TOKENS})
    assert not unknown, f"STATUS_TOKENS names tokens that do not exist: {unknown}"
    for token in TOKENS.values():
        assert token.startswith("#") and len(token) == 7


def test_an_unknown_status_is_visible_rather_than_blank() -> None:
    """The behaviour `UNKNOWN_SYMBOL` exists for, asserted once.

    It is deliberately not a crash and deliberately not `muted`: `?` in the
    ordinary foreground is a row somebody notices, and the alternatives are a
    traceback or a row that looks like it was meant to be ignored.
    """
    assert symbol_for("NOT_A_STATUS") == UNKNOWN_SYMBOL
    assert token_for("NOT_A_STATUS") == "text"
    assert colour_for("NOT_A_STATUS") == TOKENS["text"]


def test_lookup_is_case_insensitive() -> None:
    """Because the two callers disagree about case and neither is wrong.

    A `DagNode.status` is an enum and arrives uppercase; a row borrowed from a
    written-down word may not be. Uppercasing in the lookup is what keeps that
    from being two tables.
    """
    assert symbol_for("running") == symbol_for("RUNNING")
    assert colour_for("paused") == colour_for("PAUSED")
    assert token_for("Reviewing") == token_for("REVIEWING")
