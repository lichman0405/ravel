"""Timestamps.

Every timestamp RAVEL writes is timezone-aware UTC. A naive timestamp in an
authoritative record cannot be ordered against one written by another process
in another zone, and "retrieved_at" in the Evidence Ledger is a claim about
when a source was actually read.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utcnow() -> datetime:
    """The current time, as an aware UTC datetime."""
    return datetime.now(UTC)


def ensure_utc(moment: datetime) -> datetime:
    """Reject a naive timestamp rather than guessing its zone.

    Raises:
        ValueError: The timestamp carries no timezone.
    """
    if moment.tzinfo is None:
        raise ValueError("timestamp is naive; RAVEL records are timezone-aware UTC")
    return moment.astimezone(UTC)


def json_iso(moment: datetime) -> str:
    """ISO 8601 UTC timestamp with a `Z` suffix.

    This matches Pydantic's `model_dump(mode="json")` for timezone-aware UTC
    datetimes, so a timestamp serialized by hand and one serialized through a
    model compare equal as strings.
    """
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")
