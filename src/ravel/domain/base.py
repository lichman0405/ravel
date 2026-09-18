"""The shape every domain record shares.

Records are frozen and reject unknown fields. Both properties are load-bearing:

- **Frozen** because a record that can be edited in place cannot be evidence of
  what was decided at a point in time, and because a mutable record invites two
  ways to change the same thing — an assignment here, a transition method
  there — which is how a state machine stops being the only path.
  Entities with a life cycle are frozen too: `Project.transition` and
  `DagNode.transition` return a new value, and the repository writes it.
- **`extra="forbid"`** because a record that silently swallows an unexpected
  field is how a typo in a tool call becomes a missing authorization check.

Values are stripped of surrounding whitespace so that a title of `" "` is
rejected as empty rather than stored as a space.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class Record(BaseModel):
    """Base for every authoritative RAVEL record."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_assignment=False,
        str_strip_whitespace=True,
        use_enum_values=False,
    )
