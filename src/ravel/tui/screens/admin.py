"""The administrator's screen: is the machine well, and where are the logs.

`docs/08` §5 gives an admin one verb — inspect runtime health — and A18
repeats it as "Admin can inspect runtime health". This screen is exactly that,
and it is built so that "inspect" cannot quietly become "operate": **every
route it reads is a `GET`.** `tests/unit/test_gateway_runtime.py` asserts that
of the admin routes themselves, so an administrator credential that could stop
a project or move a node would fail a test rather than pass a review.

What health means here is three separate questions, and they are kept separate
because they fail separately:

* **The harness.** Which DSH is pinned, whether this deployment carries patches
  the pin does not, and whether a runtime pool has actually been started in
  this process. `pool_started: false` is not an error — a Gateway that has
  served only reads has never needed a harness — so it renders as a sentence
  and not as three zeroes that read like a fault.
* **Temporal.** Whether the queue answers. A durable-execution service that is
  down does not lose work that is already recorded, and the panel says so
  rather than implying the project is in danger.
* **The project's own counters**, plus the paths of the logs, because the last
  step of any diagnosis is reading them and a screen that made somebody go and
  find the path has stopped one step short.
"""

from __future__ import annotations

from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType

from ravel.tui import format as fmt
from ravel.tui.client import GatewayClient
from ravel.tui.widgets import Notice, Panel, Scrolling


class AdminScreen(Scrolling):
    """Runtime health, read-only, by construction and by test."""

    can_focus = True
    BINDINGS: ClassVar[list[BindingType]] = [
        ("f5", "reload", "Refresh"),
    ]

    def __init__(
        self, client: GatewayClient, project_id: str, *, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.client = client
        self.project_id = project_id
        self.runtime: dict[str, Any] = {}

    def compose(self) -> ComposeResult:
        yield Notice(id="notice")
        yield Panel("Harness", id="harness")
        yield Panel("Temporal", id="temporal")
        yield Panel("This project", id="runtime")
        yield Panel("Where the logs are", id="logs")

    async def refresh_everything(self) -> None:
        """Read the three health routes and fill four panels.

        Three requests rather than one combined health endpoint, because the
        three answers have different latencies and different failure modes: the
        Temporal probe opens a connection to another service and the harness
        and runtime reads are database reads. Combining them would make the
        fast two wait on the slow one, and one service being down would blank
        the whole screen instead of one panel.
        """
        client = self.client
        try:
            harness = await client.harness_health(self.project_id)
            runtime = await client.runtime(self.project_id)
            temporal = await client.temporal_health(self.project_id)
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{refused}", level="bad")
            return

        self.runtime = runtime
        self.panel("harness").show(fmt.harness_lines(harness, runtime))
        self.panel("temporal").show(fmt.temporal_lines(temporal))
        self.panel("runtime").show(fmt.runtime_lines(runtime))
        self.panel("logs").show(
            [
                f"{name.replace('_', ' ')}: {path}"
                for name, path in (runtime.get("logs") or {}).items()
            ]
            or ["This deployment has not been told where its logs are."]
        )
        self.notice("Runtime health read.", level="good")

    async def action_reload(self) -> None:
        await self.refresh_everything()

    # ── Small helpers the tests read too ────────────────────────────────────

    def panel(self, name: str) -> Panel:
        return self.query_one(f"#{name}", Panel)

    def notice(self, message: str, *, level: str = "info") -> None:
        self.query_one("#notice", Notice).say(message, level=level)


__all__ = ["AdminScreen"]
