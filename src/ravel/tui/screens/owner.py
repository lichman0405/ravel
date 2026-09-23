"""The owner's screen, laid out in the order `docs/08` §5 gives it.

The section is a priority list, not a feature list — Master's current focus,
then what needs attention, then execution state, then the DAG, then the record
— and it closes with a sentence that decides the layout: *Master must feel
present.* So Master's panel is at the top, it is the widest thing on the
screen, and it shows what Master last said rather than a status code.

**Nothing here decides anything.** Every panel is filled from a route, the
composer sends a message, and the bindings send one command each — two control
the project, three control who is in it. Where a decision is needed — should
this node be replaced, is this project finished — the screen says what the state
is and who decides it, and does not offer a button. That is §2's
"display/input/control only" made concrete: the TUI can *ask* Master, and cannot
*be* Master.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, ClassVar, cast

from textual.app import ComposeResult
from textual.binding import BindingType
from textual.containers import Horizontal
from textual.widgets import Input, Select, TabbedContent, TabPane

from ravel.tui import format as fmt
from ravel.tui.client import GatewayClient
from ravel.tui.widgets import Notice, Panel, Scrolling, StatusTable

if TYPE_CHECKING:  # the app imports this module, so the name is only a type
    from ravel.tui.app import RavelTUI

#: The columns of the DAG table. Named here because the screen and the test
#: that reads a row both need to agree about what a row is.
DAG_COLUMNS = ("status", "node", "objective")

#: The columns of the member table. Same reason.
MEMBER_COLUMNS = ("role", "username", "added by")

#: The roles an owner may confer, as a closed list.
#:
#: Closed because the Gateway validates the role against the domain enum and a
#: free-text field would make a typo a 422 with a wall of JSON on the notice
#: line. `LAB_USER` leads because it is what an owner adds most often, and
#: `ADMIN` is present because an owner may confer it — the domain's ranking
#: exists so that authority cannot be *escalated*, and granting an operational
#: role the granter never held is not that.
MEMBER_ROLES = (
    ("Lab user — works the bench", "LAB_USER"),
    ("Project owner — may direct this project", "PROJECT_OWNER"),
    ("Administrator — runtime only, no science", "ADMIN"),
)

#: Which tab the membership controls belong to.
#:
#: The bindings below fire wherever the screen has focus, and a key that
#: withdraws somebody's role must not be one keystroke away while the reader is
#: looking at the DAG. So the three membership actions check this before they do
#: anything, which turns "pressed the wrong key on the wrong tab" into a
#: sentence rather than a withdrawal.
MEMBERSHIP_TAB = "tab-membership"

#: How long a burst of events is allowed to gather before the screen redraws.
#:
#: Not a delay for its own sake: committing a stage emits an event per node, and
#: redrawing per event would issue seven requests per node to show states that
#: only the last of them is about. A quarter of a second is below the threshold
#: at which a person notices a lag and above the length of any burst this
#: system produces.
SETTLE_SECONDS = 0.25

#: How long to wait before reconnecting a stream that dropped. Short, because
#: the usual reason is a Gateway restart and the usual wish is to be watching
#: again by the time it finishes coming up.
RECONNECT_SECONDS = 2.0


class OwnerScreen(Scrolling):
    """Everything about one project, read-only except for five commands."""

    can_focus = True
    BINDINGS: ClassVar[list[BindingType]] = [
        ("p", "pause", "Pause"),
        ("r", "resume", "Resume"),
        ("e", "focus_composer", "Message Master"),
        ("m", "membership_tab", "Membership"),
        ("a", "add_member", "Add member"),
        ("x", "withdraw_member", "Withdraw member"),
        ("n", "new_project", "New project"),
        ("f5", "reload", "Refresh"),
    ]

    def __init__(
        self, client: GatewayClient, project_id: str, *, id: str | None = None
    ) -> None:
        super().__init__(id=id)
        self.client = client
        self.project_id = project_id
        self.projection: dict[str, Any] = {}
        self.nodes: list[dict[str, Any]] = []
        self.members: list[dict[str, Any]] = []
        #: Set when an event has arrived and the screen has not caught up.
        self._behind = asyncio.Event()
        self._watching = True

    def compose(self) -> ComposeResult:
        yield Notice(id="notice")
        # 1. Master, present and at the top.
        yield Panel("Master", id="master-focus")
        # 2. What wants a person. Approvals sit directly under it because an
        # open one *is* something waiting on a person, and it is the only state
        # in which the runtime has stopped on purpose.
        yield Panel("Approvals", id="approvals")
        yield Panel("Attention required", id="attention")
        # 3. What is running.
        yield Panel("Execution", id="execution")
        # 4. The graph, read-only.
        yield Panel("Scientific DAG", id="dag-summary")
        yield StatusTable(DAG_COLUMNS, id="dag")
        # 5. Why the graph is the shape it is.
        with TabbedContent(id="record"):
            with TabPane("Decisions", id="tab-decisions"):
                yield Panel("Decisions", id="decisions")
            with TabPane("Reviews", id="tab-reviews"):
                yield Panel("Reviews", id="reviews")
            with TabPane("Research", id="tab-research"):
                yield Panel("Research results", id="research")
            with TabPane("Evidence", id="tab-evidence"):
                yield Panel("Evidence", id="evidence")
            with TabPane("Membership", id="tab-membership"):
                yield Panel("Who is in this project", id="members")
                yield StatusTable(
                    MEMBER_COLUMNS,
                    id="member-table",
                    selectable=True,
                    first_column_is_a_status=False,
                )
                with Horizontal(id="member-row"):
                    yield Input(placeholder="username to add", id="member-username")
                    yield Select(MEMBER_ROLES, prompt="as", id="member-role")
                # Opening a project belongs on this tab rather than beside the
                # commands, because it is the same thing the panel above is
                # about: a project's first membership is its creator's, and
                # there is nothing else a new project is.
                yield Input(placeholder="title for a new project", id="new-project-title")
        with Horizontal(id="composer-row"):
            yield Input(placeholder="Ask Master, or tell it something.", id="composer")

    # ── Following it ────────────────────────────────────────────────────────

    async def on_mount(self) -> None:
        """Start following the project's events, and keep following them.

        §7 asks the TUI to hold the last event sequence and replay from it on
        reconnect, and `GatewayClient.stream` does the holding — its
        `last_event_seq` advances as frames are *delivered* here, so a socket
        that dies mid-page resumes from the last event that reached the screen
        rather than from the last one the socket happened to receive.

        Two workers rather than one because the two jobs have different
        rhythms. Following is a long-lived read that must not be delayed by
        anything; redrawing is seven requests that must not be issued seven
        times for one burst. The event between them is `_behind`.
        """
        # Distinct groups, because `exclusive` cancels the rest of its group and
        # the default group is shared: two workers started without saying so
        # would leave the second having killed the first.
        self.run_worker(self._follow_events(), name="events", group="events", exclusive=True)
        self.run_worker(self._refresh_when_behind(), name="redraw", group="redraw", exclusive=True)

    async def _follow_events(self) -> None:
        """Read frames until the screen goes away, reconnecting when they stop.

        Every failure is survivable and none of them is fatal to the screen.
        The stream is a convenience on top of a screen that can always be
        refreshed by hand, so the loop reconnects with a delay and says so on
        the notice line rather than ending or raising.
        """
        while self._watching:
            try:
                async for frame in self.client.stream(self.project_id):
                    if frame.get("type") == "event":
                        self._behind.set()
            except Exception as dropped:  # a dropped socket is not a reason to exit
                if not self._watching:
                    return
                self.notice(f"Lost the live stream ({dropped}); retrying.", level="bad")
            if self._watching:
                await asyncio.sleep(RECONNECT_SECONDS)

    async def _refresh_when_behind(self) -> None:
        """Redraw once a burst has settled, for as long as the screen is up."""
        while self._watching:
            await self._behind.wait()
            await asyncio.sleep(SETTLE_SECONDS)
            self._behind.clear()
            if self._watching:
                await self.refresh_everything()

    async def on_unmount(self) -> None:
        """Stop following, so a screen that is gone stops issuing requests."""
        self._watching = False
        self._behind.set()

    # ── Filling it ──────────────────────────────────────────────────────────

    async def refresh_everything(self) -> None:
        """Read every panel's route and redraw.

        One method rather than a worker per panel: a screen whose panels arrive
        at different times is a screen that shows a stale DAG beside a fresh
        projection, and a person cannot tell which half they are reading. The
        cost is that the slowest route sets the pace, and all of them are
        indexed reads.

        A refusal is displayed rather than raised — the notice line carries it
        and the panels keep whatever they last had, because a Gateway that
        blipped should not empty the screen somebody is reading.
        """
        client = self.client
        try:
            self.projection = await client.projection(self.project_id)
            self.nodes = await client.dag(self.project_id)
            executions = await client.executions(self.project_id)
            decisions = await client.decisions(self.project_id)
            reviews = await client.reviews(self.project_id)
            evidence = await client.evidence(self.project_id)
            approvals = await client.approvals(self.project_id)
            messages = await client.messages(self.project_id)
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{refused}", level="bad")
            return
        # Read alongside the rest rather than lazily behind the tab: it is one
        # indexed read, and a tab that filled itself on first click would be
        # the one panel on this screen that could be stale. It is in its own
        # block because it is the only one of these routes that requires the
        # *owner* role rather than membership, so it is the only one that can
        # refuse a caller who is still, by the Gateway's reckoning, a member —
        # and an ownership withdrawn mid-session must blank one panel rather
        # than freeze the screen somebody is reading.
        members_refused = ""
        try:
            self.members = await client.members(self.project_id)
        except Exception as refused:
            self.members = []
            members_refused = str(refused)

        # The projection answers "where is this project now", and that includes
        # how far its stream has got. Seeding the cursor from it is what stops
        # every launch from replaying the project's whole history: the screen
        # has just drawn the current state, so the only events worth following
        # are the ones that come after it. `max` rather than assignment because
        # the socket is already advancing this number, and a redraw must never
        # move a live cursor backwards.
        self.client.credentials.last_event_seq = max(
            self.client.credentials.last_event_seq, int(self.projection.get("last_event_seq") or 0)
        )

        project = self.projection.get("project", {})
        self.panel("master-focus").show(
            [
                (f"{project.get('display_id', '')}  {project.get('title', '')}", "text"),
                (fmt.elide(str(project.get("objective", ""))), "muted"),
                "",
                *self._conversation(messages),
            ]
        )
        self.panel("attention").show(fmt.attention_lines(self.projection, self.nodes))
        self.panel("execution").show(fmt.execution_lines(self.nodes, executions))
        self.panel("dag-summary").show(
            [
                f"{fmt.symbol_for(project.get('status', ''))} "
                f"{project.get('status', '')}  —  {fmt.node_summary(self.nodes)}",
                (f"your role: {self.projection.get('role', '')}", "muted"),
            ]
        )
        self.query_one("#dag", StatusTable).fill(fmt.node_row(node) for node in self.nodes)
        self.panel("approvals").show(fmt.approval_lines(approvals))
        self.panel("decisions").show(fmt.decision_lines(decisions))
        self.panel("reviews").show(fmt.review_lines(reviews))
        self.panel("research").show(fmt.research_lines(evidence))
        self.panel("evidence").show(fmt.evidence_lines(evidence))

        self.panel("members").show(
            [members_refused] if members_refused else fmt.member_lines(self.members)
        )
        self.query_one("#member-table", StatusTable).fill(
            fmt.member_row(member) for member in self.members
        )

    @staticmethod
    def _conversation(messages: list[dict[str, Any]]) -> list[tuple[str, str]]:
        """The last few things said, oldest first, with who said them.

        Truncated to the tail because this panel exists to answer "where is
        Master", and a transcript that has scrolled back four hundred turns
        answers it worse than the last four do.

        Who spoke is `author_type` and not a name: a person's utterance carries
        a user id and Master's carries an agent identity, and resolving either
        to something readable would be a second lookup for a label. What a
        reader of this panel needs is which of the two is talking.
        """
        if not messages:
            return [("Nothing said yet. Say something below.", "muted")]
        lines: list[tuple[str, str]] = []
        for message in messages[-4:]:
            from_master = str(message.get("author_type", "")) == "AGENT"
            speaker = "Master" if from_master else "you"
            token = "accent" if from_master else "muted"
            body = fmt.elide(str(message.get("body", "")), 140)
            lines.append((f"{speaker}: {body}", token))
        return lines

    # ── Saying things ───────────────────────────────────────────────────────

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Send what was typed, and show the answer where Master's panel is."""
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        self.notice("Asking Master…", level="wait")
        try:
            await self.client.say(self.project_id, text)
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"Master did not answer: {refused}", level="bad")
            return
        self.notice("Master answered.", level="good")
        await self.refresh_everything()

    async def action_pause(self) -> None:
        await self._control("pause", "Paused.")

    async def action_resume(self) -> None:
        await self._control("resume", "Resumed.")

    async def action_focus_composer(self) -> None:
        self.query_one("#composer", Input).focus()

    async def action_add_member(self) -> None:
        """Give somebody who already has an account a role in this project.

        The account must exist: RAVEL has no registration over HTTP and this
        screen cannot make a person. A name that has no account is answered
        with the Gateway's own sentence rather than a 422, because the route
        answers 404 for it deliberately — "no account called that" is not a
        secret, and it is the repair the caller needs.
        """
        if not self._on_membership_tab():
            return
        username = self.query_one("#member-username", Input).value.strip()
        chosen = self.query_one("#member-role", Select).value
        if not username:
            self.notice("Name somebody to add.", level="bad")
            return
        if not isinstance(chosen, str):
            self.notice("Choose the role to give them.", level="bad")
            return

        self.notice(f"Adding {username} as {chosen}…", level="wait")
        try:
            await self.client.add_member(self.project_id, username, chosen)
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{username} was not added: {refused}", level="bad")
            return
        self.query_one("#member-username", Input).value = ""
        self.notice(f"{username} is now {chosen} here.", level="good")
        await self.refresh_everything()

    async def action_withdraw_member(self) -> None:
        """Take the selected member's role away. The row stays; the authority goes.

        The confirmation is the notice line and the row's disappearance rather
        than a modal, and the trade is deliberate: the action is reversible by
        adding the person back, the effect is immediate on their next request —
        which the panel says — and a modal on a console somebody drives from a
        keyboard is a second thing to dismiss. What is *not* reversible is the
        record, and that is the point of the withdrawal rather than a gap in it.
        """
        if not self._on_membership_tab():
            return
        chosen = self._selected_member()
        if chosen is None:
            self.notice("Select the member to withdraw in the table first.", level="bad")
            return

        self.notice(f"Withdrawing {chosen.get('username', '')}…", level="wait")
        try:
            await self.client.revoke_member(self.project_id, str(chosen.get("user_id", "")))
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{chosen.get('username', '')} was not withdrawn: {refused}", level="bad")
            return
        self.notice(
            f"{chosen.get('username', '')} no longer holds a role here; the record of "
            "the withdrawal stays.",
            level="good",
        )
        await self.refresh_everything()

    async def action_new_project(self) -> None:
        """Open a project, owned by whoever opened it.

        The one membership nobody confers, and the reason the Gateway's route is
        open to any authenticated caller: there is nobody to be an owner *of*
        yet. It sits on the membership tab because a project's first membership
        is not a separate subject.

        Opening one does not switch to it. That would mean this screen deciding
        which project the application is looking at, and `RavelTUI` is the thing
        that knows what it has mounted — so what happens here is that the
        application is told to re-read its memberships, which is what makes
        `ctrl+n` able to reach the project that was just made.
        """
        if not self._on_membership_tab():
            return
        title = self.query_one("#new-project-title", Input).value.strip()
        if not title:
            self.notice("Name the new project in the box, then press n again.", level="bad")
            return
        self.notice(f"Opening {title}…", level="wait")
        try:
            opened = await self.client.open_project(
                title, "Opened from the console; no objective has been stated yet."
            )
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{title} was not opened: {refused}", level="bad")
            return
        self.query_one("#new-project-title", Input).value = ""
        self.notice(
            f"Opened {opened.get('display_id', '')} ({title}). "
            "Press ctrl+n to move to it.",
            level="good",
        )
        await cast("RavelTUI", self.app).reload_memberships()

    # ── The membership tab's own rules ──────────────────────────────────────

    def _on_membership_tab(self) -> bool:
        """Whether the membership controls are the ones on screen.

        The guard all three membership bindings share. `a`, `x` and `n` fire
        wherever this screen has focus, so without this a reader pressing `x`
        while looking at the DAG would be withdrawing whoever happened to be
        selected in a table they cannot see. The refusal names the key that
        opens the tab rather than staying silent, so a person who meant to do
        it is told how rather than being told no.
        """
        record = self.query_one("#record", TabbedContent)
        if record.active == MEMBERSHIP_TAB:
            return True
        self.notice(
            "Membership is on its own tab — press m to open it.", level="bad"
        )
        return False

    def action_membership_tab(self) -> None:
        """Bring the membership tab up, and put the cursor in the table.

        The companion to the guard above rather than a convenience: a refusal
        that names a key has to name one that exists, and this is what makes
        `m` in that sentence true. Focus goes to the table rather than to the
        username field because the first thing an owner does here is more often
        look at who is present than type a name.
        """
        self.query_one("#record", TabbedContent).active = MEMBERSHIP_TAB
        self.query_one("#member-table", StatusTable).focus()

    def _selected_member(self) -> dict[str, Any] | None:
        """The member under the table's cursor, or `None` if nothing is chosen.

        The row index is checked against the list rather than trusted: the
        table is refilled on every redraw, and a cursor left past the end of a
        list that just got shorter must answer "nothing selected" rather than
        raise out of a keypress.
        """
        table = self.query_one("#member-table", StatusTable)
        index = table.cursor_row
        if 0 <= index < len(self.members):
            return self.members[index]
        return None

    async def _control(self, command: str, done: str) -> None:
        """Send one of the two life-cycle commands, and say what happened.

        Both are the owner's alone — the Gateway checks `may_direct_project` —
        and neither touches the DAG: stopping a project stops work, it does not
        rewrite the plan.
        """
        client = self.client
        try:
            if command == "pause":
                await client.pause(self.project_id)
            else:
                await client.resume(self.project_id)
        except Exception as refused:  # a refusal is shown, not swallowed
            self.notice(f"{command} was refused: {refused}", level="bad")
            return
        self.notice(done, level="good")
        await self.refresh_everything()

    # ── Small helpers the tests read too ────────────────────────────────────

    def panel(self, name: str) -> Panel:
        return self.query_one(f"#{name}", Panel)

    def notice(self, message: str, *, level: str = "info") -> None:
        self.query_one("#notice", Notice).say(message, level=level)


__all__ = [
    "DAG_COLUMNS",
    "MEMBERSHIP_TAB",
    "MEMBER_COLUMNS",
    "MEMBER_ROLES",
    "OwnerScreen",
]
