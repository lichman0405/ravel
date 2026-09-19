"""The local console: the user's entry point, and never a source of truth.

`docs/08` §2 is a list of what the TUI does not do — it runs no scientific
logic, calls DSH never, mutates the DAG never, holds no authoritative state,
decides no review, and stores no research truth — and the package is arranged
so that each of those is enforced by something rather than promised:

* **No DSH, no database.** Nothing in this package imports
  `ravel.harness.*` or `ravel.state.*`. Everything it knows arrives over
  `ravel.tui.client`, which speaks HTTP and WebSocket to the Gateway and has
  no other way to learn anything.
* **No DAG mutation, because there is no route for one.** `GatewayClient` has
  a `dag()` that reads and no method that writes; the Gateway itself has
  nothing to call. `tests/e2e/test_tui.py` asserts that by probing every route
  with an owner's token and reading the DAG back.
* **No state worth losing.** `Credentials.last_event_seq` is the only thing
  kept across a reconnect and it is a fact about what this person has *seen*,
  not about the project. Kill the process and nothing is lost but the scroll
  position.

The modules, in the order a request travels through them: `client` turns HTTP
into Python, `format` turns Python into lines, `widgets` draws lines, `screens`
decides which lines, and `app` decides which screen.
"""

from __future__ import annotations

__all__: list[str] = []
