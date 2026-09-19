"""The small design system `docs/08` §4 asks for, and nothing more than that.

The section is specific about two things and this module is both of them. It
names twelve *semantic* tokens — background, surface, border, text, muted,
accent, success, running, waiting, blocked, failed, review — and it says to use
semantic color only, in the spirit of GitHub, Linear, `htop` and the VS Code
problems pane: a professional dense console, no cyberpunk, no decorative
hacker-green.

**A colour is chosen by what a thing means, never by where it is.** That is the
whole of what "semantic" buys: the DAG view, the execution list and the event
log all colour a RUNNING node the same way without agreeing on it separately,
and a status that gains a new meaning is a row added to `STATUS_TOKENS` rather
than a hunt through the widgets for a hex code. `tests` assert that every status
the domain can produce has a token, because the failure otherwise is a node
rendered in the default foreground — which looks like a deliberate choice.

**The symbols are the same argument in text.** Colour is unavailable to a
capture, a pipe, or a person who cannot distinguish these particular hues, so
every status also has a glyph and the two are decided together. `●` running and
`✓` passed are legible with no colour at all.
"""

from __future__ import annotations

from typing import Final

#: The twelve tokens, and their values. Dark, low-chroma, and close together in
#: lightness so that the *accent* colours carry meaning rather than the
#: background doing it: a console where every panel shouts has no way left to
#: say that something is wrong.
TOKENS: Final[dict[str, str]] = {
    "background": "#0d1117",
    "surface": "#161b22",
    "border": "#30363d",
    "text": "#e6edf3",
    "muted": "#8b949e",
    "accent": "#58a6ff",
    "success": "#3fb950",
    "running": "#d29922",
    "waiting": "#79c0ff",
    "blocked": "#db6d28",
    "failed": "#f85149",
    "review": "#a371f7",
}

#: Which token means what. Keyed by the values the domain actually produces —
#: node statuses, project statuses, review outcomes, job states, and the two
#: agent roles a person waits on — because one lookup serving every screen is
#: what keeps the screens consistent.
#:
#: `muted` is missing on purpose. Nothing in this system is muted *because of
#: its status*; muted is for the things that are not statuses at all — an
#: identifier, a timestamp, an empty panel's explanation.
STATUS_TOKENS: Final[dict[str, str]] = {
    # A project.
    "CREATED": "muted",
    "CONTRACT_DEFINED": "accent",
    "EXECUTING": "running",
    "PAUSED": "waiting",
    "COMPLETED": "success",
    "FAILED": "failed",
    "INCONCLUSIVE": "blocked",
    "CANCELLED": "muted",
    # A DAG node, or a backend job.
    "PLANNED": "muted",
    "READY": "accent",
    "RUNNING": "running",
    "WAITING_EXTERNAL": "waiting",
    "WAITING_DECISION": "review",
    "REVIEWING": "review",
    "BLOCKED": "blocked",
    "PARTIAL": "blocked",
    "PASSED": "success",
    "TIMED_OUT": "failed",
    "SUBMITTED": "accent",
    # A review.
    "PASS": "success",
    "FAIL": "failed",
    "NEEDS_MORE": "review",
}

#: The glyph beside a status. Chosen to be distinguishable in a monospace
#: capture with no colour, and to be single-width so a column of them lines up.
STATUS_SYMBOLS: Final[dict[str, str]] = {
    "CREATED": "·",
    "CONTRACT_DEFINED": "◦",
    "EXECUTING": "●",
    "PAUSED": "‖",
    "COMPLETED": "✓",
    "FAILED": "✗",
    "INCONCLUSIVE": "⊘",
    "CANCELLED": "⊘",
    "PLANNED": "·",
    "READY": "◦",
    "RUNNING": "●",
    "WAITING_EXTERNAL": "◐",
    "WAITING_DECISION": "◇",
    "REVIEWING": "◇",
    "BLOCKED": "⊘",
    "PARTIAL": "◑",
    "PASSED": "✓",
    "TIMED_OUT": "✗",
    "SUBMITTED": "◦",
    "PASS": "✓",
    "FAIL": "✗",
    "NEEDS_MORE": "◇",
}

#: What a status with no entry is drawn as. Visible rather than blank: an
#: unknown status is a fact about this program's vocabulary and not a reason to
#: render nothing, and a `?` on the screen is how somebody finds out.
UNKNOWN_SYMBOL: Final[str] = "?"


def token_for(status: str) -> str:
    """The token name for a status, defaulting to `text`.

    The default is the readable foreground rather than `muted`, because the two
    ways to be wrong are not equally bad: an unrecognised status drawn in `text`
    looks like an ordinary row and is easy to miss, while one drawn in `muted`
    looks deliberately de-emphasised and is easy to *ignore*. Neither is good,
    and the second is worse.
    """
    return STATUS_TOKENS.get(status.upper(), "text")


def symbol_for(status: str) -> str:
    """The glyph for a status, or `?` if this program does not know it."""
    return STATUS_SYMBOLS.get(status.upper(), UNKNOWN_SYMBOL)


def colour_for(status: str) -> str:
    """The hex value a status is drawn in."""
    return TOKENS[token_for(status)]


__all__ = [
    "STATUS_SYMBOLS",
    "STATUS_TOKENS",
    "TOKENS",
    "UNKNOWN_SYMBOL",
    "colour_for",
    "symbol_for",
    "token_for",
]
