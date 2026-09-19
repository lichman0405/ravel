"""The Gateway's routes, one module per surface.

The split is by who is asking and what they may see, not by URL prefix, because
that is the split the security model is written in: `auth` is for a caller who
has not been identified yet, `projects` is the read surface every member
shares, `control` is the owner's, `conversation` is the owner talking to
Master, `events` is the live view every member follows, `lab` is the lab
user's, and `admin` is the administrator's. A route that does not obviously
belong to one of those is a route whose authority has not been decided.
"""

from __future__ import annotations

__all__: list[str] = []
