"""The application: sign in, choose a project, and show the screen that role gets.

`docs/08` §5 gives each of the three user roles a view, and A18 states them as
a list of permissions. This module is where that list becomes a mapping —
`ROLE_SCREENS` — and the mapping is deliberately a data structure rather than
three branches inside a mount method, because the thing worth testing is
"which role gets which screen" and a test can read a dictionary.

**The role comes from the Gateway, never from the client.** `GET /projects`
answers with this caller's memberships and the role held in each; nothing here
takes a role as an argument, and a screen cannot be selected by asking for it.
That is the same rule the Gateway enforces from the other side — it re-derives
the caller's standing from the database on every request — and it means a TUI
that lied about its role would show itself a screen whose buttons all fail.

**A person with several projects switches between them; a person with one is
put in it.** `project_id` may be given (the CLI's `--project`, and what the
tests use); otherwise the only membership is chosen, and with more than one the
screen says which and waits to be told.

**`F2` changes the language, and changing it rebuilds.** The console speaks
English or Chinese — see `ravel.tui.i18n` — and the two are a process setting
rather than a per-widget one, so the switch is one call and then a redraw. The
redraw is a `show_project` rather than a walk over the mounted widgets, and
that is the honest repair rather than a lazy one: a panel reads its title out of
the catalogue every time it draws, but a `DataTable`'s column headers and a
`TabPane`'s label are set once, when they are constructed. Rebuilding is what
makes those two come out in the new language, and the cost — re-reading the
routes — is a handful of indexed reads behind a keystroke nobody presses twice
a second.
"""

from __future__ import annotations

from typing import Any, ClassVar, cast

from textual.app import App, ComposeResult
from textual.binding import BindingType
from textual.containers import Container
from textual.screen import Screen
from textual.widgets import Header, Input, Label

from ravel.tui import i18n
from ravel.tui.client import GatewayClient, GatewayError
from ravel.tui.i18n import t
from ravel.tui.screens.admin import AdminScreen
from ravel.tui.screens.lab import LabScreen
from ravel.tui.screens.owner import OwnerScreen
from ravel.tui.tokens import TOKENS
from ravel.tui.widgets import KeyHints

#: Which screen each role gets. `docs/08` §5, as a table.
#:
#: A missing role is `None` from `screen_for_role` rather than a default,
#: because the only defensible default would be the *most* restricted screen
#: and getting that wrong in the other direction shows a lab user the whole
#: project's DAG. A role this program does not know how to draw is a screen
#: that says so.
ROLE_SCREENS: dict[str, type[OwnerScreen | LabScreen | AdminScreen]] = {
    "PROJECT_OWNER": OwnerScreen,
    "LAB_USER": LabScreen,
    "ADMIN": AdminScreen,
}

_COLOURS = {name: value for name, value in TOKENS.items()}

#: The stylesheet, written from the tokens rather than beside them. A hex code
#: typed twice is a hex code that will disagree with itself eventually.
CSS = f"""
Screen {{
    background: {_COLOURS["background"]};
    color: {_COLOURS["text"]};
}}
Header {{
    background: {_COLOURS["surface"]};
    color: {_COLOURS["text"]};
}}
KeyHints {{
    background: {_COLOURS["surface"]};
    color: {_COLOURS["muted"]};
}}
#body {{
    height: 1fr;
}}
Panel {{
    height: auto;
    padding: 0 1;
}}
StatusTable {{
    height: auto;
    max-height: 40%;
    background: {_COLOURS["background"]};
}}
Notice {{
    height: auto;
    padding: 0 1;
}}
Input {{
    background: {_COLOURS["surface"]};
    border: none;
}}
Select {{
    background: {_COLOURS["surface"]};
}}
TabbedContent {{
    height: auto;
}}
#signin {{
    align: center middle;
}}
#signin-form {{
    width: 60;
    height: auto;
    border: round {_COLOURS["border"]};
    padding: 1 2;
    background: {_COLOURS["surface"]};
}}
#signin-error {{
    color: {_COLOURS["failed"]};
    height: auto;
}}
"""


def screen_for_role(role: str) -> type[OwnerScreen | LabScreen | AdminScreen] | None:
    """The screen a role gets, or `None` if this program does not know it.

    A module-level function rather than a method so that the mapping can be
    tested without starting anything: A18's three sentences are a claim about
    this function, and a claim about a function is cheaper to check than a
    claim about a mounted screen.
    """
    return ROLE_SCREENS.get(role.upper())


class SignIn(Screen[None]):
    """Two fields and a button, because that is all V0 has.

    There is no account creation here and no password reset: `docs/06` fixes
    the three roles and `ravel.gateway` exposes no route that makes a user, so
    a "register" button would be a button that cannot work.

    **The line under the form is a small state machine, not a string.** It has
    three states — nothing to say, a sign-in in flight, and a refusal — and
    which one it is in is held as a fact rather than as whatever text was last
    written there. That is what lets `relabel` redraw the line when the language
    changes without having to guess whether what is on it is a sentence this
    program wrote or a Gateway's own words, which stay as the Gateway wrote
    them.
    """

    BINDINGS: ClassVar[list[BindingType]] = [("escape", "quit_app", "binding.quit_app")]

    def __init__(self) -> None:
        super().__init__()
        #: True while a sign-in is in flight.
        self.trying = False
        #: The Gateway's own refusal, shown as it wrote it.
        self.refused = ""

    def compose(self) -> ComposeResult:
        with Container(id="signin"), Container(id="signin-form"):
            yield Label("RAVEL")
            yield Input(placeholder=t("signin.username"), id="username")
            yield Input(placeholder=t("signin.password"), password=True, id="password")
            yield Label("", id="signin-error")

    def on_mount(self) -> None:
        self.query_one("#username", Input).focus()

    def relabel(self) -> None:
        """Say the same thing in the language the console is speaking now.

        What was typed is left alone — a person who switched language halfway
        through typing their password should not have to type it again — and so
        is a refusal, which is the Gateway's sentence and not this program's.
        """
        self.query_one("#username", Input).placeholder = t("signin.username")
        self.query_one("#password", Input).placeholder = t("signin.password")
        self._show()

    def _show(self) -> None:
        """Draw whichever of the three states the form is in."""
        label = self.query_one("#signin-error", Label)
        if self.trying:
            label.update(t("signin.signing_in"))
        else:
            label.update(self.refused)

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Try to sign in once both fields have something in them.

        Enter after the username moves to the password rather than attempting a
        sign-in that cannot succeed, and Enter after the password attempts it.
        The failure is shown in place rather than as a message box, so the
        fields stay where they were and nothing has to be typed again.
        """
        username = self.query_one("#username", Input)
        password = self.query_one("#password", Input)
        if event.input is username and not password.value:
            password.focus()
            return

        self.trying = True
        self.refused = ""
        self._show()
        try:
            await cast(RavelTUI, self.app).sign_in(username.value.strip(), password.value)
        except GatewayError as refused:
            self.refused = refused.detail
        self.trying = False
        # A sign-in that worked is one where `sign_in` has already taken this
        # screen down, and a widget that is no longer mounted has no line to
        # update — and no refusal to show either, which is the good ending.
        if self.is_mounted:
            self._show()


class RavelTUI(App[None]):
    """The console described by `docs/08`, and the whole of the user's entry point."""

    CSS = CSS
    TITLE = "RAVEL"
    #: No command palette. It lists action *names* — `pause`, `add_member`,
    #: `report_deviation` — which are identifiers from this module's methods
    #: rather than messages anyone has a translation for, and a palette of
    #: English identifiers over a Chinese screen is worse than no palette on a
    #: console whose keys are all on one line at the bottom.
    ENABLE_COMMAND_PALETTE = False
    # The descriptions are message keys rather than sentences, and
    # `binding.toggle_language` names F2 in the language it switches *to* — so
    # a reader who cannot read the screen can see which key to press.
    BINDINGS: ClassVar[list[BindingType]] = [
        ("ctrl+q", "quit", "binding.quit"),
        ("ctrl+n", "next_project", "binding.next_project"),
        ("f2", "toggle_language", "binding.toggle_language"),
    ]

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        username: str = "",
        password: str = "",
        project_id: str = "",
        client: GatewayClient | None = None,
    ) -> None:
        super().__init__()
        self.client = client or GatewayClient(base_url=base_url)
        self._username = username
        self._password = password
        self.project_id = project_id
        self.memberships: list[dict[str, Any]] = []
        self.body: Container | None = None
        self.role_screen: OwnerScreen | LabScreen | AdminScreen | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(id="body")
        yield KeyHints(id="key-hints")

    async def on_mount(self) -> None:
        """Sign in if credentials were given, else ask for them.

        `username` and `password` as constructor arguments exist for the same
        reason `project_id` does: a test that has to drive a login form to find
        out which screen an owner gets is a test that cannot fail for the
        reason it was written. The interactive path is the default.
        """
        self.body = self.query_one("#body", Container)
        if self._username and self._password:
            await self.sign_in(self._username, self._password)
        else:
            await self.push_screen(SignIn())

    async def sign_in(self, username: str, password: str) -> None:
        """Authenticate, learn the memberships, and show the right screen.

        Raises:
            GatewayError: if the Gateway refused. The caller — the sign-in
                screen — shows the detail and leaves the fields alone.
        """
        await self.client.login(username, password)
        self.memberships = await self.client.projects()
        if self.screen_stack and isinstance(self.screen_stack[-1], SignIn):
            await self.pop_screen()
        if not self.project_id and len(self.memberships) == 1:
            self.project_id = str(self.memberships[0]["project_id"])
        await self.show_project()

    async def show_project(self) -> None:
        """Mount the screen for this caller's role in the selected project."""
        if self.body is None:
            return
        await self.body.remove_children()

        standing = self.standing()
        if standing is None:
            self.role_screen = None
            await self.body.mount(
                Label(
                    self._no_project_message(),
                    id="no-project",
                )
            )
            return

        screen_type = screen_for_role(str(standing["role"]))
        if screen_type is None:
            self.role_screen = None
            await self.body.mount(
                Label(t("app.unknown_role", role=standing["role"]), id="unknown-role")
            )
            return

        screen = screen_type(self.client, self.project_id)
        self.role_screen = screen
        await self.body.mount(screen)
        screen.focus()
        await screen.refresh_everything()

    def standing(self) -> dict[str, Any] | None:
        """This caller's membership in the selected project, or `None`.

        Read from what the Gateway answered rather than from anything stored
        here, so a screen can only ever be chosen for a project the Gateway
        already said this person is in.
        """
        for membership in self.memberships:
            if str(membership["project_id"]) == self.project_id:
                return membership
        return None

    def _no_project_message(self) -> str:
        """What to say when there is no project this caller is looking at.

        Two different facts, and the second is not a failure: somebody in
        several projects has simply not been told which one, and the sentence
        that says so names them all.
        """
        if not self.memberships:
            return t("app.no_membership")
        named = ", ".join(
            f"{membership['display_id']} ({membership['role']})" for membership in self.memberships
        )
        return t("app.several_projects", named=named)

    async def action_toggle_language(self) -> None:
        """Switch between the languages this console speaks, and redraw.

        The sign-in screen is relabelled in place rather than rebuilt, because
        rebuilding it would take half-typed credentials with it — a person who
        cannot read the form is exactly the person most likely to have started
        typing in it. Every other screen is rebuilt through `show_project`,
        which is the same path a project switch takes and therefore the same
        one the tests already cover.
        """
        i18n.toggle()
        top = self.screen_stack[-1] if self.screen_stack else None
        if isinstance(top, SignIn):
            top.relabel()
        else:
            await self.show_project()
        self.query_one(KeyHints).refresh_hints()

    async def action_next_project(self) -> None:
        """Move to the next membership, and redraw for the role held there.

        The role is looked up per project rather than once at sign-in, because
        the same person may own one project and work the bench in another, and
        a screen chosen at login would be wrong for one of them.
        """
        if len(self.memberships) < 2:
            return
        identifiers = [str(membership["project_id"]) for membership in self.memberships]
        try:
            here = identifiers.index(self.project_id)
        except ValueError:
            here = -1
        self.project_id = identifiers[(here + 1) % len(identifiers)]
        await self.show_project()

    async def reload_memberships(self) -> None:
        """Re-read what this person is in, and redraw only if that matters here.

        Called when the membership list itself has changed underneath the
        screen — chiefly when an owner opens a project, which `ctrl+n` could
        not otherwise reach, since the list is read at sign-in. The current
        project is deliberately kept: opening a project is not the same wish as
        leaving the one being looked at.

        **The screen is only rebuilt when the caller's standing in *this*
        project has changed.** A role granted, withdrawn or altered arrives
        here too, and the Gateway has been enforcing it since the moment it was
        written — what this redraw does is bring the *screen* into line with a
        decision already in force. But a change to some other project does not
        make the mounted screen wrong, and tearing it down for one would throw
        away the notice line, the open tab and the reader's place in it. A
        demotion still lands on the screen its new role gets, because
        `show_project` picks by role rather than by remembering which one was
        drawn last.
        """
        before = self._standing_key()
        self.memberships = await self.client.projects()
        if self._standing_key() != before:
            await self.show_project()

    def _standing_key(self) -> tuple[str, str] | None:
        """This caller's place in the current project, as something comparable.

        `None` when they are in none, which is a different fact from a
        membership in a project they have since been withdrawn from — and the
        one case where the mounted screen has to go, because there is no
        standing left to pick a screen from.
        """
        standing = self.standing()
        return None if standing is None else (str(standing["project_id"]), str(standing["role"]))

    async def action_quit_app(self) -> None:
        self.exit()

    async def on_unmount(self) -> None:
        await self.client.aclose()


def run() -> None:
    """Start the console. The entry point `python -m ravel.tui` calls."""
    RavelTUI().run()


__all__ = ["CSS", "ROLE_SCREENS", "RavelTUI", "SignIn", "run", "screen_for_role"]
