"""What RAVEL reads out of a document, and what it refuses to.

`KNOWN_LIMITATIONS.md` L-25 was that a source RAVEL stored in full could only
ever be seen through a six-hundred-character excerpt. The fix is a reader, and
a reader's whole risk is the same one the excerpt had: producing text that is
not the document's. So these cases are mostly about the edges — a scan, an
encrypted file, a format with no words in it, a JSON document that does not
parse — where the honest answer is a sentence saying what is there and the
tempting answer is plausible text.

The other half is the arithmetic a bounded reader is made of: that a window is
clamped rather than refused, that an offset a search reports is an offset a
read can use, and that a page range is refused rather than quietly narrowed.
Every one of those is a place where a reader that is slightly wrong reads the
document as something it is not, and the reader never finds out.
"""

from __future__ import annotations

import pytest
from tests.documents import a_pdf, a_scanned_pdf, an_encrypted_pdf

from ravel.research import deepread
from ravel.research.deepread import (
    COUNT_LIMIT,
    MAX_EXTRACT_CHARS,
    MAX_PAGES_PER_READ,
    ReadFormat,
    document_of,
    find,
    format_of,
    page_range,
    window,
)
from ravel.research.fetching import EXCERPT_CHARS, excerpt_of

# ── What a body is ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("text/html", ReadFormat.HTML),
        ("text/html; charset=utf-8", ReadFormat.HTML),
        ("application/xhtml+xml", ReadFormat.HTML),
        ("application/xml", ReadFormat.XML),
        ("text/xml", ReadFormat.XML),
        ("application/atom+xml", ReadFormat.XML),
        ("application/json", ReadFormat.JSON),
        ("application/vnd.api+json", ReadFormat.JSON),
        ("text/plain", ReadFormat.TEXT),
        ("text/csv", ReadFormat.TEXT),
        ("text/x-bibtex", ReadFormat.TEXT),
        ("application/pdf", ReadFormat.PDF),
        ("image/png", ReadFormat.OTHER),
        ("application/zip", ReadFormat.OTHER),
        ("", ReadFormat.OTHER),
        (None, ReadFormat.OTHER),
    ],
)
def test_each_media_type_is_read_as_itself(media_type: str | None, expected: ReadFormat) -> None:
    """The mapping is RAVEL's decision about how to read, and it is stated here.

    Written out rather than derived, because the point of the table is to be
    read: a type that moves from one format to another is a change in what a
    Research session sees, and it should be a change somebody agreed to.
    """
    assert format_of(b"body", media_type) is expected


def test_a_pdf_is_read_as_a_pdf_when_the_server_does_not_say_it_is_one() -> None:
    """`%PDF-` is the format's own declaration, and publishers omit the header.

    A paper served as `application/octet-stream` is common enough that refusing
    to read it would be refusing for no reason — and the note says which of the
    two sources of truth was believed, so the decision is visible.
    """
    assert format_of(a_pdf(["x"]), "application/octet-stream") is ReadFormat.PDF
    extraction = document_of(a_pdf(["x"]), "application/octet-stream")
    assert "begin with %PDF-" in extraction.note
    assert "application/octet-stream" in extraction.note


def test_a_pdf_a_server_calls_html_is_read_as_a_pdf_anyway() -> None:
    """The bytes win, because the bytes are what arrived.

    Nothing that begins with `%PDF-` is HTML, and reading it as HTML would
    produce a page of binary noise presented as the document's text.
    """
    assert format_of(a_pdf(["x"]), "text/html") is ReadFormat.PDF


# ── What text comes out ─────────────────────────────────────────────────────


def test_html_is_read_as_the_words_and_says_what_was_removed() -> None:
    """Tags, script and style are markup; the text around them is the document."""
    extraction = document_of(
        b"<html><head><style>p{color:red}</style><script>var x=1;</script>"
        b"</head><body><p>Solubility was 12 g/L</p></body></html>",
        "text/html",
    )
    assert extraction.format is ReadFormat.HTML
    assert extraction.text == "Solubility was 12 g/L"
    assert "script" in extraction.note and "tags" in extraction.note


def test_plain_text_is_not_touched() -> None:
    """The one format where the text is the bytes, and the note says so."""
    body = b"line one\nline two\tindented\n"
    extraction = document_of(body, "text/plain")
    assert extraction.text == body.decode()
    assert extraction.note == ""


def test_text_that_is_not_utf8_says_which_bytes_were_replaced() -> None:
    """A mis-declared encoding is common, and the replacement is RAVEL's doing.

    Without the note, a reader would see the replacement character and read it
    as something the source printed.
    """
    extraction = document_of(b"caf\xe9 latte", "text/plain")
    assert "caf" in extraction.text and "latte" in extraction.text
    assert "not valid UTF-8" in extraction.note


def test_json_is_reindented_and_the_values_are_unchanged() -> None:
    """One line of JSON is a region nobody can read a region of."""
    extraction = document_of(b'{"a":1,"b":{"c":[1,2]}}', "application/json")
    assert extraction.format is ReadFormat.JSON
    assert extraction.text == '{\n  "a": 1,\n  "b": {\n    "c": [\n      1,\n      2\n    ]\n  }\n}'
    assert "unchanged" in extraction.note


def test_json_that_does_not_parse_is_shown_as_it_arrived() -> None:
    """A truncated response is not repaired into a document nobody wrote.

    The truncation is not RAVEL's to fix, and an object invented to make the
    text parse would be a value the source never carried.
    """
    extraction = document_of(b'{"a": 1, "b":', "application/json")
    assert extraction.text == '{"a": 1, "b":'
    assert "not valid JSON" in extraction.note


def test_xml_is_read_as_its_text_nodes() -> None:
    """Comments and processing instructions carry no text and are dropped."""
    extraction = document_of(
        b"<?xml version='1.0'?><entry><!-- a note --><title>UFF</title></entry>",
        "application/xml",
    )
    assert extraction.format is ReadFormat.XML
    assert extraction.text == "UFF"
    assert "comments" in extraction.note


def test_a_format_with_no_words_says_so_instead_of_returning_nothing() -> None:
    """A PNG is not an empty document, and the difference is the whole message.

    A research task that opened a figure instead of a paper needs to be told
    it has a figure; "no matches" would read as "the paper does not say that".
    """
    extraction = document_of(b"\x89PNG\r\n\x1a\n", "image/png")
    assert extraction.format is ReadFormat.OTHER
    assert extraction.text == ""
    assert extraction.extractable is False
    assert "image/png" in extraction.note


def test_a_document_larger_than_the_cap_is_cut_and_says_so() -> None:
    """The extraction cap bounds memory, and a reader is told it is not the end."""
    extraction = document_of(("word " * (MAX_EXTRACT_CHARS // 4)).encode(), "text/plain")
    assert extraction.chars == MAX_EXTRACT_CHARS
    assert extraction.truncated is True


# ── The arithmetic a bounded read is made of ────────────────────────────────


def test_a_window_is_clamped_to_the_text_rather_than_refused() -> None:
    """Asking past the end is a question with an answer: there is nothing there."""
    text = "0123456789"
    assert window(text, 8, 100) == deepread.Slice(text="89", start=8, end=10, total_chars=10)
    assert window(text, 100, 10).text == ""
    assert window(text, -5, 4).start == 0
    assert window(text, 0, 4).truncated is True
    assert window(text, 0, 10).truncated is False


def test_an_offset_a_search_reports_is_an_offset_a_read_can_use() -> None:
    """The property that makes the two tools a loop rather than two looks.

    A reader that finds a phrase and cannot get back to the paragraph it is in
    has to guess where the paragraph starts, and guessing is what a deep read
    exists to replace.
    """
    text = document_of(
        b"<p>Introduction.</p><p>The exchange-correlation functional was PBE.</p>", "text/html"
    ).text
    matches, total, exact = find(text, "functional was PBE")
    assert total == 1 and exact is True
    (match,) = matches
    assert window(text, match.offset, 30).text.startswith("functional was PBE")
    assert text[match.offset : match.offset + len("functional was PBE")] == "functional was PBE"


def test_a_search_is_literal_and_case_insensitive() -> None:
    """A research task knows the string it is looking for; near-misses are noise."""
    assert find("Force Field", "force field")[1] == 1
    assert find("F0rce Field", "force field")[1] == 0
    assert find("forcefields", "force field")[1] == 0


def test_a_search_that_spans_a_line_break_finds_nothing() -> None:
    """Literal, including the whitespace, and the tool says so to the model.

    A phrase read off a page and searched for comes back with the document's
    own line break missing, and a search that quietly matched across it would
    have to report an offset in a text the reader never sees. A miss is the
    right answer; the retry — fewer words — is the caller's.
    """
    text = "the current source, a diode\nwith ideality factor"
    assert find(text, "a diode with ideality")[1] == 0
    assert find(text, "with ideality factor")[1] == 1


def test_a_search_counts_past_the_matches_it_shows() -> None:
    """`total_matches` is the document's answer; the matches are a page of it."""
    matches, total, exact = find("a-a-a-a-a", "a-", max_matches=2)
    assert len(matches) == 2
    assert [match.index for match in matches] == [0, 1]
    assert [match.offset for match in matches] == [0, 2]
    assert total == 4
    assert exact is True


def test_a_count_stops_being_exact_when_it_stops_counting() -> None:
    """An inexact count says so rather than reporting the limit as the answer."""
    _, total, exact = find("a" * (COUNT_LIMIT + 10), "a", max_matches=1)
    assert total == COUNT_LIMIT
    assert exact is False


def test_a_match_inside_its_own_repetition_is_one_match() -> None:
    """Non-overlapping, so `total_matches` counts occurrences rather than offsets."""
    assert find("aaaa", "aa")[1] == 2


def test_folding_that_would_move_an_offset_falls_back_to_the_characters() -> None:
    """An offset has to name the character it was found at.

    `ß` folds to `ss` and `İ` to two code points, and a search over the folded
    text would report positions in a string the reader never sees. The search
    narrows to a case-sensitive one instead, which is a smaller answer about
    the right characters.
    """
    text = "Die Straße ist lang"
    assert find(text, "straße")[1] == 1
    assert find(text, "STRASSE")[1] == 0, "a case-sensitive fallback, not a wrong offset"
    matches, _, _ = find(text, "straße")
    assert text[matches[0].offset : matches[0].offset + 6] == "Straße"


def test_the_context_around_a_match_collapses_whitespace() -> None:
    """A window of newlines and indentation tells a reader nothing."""
    matches, _, _ = find("one\n\n\t two TARGET three", "TARGET", context_chars=40)
    assert matches[0].context == "one two TARGET three"


# ── Page ranges ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        ("3", (3,)),
        ("3-5", (3, 4, 5)),
        ("1,4", (1, 4)),
        ("1,4-5", (1, 4, 5)),
        (" 2 , 3 ", (2, 3)),
    ],
)
def test_a_page_range_is_read_in_the_forms_it_documents(
    spec: str, expected: tuple[int, ...]
) -> None:
    assert page_range(spec, 10) == expected


def test_naming_no_pages_reads_the_first_ones() -> None:
    """The default is a bounded read, not a whole document."""
    assert page_range(None, 100) == (1, 2, 3, 4, 5)
    assert page_range("", 2) == (1, 2)


@pytest.mark.parametrize(
    ("spec", "page_count", "because"),
    [
        ("0", 10, "pages are numbered from 1"),
        ("11", 10, "asks for page 11"),
        ("one", 10, "not a page range"),
        ("1-6", 10, "at most 5"),
        (",,", 10, "names no page"),
        ("5-3", 10, "runs upwards"),
    ],
)
def test_a_page_range_that_cannot_be_read_is_refused(
    spec: str, page_count: int, because: str
) -> None:
    """Refused rather than clamped, and the message says which pages exist.

    A caller that asked for six pages and silently got five would believe it
    had read the range, and the page it never saw would be missing from
    everything it concluded.
    """
    with pytest.raises(ValueError, match=because):
        page_range(spec, page_count)


def test_a_read_never_names_more_pages_than_the_cap() -> None:
    """The cap is the cap, however the range is written."""
    with pytest.raises(ValueError, match=str(MAX_PAGES_PER_READ)):
        page_range("1,2,3,4,5,6", 100)


# ── PDFs ────────────────────────────────────────────────────────────────────


def test_a_pdf_is_read_page_by_page() -> None:
    """The unit of a PDF is the page, and the text is the page's own words."""
    document = deepread.PdfDocument(
        a_pdf(["Methods: the force field was UFF.", "Convergence at 1e-6 eV."])
    )
    assert document.usable and document.page_count == 2
    pages = document.pages((1, 2))
    assert [page.text for page in pages.pages] == [
        "Methods: the force field was UFF.",
        "Convergence at 1e-6 eV.",
    ]
    assert pages.scanned is False


def test_only_the_pages_asked_for_are_extracted() -> None:
    """Bounded work rather than a bounded result.

    Extracting every page and slicing the text would read the whole paper, and
    reading the whole paper is what this reader exists not to do.
    """
    document = deepread.PdfDocument(a_pdf([f"page {number} text" for number in range(1, 21)]))
    assert len(document.pages((7,)).pages) == 1
    assert document.pages((7,)).pages[0].text == "page 7 text"


def test_a_pdf_with_no_text_layer_reports_a_scan_rather_than_empty_pages() -> None:
    """The pages exist and carry no words, and those are different findings.

    This is the case where a reader that produced text would be inventing it:
    there is nothing to read, and the only honest answers are "this is a scan"
    and "RAVEL does not do OCR".
    """
    document = deepread.PdfDocument(a_scanned_pdf(4))
    assert document.usable and document.page_count == 4
    pages = document.pages((1, 2, 3))
    assert pages.scanned is True
    assert [page.text for page in pages.pages] == ["", "", ""]


def test_a_scan_with_a_text_cover_page_is_not_called_a_scan() -> None:
    """The judgement is about the pages read, not about the file.

    A scanned paper usually has a text first page, and a reader that called the
    whole document a scan because one page was blank would be wrong about the
    part it could read.
    """
    document = deepread.PdfDocument(a_scanned_pdf(4, text_on=[1]))
    assert document.pages((1,)).scanned is False
    assert document.pages((2, 3)).scanned is True


def test_an_encrypted_pdf_is_reported_rather_than_opened() -> None:
    """RAVEL will not guess a password, and says so instead of failing."""
    document = deepread.PdfDocument(an_encrypted_pdf(["secret methods"]))
    assert document.usable is False
    assert "encrypted" in document.unusable
    assert document.pages((1,)).pages == ()


def test_a_file_that_is_not_a_pdf_is_reported_rather_than_raised() -> None:
    """A publisher that mislabels a file hands a research task a broken one.

    It reaches the agent as a sentence about the file, not as a traceback: the
    bytes are still stored and hashed, and the failure to read them is a fact
    about the source.
    """
    document = deepread.PdfDocument(b"%PDF-1.4 but not really a pdf at all")
    assert document.usable is False
    assert "did not parse as a PDF" in document.unusable


def test_asking_whether_a_pdf_has_text_samples_rather_than_reads_it_all() -> None:
    """A metadata call must not cost what reading the paper costs.

    The answer is about the pages sampled, and the cap is what makes it a
    cheap question — so a text layer that only begins on page five is reported
    as absent, which is the conservative direction for a scan check.
    """
    mostly_scan = deepread.PdfDocument(a_scanned_pdf(20, text_on=[5]))
    assert mostly_scan.has_text() is False
    assert mostly_scan.pages((5,)).scanned is False


# ── One rendering, not two ──────────────────────────────────────────────────


def test_the_excerpt_is_the_beginning_of_what_a_read_returns() -> None:
    """An excerpt and a read are the same text, so they cannot disagree.

    The excerpt is what a session is shown when it opens a source and the read
    is what it gets when it reads the source back; if the two were produced by
    different renderings, a passage could be checkable under one and not the
    other, and the snapshot would stop being the thing a claim is checked
    against.
    """
    body = ("<p>" + "Solubility was measured at 12 g/L. " * 60 + "</p>").encode()
    excerpt = excerpt_of(body, "text/html")
    assert excerpt
    assert len(excerpt) == EXCERPT_CHARS
    assert document_of(body, "text/html").text.startswith(excerpt)
