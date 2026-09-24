"""The bench's screen: one task, the contract it runs under, and two ways to answer.

`docs/08` §5 gives a lab user three verbs — view the assigned task, upload a
result, report a deviation. This screen is those three and nothing else, and
the restraint is the design: every extra control on a bench terminal is a
control somebody will press while holding a sample.

**The contract is the instruction.** There is no second document saying what to
do, so the screen shows the frozen contract's objective, its closed list of
allowed actions and ranges, and what must come back. A task whose contract has
not been written yet says so and says not to start — which is the honest
rendering of a node Master has planned but not yet equipped, and is why
`_instruction` in the Gateway returns `None` rather than an empty contract.

**Reporting a deviation changes nothing.** The lab user observes that the plan
and the bench disagree; whether the action was in fact permitted is Master's
ruling, written later as a decision, and this screen does not offer an opinion
on it. A screen that greyed out the button for a "probably fine" action would
be making the judgement the contract exists to avoid asking for.
"""

from __future__ import annotations

import mimetypes
from pathlib import Path
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal
from textual.widgets import Input, Select, Static, TabbedContent, TabPane

from ravel.tui import format as fmt
from ravel.tui.client import GatewayClient
from ravel.tui.i18n import t
from ravel.tui.widgets import Notice, Panel, Scrolling, StatusTable

#: The columns of the task table, as message keys.
TASK_COLUMNS = ("column.status", "column.task", "column.objective")

#: The kinds of deviation a bench can report, as a closed list of
#: (label key, value).
#:
#: Closed because the Gateway's `DeviationRequest` demands a named action, and
#: a free-text field would let somebody type the action they meant in a way
#: that no later reader could group. These are the shapes the Execution
#: Contract vocabulary actually has — an action, a parameter, a substitution,
#: a stop — plus the honest fourth, "none of the above".
#:
#: The label is a key and the value is what is sent: a bench reads a sentence
#: and the record keeps a term.
DEVIATION_KINDS = (
    ("lab.deviation.action", "action"),
    ("lab.deviation.parameter", "parameter"),
    ("lab.deviation.substitution", "substitution"),
    ("lab.deviation.stop", "stop"),
    ("lab.deviation.other", "other"),
)


class LabScreen(Scrolling):
    """The bench terminal."""

    can_focus = True
    # Message keys rather than sentences: `KeyHints` translates them on the way
    # to the bottom line. See `ravel.tui.widgets.KeyHints`.
    BINDINGS: ClassVar[list[BindingType]] = [
        ("u", "upload", "binding.upload"),
        ("d", "report_deviation", "binding.report_deviation"),
        ("f5", "reload", "binding.reload"),
    ]

    def __init__(
        self, client: GatewayClient, project_id: str, *, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.client = client
        self.project_id = project_id
        self.tasks: list[dict[str, Any]] = []
        self.selected: str = ""

    def compose(self) -> ComposeResult:
        yield Notice(id="notice")
        yield Static(t("lab.heading.tasks"), id="tasks-heading")
        # Selectable, because on this screen the table is not only a report: a
        # person with two experiments picks the one they are standing at, and
        # the panel below follows the pick.
        yield StatusTable(TASK_COLUMNS, id="tasks", selectable=True)
        with TabbedContent(id="task-record"):
            with TabPane(t("lab.tab.instruction"), id="tab-instruction"):
                yield Panel("lab.panel.contract", id="instruction")
            with TabPane(t("lab.tab.prepared"), id="tab-prepared"):
                yield Panel("lab.panel.prepared", id="prepared")
            with TabPane(t("lab.tab.status"), id="tab-status"):
                yield Panel("lab.panel.status", id="status")
            with TabPane(t("lab.tab.messages"), id="tab-messages"):
                yield Panel("lab.panel.messages", id="messages")
            with TabPane(t("lab.tab.reported"), id="tab-reported"):
                yield Panel("lab.panel.reported", id="reported")
        yield Panel("lab.panel.upload", id="upload-panel")
        with Horizontal(id="upload-row"):
            yield Input(placeholder=t("lab.placeholder.path"), id="upload-path")
            # Which output the file answers, asked rather than inferred: the
            # names are the contract's, and a screen that guessed one from a
            # filename would be deciding what a person's data is. The list is
            # the handover's owed outputs and is filled in `refresh_everything`.
            yield Select([], prompt=t("lab.placeholder.output"), id="upload-output")
        yield Panel("lab.panel.deviation", id="deviation-panel")
        with Horizontal(id="deviation-row"):
            yield Select(
                [(t(label), value) for label, value in DEVIATION_KINDS],
                prompt=t("lab.placeholder.kind"),
                id="deviation-kind",
            )
        with Horizontal(id="deviation-action-row"):
            yield Input(placeholder=t("lab.placeholder.action"), id="deviation-action")
        with Horizontal(id="deviation-description-row"):
            yield Input(placeholder=t("lab.placeholder.description"), id="deviation-description")

    # ── Reading ─────────────────────────────────────────────────────────────

    async def refresh_everything(self) -> None:
        """Load the task list, and the full record of whichever task is selected.

        The list route returns each task with its contract, so the first paint
        needs one request. Only the refresh after a selection change pays for
        the second, and that is the one that also carries the backend job and
        what has been reported against it.
        """
        client = self.client
        try:
            self.tasks = await client.lab_tasks(self.project_id)
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{refused}", level="bad")
            return

        table = self.query_one("#tasks", StatusTable)
        table.fill(fmt.lab_task_row(task) for task in self.tasks)
        if not self.tasks:
            self.selected = ""
            self._offer_outputs([])
            self.panel("instruction").show([t("lab.notice.no_tasks")])
            for name in ("prepared", "status", "messages", "reported"):
                self.panel(name).show([])
            return

        identifiers = [str(task["node"]["node_id"]) for task in self.tasks]
        if self.selected not in identifiers:
            self.selected = identifiers[0]
        task = next(task for task in self.tasks if str(task["node"]["node_id"]) == self.selected)
        self._offer_outputs(self._uploadable(task))
        self.panel("instruction").show(fmt.instruction_lines(task))
        self.panel("prepared").show(fmt.preparation_lines(task))
        self.panel("status").show(self._status_lines(task))
        self.panel("messages").show(self._message_lines(task))
        self.panel("reported").show(self._reported_lines(task))

    @staticmethod
    def _uploadable(task: dict[str, Any]) -> list[tuple[str, str]]:
        """What this bench may send a file for, as `(label, output)` pairs.

        **The contract's names, in the contract's order.** `output` is a term
        the contract states, and this list is that term read back rather than a
        list of filenames the screen has decided to accept — which is why an
        upload answering a name nobody required is refused at the door and
        cannot be reached from here.

        An output that has *already* arrived is still offered. A bench sending a
        corrected log is doing the ordinary thing, and the upload door records
        the second file as a second version of the same output rather than as a
        second output. The label says which is which, because a person who has
        already sent one should be able to see that they are about to add to it.
        """
        handover = task.get("handover") or {}
        owed = set(handover.get("missing_outputs") or [])
        names = (handover.get("handover") or {}).get("required_outputs") or []
        return [
            (
                t("lab.output.owed" if name in owed else "lab.output.sent", name=name),
                str(name),
            )
            for name in names
        ]

    def _offer_outputs(self, options: list[tuple[str, str]]) -> None:
        """Put the owed outputs in the selector, keeping a choice that is still valid.

        Re-read on every refresh, because a file arriving is what changes the
        list — so the labels go stale the moment the bench uploads. A choice
        that is still among them survives, so a refresh triggered by somebody
        else's upload does not quietly re-point the field under the hand of
        whoever is standing at the terminal.
        """
        field = self.query_one("#upload-output", Select)
        chosen = field.value
        field.set_options(options)
        if isinstance(chosen, str) and any(value == chosen for _, value in options):
            field.value = chosen

    @staticmethod
    def _status_lines(task: dict[str, Any]) -> list[str]:
        """The node, the job, and the four properties of a contract that bound it.

        `contract_id` and `version` are on the line together because "which
        version of the instruction was I following" is the question that gets
        asked after an experiment, and a version number without the contract it
        belongs to is not an answer to it.
        """
        node = task.get("node", {})
        contract = task.get("instruction") or {}
        lines = [
            f"{fmt.status_line(str(node.get('status', '')))}  "
            f"{node.get('display_id', '')}  {fmt.elide(str(node.get('objective', '')), 90)}",
        ]
        if contract:
            lines.append(
                t(
                    "lab.instruction.contract",
                    contract=str(contract.get("contract_id", ""))[:12],
                    version=contract.get("version", "?"),
                )
            )
        else:
            lines.append(t("lab.instruction.no_contract"))
        job = task.get("backend_job")
        if job is None:
            lines.append(t("lab.instruction.no_job"))
        else:
            lines.append(
                t(
                    "lab.instruction.backend",
                    backend=job.get("backend", "?"),
                    attempt=job.get("attempt", "?"),
                    state=job.get("state", "?"),
                )
            )
            if job.get("backend_state"):
                lines.append(
                    t(
                        "lab.instruction.backend_says",
                        state=fmt.elide(str(job["backend_state"]), 100),
                    )
                )
            if job.get("failure_class"):
                lines.append(t("lab.instruction.failure", failure=job["failure_class"]))
        return lines

    @staticmethod
    def _message_lines(task: dict[str, Any]) -> list[str]:
        """What the Worker said to this bench, oldest first.

        Its own panel rather than three lines inside the status panel, because
        a message is addressed to the person at the terminal: an `ESCALATE` is
        the Worker asking for authority it does not have, and it is the one
        thing on this screen a lab user may need to act on — by waiting, or by
        reporting a deviation — rather than merely read past.

        `approved_by_contract` is printed when it is false, which is the case
        where the Worker said something its contract did not let it say. That
        is a fact about the project, and hiding it would make the two
        indistinguishable on the screen the bench is actually looking at.
        """
        messages = task.get("messages", [])
        if not messages:
            return [t("lab.messages.empty")]
        lines = []
        for message in messages:
            outside = "" if message.get("approved_by_contract", True) else t("lab.messages.outside")
            lines.append(
                f"{fmt.moment(message.get('sent_at'))}  {message.get('kind', '')}{outside}"
            )
            lines.append(f"    {fmt.elide(str(message.get('body', '')), 120)}")
        return lines

    @staticmethod
    def _reported_lines(task: dict[str, Any]) -> list[str]:
        """Deviations raised against this task, with whether Master has ruled.

        An open one is highlighted rather than listed the same as a resolved
        one, because "I reported this and nobody has answered" is the state a
        lab user most needs to recognise from across the room.
        """
        deviations = task.get("deviations", [])
        if not deviations:
            return [t("lab.reported.empty")]
        lines = []
        for deviation in deviations:
            open_now = deviation.get("resolved_by_decision_ref") is None
            mark = fmt.symbol_for("WAITING_DECISION" if open_now else "PASSED")
            lines.append(
                f"{mark} {deviation.get('requested_action', '')}: "
                f"{fmt.elide(str(deviation.get('description', '')), 100)}"
            )
        return lines

    # ── Acting ──────────────────────────────────────────────────────────────

    async def on_data_table_row_selected(self, event: StatusTable.RowSelected) -> None:
        """Follow the row somebody picked."""
        index = event.cursor_row
        if 0 <= index < len(self.tasks):
            self.selected = str(self.tasks[index]["node"]["node_id"])
            await self.refresh_everything()

    async def action_reload(self) -> None:
        self.notice(t("lab.notice.reading"), level="wait")
        await self.refresh_everything()
        self.notice(t("lab.notice.uptodate"), level="good")

    async def action_upload(self) -> None:
        """Send the file named in the path field, as the output named beside it.

        **Both fields are needed and the screen says which is missing**, because
        the Gateway's door asks which required output the bytes answer and
        refuses a file that answers nothing. Leaving the choice to the filename
        would make the screen the thing deciding what a person's data is; the
        contract already said, and this control is where a bench repeats it.

        The bytes are read here and posted here, in one request, because that is
        what the route takes. A path that does not exist, or that is a
        directory, is refused before the request is made — a person who mistyped
        a filename should be told so by the screen rather than by a 500 from a
        Gateway that tried to read it.

        What comes back decides what the screen says. The route reports which
        outputs have arrived and which are still owed, so the notice repeats the
        contract's own arithmetic rather than counting anything here, and a
        delivery that did not reach the run is said out loud instead of being
        left to look like a success.
        """
        field = self.query_one("#upload-path", Input)
        chosen = self.query_one("#upload-output", Select)
        if not self.selected:
            self.notice(t("lab.notice.no_task"), level="bad")
            return
        named = field.value.strip()
        if not named:
            self.notice(t("lab.notice.name_file"), level="bad")
            return
        if not isinstance(chosen.value, str) or not chosen.value:
            self.notice(t("lab.notice.choose_output"), level="bad")
            return

        path = Path(named).expanduser()
        if not path.is_file():
            self.notice(t("lab.notice.not_a_file", named=named), level="bad")
            return
        try:
            content = path.read_bytes()
        except OSError as refused:
            self.notice(t("lab.notice.unreadable", named=named, error=refused), level="bad")
            return

        output = chosen.value
        self.notice(
            t("lab.notice.sending", file=path.name, output=output, bytes=len(content)),
            level="wait",
        )
        try:
            answer = await self.client.send_output(
                self.project_id,
                self.selected,
                output,
                content,
                filename=path.name,
                media_type=_media_type(path),
            )
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(t("lab.notice.upload_refused", error=refused), level="bad")
            return

        field.value = ""
        missing = [str(name) for name in answer.get("missing_outputs") or []]
        said = t("lab.notice.sent", file=path.name, output=output)
        said += (
            t("lab.notice.still_owed", missing=", ".join(missing))
            if missing
            else t("lab.notice.everything_owed")
        )
        if not answer.get("delivered_to_run", False):
            said += t("lab.notice.run_not_told")
        self.notice(said, level="good")
        await self.refresh_everything()

    async def action_report_deviation(self) -> None:
        """Record that the plan and the bench disagree.

        All three fields are required and the Gateway enforces that, so the
        screen says which one is missing rather than letting a 422 come back
        as a wall of JSON. Nothing is decided here: the record is written with
        `permitted: false` because the *reporter* did not permit it, and what
        Master rules is a decision this screen will show once it exists.
        """
        if not self.selected:
            self.notice(t("lab.notice.no_task_selected"), level="bad")
            return
        kind = self.query_one("#deviation-kind", Select).value
        action = self.query_one("#deviation-action", Input).value.strip()
        description = self.query_one("#deviation-description", Input).value.strip()
        if not action or not description:
            self.notice(t("lab.notice.deviation_needs"), level="bad")
            return

        requested = f"{kind}: {action}" if isinstance(kind, str) else action
        self.notice(t("lab.notice.reporting"), level="wait")
        try:
            await self.client.report_deviation(
                self.project_id, self.selected, requested, description
            )
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(t("lab.notice.report_refused", error=refused), level="bad")
            return
        for identifier in ("#deviation-action", "#deviation-description"):
            self.query_one(identifier, Input).value = ""
        self.notice(t("lab.notice.reported"), level="good")
        await self.refresh_everything()

    def _selected_node(self) -> dict[str, Any]:
        for task in self.tasks:
            if str(task["node"]["node_id"]) == self.selected:
                return dict(task["node"])
        return {}

    # ── Small helpers the tests read too ────────────────────────────────────

    def panel(self, name: str) -> Panel:
        return self.query_one(f"#{name}", Panel)

    def notice(self, message: str, *, level: str = "info") -> None:
        self.query_one("#notice", Notice).say(message, level=level)


def _media_type(path: Path) -> str:
    """A guess, and a conservative one.

    `mimetypes` is consulted rather than a table written here, and anything it
    does not know becomes `application/octet-stream` — which is the correct
    answer for a bench instrument's `.dat` and is not a claim about what the
    file is.
    """
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "application/octet-stream"


__all__ = ["DEVIATION_KINDS", "TASK_COLUMNS", "LabScreen"]
