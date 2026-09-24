"""The console speaks two languages, and this is what holds that to be true.

Three claims, and each one is the failure that would otherwise be found by
somebody who cannot read the screen.

**A message exists in both languages or the program does not start.** That is a
property of `MESSAGES`' shape — a key maps to an `(english, chinese)` pair — so
the test is about the *pairs*: both halves non-empty, both asking for the same
`{fields}`, both formattable. A translation that drops a placeholder is a line
that silently loses a fact, and neither language's half is allowed to be a
placeholder itself.

**Every sentence in the console lives in the catalogue.** The sweep at the
bottom walks the syntax tree of every other module under `ravel.tui` and fails
on a string that reads like prose. The heuristic is deliberately crude — two or
more whitespace-separated runs of letters — and it is crude on purpose: it has
to be obvious to a reader why a literal was flagged, and the repair is always
the same one, which is to move the sentence into `i18n.py` and name it by key.
The two literals that are not prose and not keys are exempted by name below,
each with the reason it is exempt, so a fourth one cannot slip in unnoticed.

**Switching is a function of the module and not of a widget.** `set_language`
refuses a language this console does not speak, changes nothing when it refuses,
and `RAVEL_TUI_LANGUAGE` is read once at import — so the test reloads the module
to ask what a deployment would actually get.
"""

from __future__ import annotations

import ast
import importlib
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from ravel.tui import i18n
from ravel.tui.i18n import (
    DEFAULT_LANGUAGE,
    LANGUAGE_ENV,
    LANGUAGES,
    MESSAGES,
    UnknownLanguage,
    UnknownMessage,
)

#: Where the console's modules are.
TUI_ROOT = Path(__file__).resolve().parents[2] / "src" / "ravel" / "tui"

#: The messages that are the same words in both languages, which is what a
#: *name* is. Listed rather than derived, so that a sixth one has to be argued
#: for here: the shape of this test is "somebody decided", not "it happened to
#: match". `fmt.recon.record` is the odd one and is not a name — it is a line
#: built entirely of placeholders and an arrow, and it has no English in it to
#: translate.
SAME_IN_BOTH = frozenset(
    {
        "admin.panel.harness",
        "admin.panel.temporal",
        "fmt.recon.record",
        "owner.conversation.master",
        "owner.panel.master",
    }
)

#: A key: dotted, lower case, no spaces.
KEY = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$")

#: Two or more runs of two or more letters, separated by whitespace: prose.
PROSE = re.compile(r"[A-Za-z]{2,}\S*\s+\S*[A-Za-z]{2,}")

#: Literals that are not keys and would otherwise read as prose, each with why.
EXEMPT = {
    # The stylesheet. CSS is not spoken to a reader in any language; the tokens
    # it is written from are in `tokens.py` and the panels are titled from the
    # catalogue.
    "CSS",
}


@pytest.fixture(autouse=True)
def english_again() -> Iterator[None]:
    """Every test here leaves the console speaking what it started in."""
    before = i18n.current()
    yield
    i18n.set_language(before)


def _is_prose(value: str) -> bool:
    """Whether a string literal reads like something said to a person."""
    return bool(PROSE.search(value.strip()))


def _prose_literals(path: Path) -> list[tuple[int, str]]:
    """Every string literal in `path` that reads like prose.

    Docstrings are excluded, because a docstring is addressed to whoever
    maintains this program and speaking to them in one language is the point.
    A bare string statement is excluded with them — it *is* a docstring, or it
    is nothing, and there is no third case in this package.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    }
    exempt: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id in EXEMPT for target in node.targets
        ):
            exempt.update(id(inner) for inner in ast.walk(node.value))

    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        if id(node) in docstrings or id(node) in exempt:
            continue
        if _is_prose(node.value):
            found.append((node.lineno, node.value))
    return found


# ── The catalogue ───────────────────────────────────────────────────────────


def test_every_message_is_written_in_both_languages() -> None:
    """A pair, both halves non-empty, and neither one a blank or a key."""
    for key, pair in MESSAGES.items():
        assert len(pair) == 2, key
        english, chinese = pair
        for text in (english, chinese):
            assert text.strip(), f"{key} is empty in one language"
            assert not KEY.match(text), f"{key} holds a key rather than a message"


def test_the_keys_are_shaped_like_keys() -> None:
    """`screen.thing`, lower case. A key is an identifier, not a sentence."""
    for key in MESSAGES:
        assert KEY.match(key), f"{key} is not a key"


def test_both_languages_ask_for_the_same_fields() -> None:
    """Same `{fields}`, and a message that can actually be formatted.

    A placeholder dropped in translation loses a fact quietly — the line still
    reads, it just stops saying which node or how long ago — so the two halves
    are compared field by field rather than by eye.
    """
    for key, (english, chinese) in MESSAGES.items():
        wanted = i18n.fields_in(english)
        assert wanted == i18n.fields_in(chinese), f"{key} asks for different fields"
        filled = {name: "x" for name in wanted}
        assert english.format(**filled)
        assert chinese.format(**filled)


def test_a_singular_message_has_a_plural_one() -> None:
    """Every `.one` is a sibling of a message, and never a key of its own."""
    for key in MESSAGES:
        if key.endswith(".one"):
            assert key[: -len(".one")] in MESSAGES, key


def test_only_names_read_the_same_in_both_languages() -> None:
    """The list of untranslated messages is a decision, not an oversight."""
    same = {key for key, (english, chinese) in MESSAGES.items() if english == chinese}
    assert same == SAME_IN_BOTH, f"untranslated and not accounted for: {same - SAME_IN_BOTH}"


# ── Saying one ──────────────────────────────────────────────────────────────


def test_a_message_is_read_in_the_language_that_is_current() -> None:
    i18n.set_language("en")
    english = i18n.t("owner.notice.paused")
    i18n.set_language("zh")
    assert i18n.t("owner.notice.paused") != english
    assert i18n.t("owner.notice.paused") == MESSAGES["owner.notice.paused"][1]


def test_fields_are_filled_in() -> None:
    i18n.set_language("en")
    assert "bench" in i18n.t("owner.notice.withdrawing", username="bench")


def test_a_count_of_one_takes_the_singular_message() -> None:
    i18n.set_language("en")
    assert i18n.t("fmt.ago.minute", count=1) == "1 minute ago"
    assert i18n.t("fmt.ago.minute", count=2) == "2 minutes ago"
    # And a count on a key with no singular sibling is simply filled in.
    assert i18n.t("fmt.research.claims", count=1) == "1 claim(s) recorded"


def test_a_key_that_does_not_exist_is_named_rather_than_shown() -> None:
    with pytest.raises(UnknownMessage):
        i18n.t("owner.notice.there_is_no_such_message")


def test_an_unknown_language_is_refused_and_changes_nothing() -> None:
    before = i18n.current()
    with pytest.raises(UnknownLanguage):
        i18n.set_language("fr")
    assert i18n.current() == before
    with pytest.raises(UnknownLanguage):
        i18n.set_language(None)  # type: ignore[arg-type]


def test_toggling_moves_between_every_language_and_comes_back() -> None:
    start = i18n.current()
    seen = []
    for _ in LANGUAGES:
        seen.append(i18n.toggle())
    assert sorted(seen) == sorted(LANGUAGES)
    assert i18n.current() == start


def test_the_environment_says_what_the_console_starts_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """`RAVEL_TUI_LANGUAGE` is read at import, which is what a deployment gets."""
    monkeypatch.setenv(LANGUAGE_ENV, "zh")
    reloaded = importlib.reload(i18n)
    try:
        assert reloaded.current() == "zh"
    finally:
        monkeypatch.delenv(LANGUAGE_ENV)
        importlib.reload(i18n)
    assert i18n.current() == DEFAULT_LANGUAGE


def test_the_environment_cannot_name_a_language_this_console_lacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deployment that asked for a language nobody has is told at startup."""
    monkeypatch.setenv(LANGUAGE_ENV, "fr")
    # Caught by the base class rather than by `UnknownLanguage`: reloading the
    # module replaces that class with a new object, and the name this file
    # imported at startup is the old one.
    with pytest.raises(ValueError) as refused:
        importlib.reload(i18n)
    assert "fr" in str(refused.value)
    monkeypatch.delenv(LANGUAGE_ENV)
    importlib.reload(i18n)


# ── And every other module keeps its sentences here ─────────────────────────


def test_every_sentence_in_the_console_lives_in_this_catalogue() -> None:
    """The sweep: no module but this one contains a sentence.

    Read as syntax rather than as text, so a sentence inside a docstring (which
    is addressed to a maintainer) and a sentence inside a comment (which is not
    a string at all) are both left alone, while one inside a `Label`, a notice
    or an `f`-string is caught.
    """
    offenders: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(TUI_ROOT.rglob("*.py")):
        if path.name == "i18n.py":
            continue
        if found := _prose_literals(path):
            offenders[path.name] = found

    assert not offenders, "\n".join(
        [
            f"{name}:{lineno}: {value!r}"
            for name, found in offenders.items()
            for lineno, value in found
        ]
    )


def test_the_catalogue_is_the_only_module_with_sentences_in_it() -> None:
    """The sweep's mirror image: this module is full of them, by design."""
    assert _prose_literals(TUI_ROOT / "i18n.py"), (
        "the catalogue has no prose in it, so the sweep above is checking nothing"
    )
