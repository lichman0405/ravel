"""The handful of widgets the screens are built from.

Three ideas, and the screens are combinations of them. A **panel** is a titled
block of lines. A **table** is columns. A **notice** is one line that says what
just happened. Everything `docs/08` §4 asks for in a console — density,
hierarchy, few borders — comes from having only these, because a screen that
can only be a panel or a table cannot grow a fourth kind of decoration.

**Each widget keeps the lines it is showing.** `Panel.lines` is the plain text
behind the renderable, and the tests read it instead of picking through Rich's
object graph. That is not only for the tests: it is also the honest description
of what a panel *is* here — a list of strings with, at most, a colour each — and
a widget whose content could only be recovered by rendering it would be a
widget that had stopped being about the data.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import ClassVar

from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import DataTable, Static

from ravel.tui.tokens import TOKENS, colour_for, symbol_for

#: One line of a panel: plain text, or text and the token it is drawn in.
Line = str | tuple[str, str]


class Panel(Static):
    """A titled block of lines, with the title dimmed and the body not.

    The title is part of the same renderable rather than a second widget
    because a `Static` per title per panel is how a screen ends up with four
    nested containers to say one thing. The blank line after it is the
    whitespace §4 asks for in place of a border.
    """

    def __init__(self, title: str, *, id: str | None = None, classes: str | None = None) -> None:
        super().__init__(id=id, classes=classes)
        self.title_text = title
        self.lines: list[Line] = []
        self._redraw()

    def show(self, lines: Iterable[Line]) -> None:
        """Replace the body. An empty body becomes a sentence, not a blank."""
        self.lines = list(lines)
        self._redraw()

    def _redraw(self) -> None:
        body = Text()
        body.append(self.title_text.upper() + "\n", style=f"bold {TOKENS['muted']}")
        if not self.lines:
            body.append("Nothing to show.\n", style=TOKENS["muted"])
        for line in self.lines:
            text, token = line if isinstance(line, tuple) else (line, "text")
            body.append(text + "\n", style=TOKENS.get(token, TOKENS["text"]))
        self.update(body)

    @property
    def plain(self) -> str:
        """The panel as text, which is what a person reading it would see."""
        return "\n".join(line if isinstance(line, str) else line[0] for line in self.lines)


class StatusTable(DataTable[Text]):
    """A table whose first column is a status, drawn in that status's colour.

    A `DataTable` rather than a panel of lines because these are genuinely
    tabular — a DAG row and a task row both have a status, an identifier and a
    thing — and a person scanning for the failed one wants the columns lined up.

    **A table whose first column is not a status says so.** The member table's
    first column is a *role*, and running a role through this vocabulary would
    put a `?` beside every one of them: `tokens` deliberately has no glyph for
    something it does not know, because "this program does not recognise that
    status" is worth seeing on a DAG. A role is not an unrecognised status, it
    is not a status at all, and `first_column_is_a_status=False` is how a table
    asks for the columns it actually has.
    """

    def __init__(
        self,
        columns: Sequence[str],
        *,
        id: str | None = None,
        selectable: bool = False,
        first_column_is_a_status: bool = True,
    ) -> None:
        super().__init__(
            id=id,
            zebra_stripes=False,
            show_header=True,
            # A cursor is what makes a row selectable, and `DataTable` posts
            # `RowSelected` for a row cursor and for nothing else. A table left
            # on the default cell cursor still takes the arrow keys and still
            # moves — it just moves an invisible highlight and posts
            # `CellSelected`, so a screen waiting for `RowSelected` waits
            # forever. The cursor is shown exactly when it means something: a
            # display-only table has nothing to select and no reason to draw
            # one, and the lab's task list is the one table a person picks from.
            cursor_type="row" if selectable else "cell",
            show_cursor=selectable,
        )
        self._columns = list(columns)
        self._first_is_a_status = first_column_is_a_status
        self.add_columns(*self._columns)

    def fill(self, rows: Iterable[tuple[str, str, str]]) -> None:
        """Replace every row. `(status, identifier, summary)`.

        The status column is drawn from `tokens`, so a new status is a row in
        the token table and not a change here. With
        `first_column_is_a_status=False` the first field is drawn as plain
        text, in the muted style the second column uses.
        """
        self.clear()
        for status, identifier, summary in rows:
            first = (
                Text(f"{symbol_for(status)} {status}", style=colour_for(status))
                if self._first_is_a_status
                else Text(status, style=TOKENS["text"])
            )
            self.add_row(
                first,
                Text(identifier, style=TOKENS["muted"]),
                Text(summary, style=TOKENS["text"]),
            )


class Notice(Static):
    """One line about what just happened, and the colour of how it went.

    Not a modal, not a toast, not a log. A console that interrupts is a console
    nobody leaves running, and the events a person needs from *their own*
    actions are few enough to say in one line that the next action replaces.
    """

    LEVELS: ClassVar[dict[str, str]] = {
        "info": "accent",
        "good": "success",
        "bad": "failed",
        "wait": "waiting",
    }

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        self.message = ""
        self.level = "info"
        self.say("Ready.", level="info")

    def say(self, message: str, *, level: str = "info") -> None:
        self.message = message
        self.level = level
        self.update(Text(message, style=TOKENS[self.LEVELS.get(level, "accent")]))


class Scrolling(VerticalScroll):
    """A scrolling column, which is what every screen is.

    Exists so the CSS can name one class instead of attaching the same three
    rules to four containers.
    """


__all__ = ["Line", "Notice", "Panel", "Scrolling", "StatusTable"]
