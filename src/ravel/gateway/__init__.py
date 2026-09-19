"""The HTTP surface the local TUI talks to, and nothing else talks to.

The Gateway exists because the TUI must not be a source of truth and must not
reach PostgreSQL directly. Every read it serves is assembled from the
authoritative state by the same repositories the agents use, and every write it
serves is one of the four a *user* is allowed to make: pause or resume a
project, answer an approval, set an authority envelope, and talk to Master.

There is deliberately no route that changes the Scientific DAG. Not "no route a
non-Master may call" — none at all. A user who wants the plan changed says so
to Master, and Master decides; a route that wrote a node would be the User
editing the DAG with extra steps, which the separation of powers forbids. The
absence is asserted by `tests/e2e/test_tui.py` rather than left to review.

No DSH endpoint is exposed here either. The harness serves one `(project, role)`
session inside RAVEL's own process; a client of this Gateway can ask Master a
question through the Master conversation route, and cannot address a runtime,
a session, or a model directly.
"""

from __future__ import annotations

__all__: list[str] = []
