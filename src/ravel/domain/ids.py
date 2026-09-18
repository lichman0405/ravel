"""Identifiers.

Primary keys are opaque and carry no meaning: a `node_id` that encoded a
node's type or status would change meaning when the node changed, and would
tempt code into reading state out of a name instead of out of the record.

Display identifiers (`RES-4F2A9C11`) exist for humans reading a TUI or a log.
They are generated once and never parsed.
"""

from __future__ import annotations

import secrets
import uuid
from enum import StrEnum


class DisplayPrefix(StrEnum):
    """The human-facing prefix for each record kind."""

    PROJECT = "P"
    ROADMAP = "RM"
    RESEARCH_TASK = "RES"
    HYPOTHESIS = "HYP"
    COMPUTATION = "COMP"
    EXPERIMENT = "EXP"
    REVIEW = "REV"
    DECISION = "DEC"
    EVIDENCE = "EVD"
    ARTIFACT = "ART"
    CONTRACT = "CTR"
    SESSION = "SES"
    APPROVAL = "APR"
    EXECUTION = "EXE"


def new_id() -> str:
    """A new opaque primary key."""
    return uuid.uuid4().hex


def display_id(prefix: DisplayPrefix) -> str:
    """A new human-facing identifier, unique enough to be read aloud."""
    return f"{prefix.value}-{secrets.token_hex(4).upper()}"


def is_opaque_id(value: str) -> bool:
    """Whether a string has the shape this module produces for primary keys."""
    if len(value) != 32:
        return False
    try:
        uuid.UUID(hex=value)
    except ValueError:
        return False
    return True
