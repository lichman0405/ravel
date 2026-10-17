"""The administrator's screen: is the machine well, and where are the logs.

`docs/08` §5 gives an admin one verb — inspect runtime health — and A18
repeats it as "Admin can inspect runtime health". This screen is exactly that,
and it is built so that "inspect" cannot quietly become "operate": **every
route it reads is a `GET`.** `tests/unit/test_gateway_runtime.py` asserts that
of the admin routes themselves, and `tests/e2e/test_tui.py` asserts it of this
screen, so an administrator credential that could stop a project or move a node
would fail a test rather than pass a review.

What health means here is seven separate questions, and they are kept separate
because they fail separately:

* **The processes.** Which long-running services are still reporting. This is
  the one panel that reads a process's account of itself, because there is no
  other source: a supervisor that died leaves a project that has stopped
  moving, which is exactly what a project with nothing left to do looks like.
  The Gateway does the judging — how long the silence has been, against the
  cadence the service promised — so what the screen draws is a fact and not a
  service's own opinion of itself.
* **The harness.** Which DSH is pinned, whether this deployment carries patches
  the pin does not, and whether a runtime pool has actually been started in
  this process. `pool_started: false` is not an error — a Gateway that has
  served only reads has never needed a harness — so it renders as a sentence
  and not as three zeroes that read like a fault.
* **Temporal.** Whether the queue answers. A durable-execution service that is
  down does not lose work that is already recorded, and the panel says so
  rather than implying the project is in danger.
* **The backends.** What the work has actually been handed to, and how the
  cluster is configured. The route reports *whether* a credential is set by
  naming the setting and never by reading the value, and it does not test
  reachability — a Gateway that opened an SSH session would be a Gateway
  holding the cluster password. The panel prints that sentence rather than
  paraphrasing it, because it is the one thing here a reader might otherwise
  assume had been checked.
* **The jobs**, open first, each naming the node and the failure class.
* **The recoveries.** Runs RAVEL found dead. A project with none is the healthy
  answer and is printed as a sentence, because that is what somebody came to
  the panel for.
* **The project's own counters**, plus the paths of the logs, because the last
  step of any diagnosis is reading them and a screen that made somebody go and
  find the path has stopped one step short.

**What is deliberately absent.** No pause, no resume, no approval, no envelope,
no DAG. An administrator's authority is over the runtime, and "Admin does not
make scientific decisions" is enforced by there being no method on this class
that writes anything — a claim the e2e suite checks against the routes rather
than against this docstring.
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
        yield Panel("Processes", id="services")
        yield Panel("Harness", id="harness")
        yield Panel("Temporal", id="temporal")
        yield Panel("Backends", id="backends")
        yield Panel("Jobs", id="jobs")
        yield Panel("Recovered runs", id="reconciliation")
        yield Panel("This project", id="runtime")
        yield Panel("Where the logs are", id="logs")

    async def refresh_everything(self) -> None:
        """Read the health routes and fill the panels.

        Seven requests rather than one combined health endpoint, because the
        answers have different latencies and different failure modes: the
        Temporal probe opens a connection to another service, the service read
        is a row, and the rest are database reads. Combining them would make
        the fast ones wait on the slow one, and one service being down would
        blank the whole screen instead of one panel.

        All seven are awaited in sequence rather than gathered, and that is a
        deliberate trade: `asyncio.gather` would make the first paint faster
        and would also mean a screen that half-filled itself, with no way to
        say which panels had answered. The slowest route sets the pace, and
        every one of them is an indexed read.
        """
        client = self.client
        try:
            services = await client.service_health(self.project_id)
            harness = await client.harness_health(self.project_id)
            runtime = await client.runtime(self.project_id)
            temporal = await client.temporal_health(self.project_id)
            backends = await client.backend_health(self.project_id)
            jobs = await client.jobs(self.project_id)
            recovered = await client.reconciliations(self.project_id)
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{refused}", level="bad")
            return

        self.runtime = runtime
        self.panel("services").show(fmt.service_lines(services))
        self.panel("harness").show(fmt.harness_lines(harness, runtime))
        self.panel("temporal").show(fmt.temporal_lines(temporal))
        self.panel("backends").show(fmt.backend_lines(backends))
        self.panel("jobs").show(fmt.job_lines(jobs))
        self.panel("reconciliation").show(fmt.reconciliation_lines(recovered))
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
