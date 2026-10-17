"""Reading a source RAVEL already has, a region at a time.

Phase 9 built a gateway that opens URLs and writes down what came back, and
`KNOWN_LIMITATIONS.md` L-25 recorded the hole it left: a source is fetched for
real, hashed, tiered and snapshotted, and what any session ever sees of it is a
six-hundred-character excerpt produced only for HTML. RAVEL could prove it read
a page and could not read the page.

This module is the reading half. It answers one question — *what does this
document say, in the region I asked for* — for the formats a research task
actually meets, and it is deliberately a pure function of bytes:

- **Nothing here fetches.** It is handed a body and a media type. Retrieval is
  `fetching`'s job and provenance is the Evidence Ledger's; a reader that could
  reach the network would be a second way for text to arrive that no source row
  describes.
- **Nothing here is guessed.** A scanned PDF, an encrypted PDF, a spreadsheet
  and a malformed file each produce an empty text and a sentence saying which
  one it is. There is no OCR, no model call, and no completion of text that was
  not in the bytes.
- **Text is verbatim where it can be.** HTML and XML have their tags removed
  because a tag is markup rather than content, and JSON is re-indented because
  one line of eight megabytes is not readable; the `note` on every extraction
  says what was done, so a reader knows which text is the document's words and
  which is RAVEL's rendering of them. Plain text is not touched at all.

The offsets this module returns are offsets into the text it returns, so a
match found by `find` can be handed straight back to `window` — which is what
makes a bounded read a *loop* an agent can drive rather than a single look.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from io import BytesIO

from pypdf import PdfReader

__all__ = [
    "COUNT_LIMIT",
    "DEFAULT_CONTEXT_CHARS",
    "DEFAULT_MATCHES",
    "DEFAULT_READ_CHARS",
    "DEFAULT_SEARCH_PAGES",
    "MAX_CONTEXT_CHARS",
    "MAX_MATCHES",
    "MAX_PAGES_PER_READ",
    "MAX_READ_CHARS",
    "MAX_SEARCH_PAGES",
    "Extraction",
    "Match",
    "Pages",
    "PdfDocument",
    "PdfPage",
    "ReadFormat",
    "Slice",
    "context_around",
    "document_of",
    "find",
    "format_of",
    "html_text",
    "page_numbers",
    "page_range",
    "strip_tags",
    "window",
    "xml_text",
]

#: How much text a read returns when the caller does not say. Sized for a
#: reader that is looking for a number, a method or a sentence — not for one
#: that wants the paper. The rest is always one more call away.
DEFAULT_READ_CHARS = 6000

#: The most one read will return whatever the caller asks for. A bounded read
#: that a caller can raise without limit is not bounded, and the whole point of
#: this module is that a session's context stays a size the session chose.
MAX_READ_CHARS = 24000

#: The most text an extraction will produce for one document, whatever the
#: document's size. A cap on the read would be no cap at all if producing the
#: text had already cost more memory than the body did.
MAX_EXTRACT_CHARS = 2_000_000

#: Pages one PDF read will extract. Extracting a page is the expensive part of
#: reading a PDF, and this is what keeps a read of a five-hundred-page document
#: the same cost as a read of a five-page one.
MAX_PAGES_PER_READ = 5

#: Pages a PDF search walks when the caller names no range.
DEFAULT_SEARCH_PAGES = 40

#: The most pages a PDF search will walk in one call.
MAX_SEARCH_PAGES = 200

#: Matches a search returns, and the characters of context around each. The
#: context is what makes a match usable — an offset alone says a word is on
#: page nine, and a window says what the sentence around it claims.
DEFAULT_MATCHES = 10
MAX_MATCHES = 50
DEFAULT_CONTEXT_CHARS = 240
MAX_CONTEXT_CHARS = 1000

#: How many occurrences are counted before the count stops being exact. A
#: common word in a long document has more matches than any reader wants, and
#: counting them all costs a pass over text nobody will look at.
COUNT_LIMIT = 5000

#: Pages `has_text` samples before concluding a document carries no text.
PROBE_PAGES = 3

#: The media types RAVEL reads as HTML, and as XML. Named rather than matched
#: by prefix, because `application/xhtml+xml` is HTML that ends in `+xml` and
#: the two rules would otherwise disagree about it.
_HTML_MEDIA_TYPES = frozenset({"text/html", "application/xhtml+xml"})
_XML_MEDIA_TYPES = frozenset({"application/xml", "text/xml"})
_JSON_MEDIA_TYPES = frozenset({"application/json", "text/json"})
_PDF_MEDIA_TYPES = frozenset({"application/pdf"})

#: How a PDF identifies itself. The header is part of the format, so it is
#: evidence about the bytes rather than a guess about them.
_PDF_MAGIC = b"%PDF-"


class ReadFormat(StrEnum):
    """What kind of document a body is, as far as reading it goes."""

    HTML = "html"
    TEXT = "text"
    JSON = "json"
    XML = "xml"
    PDF = "pdf"
    #: A format RAVEL can store and hash and cannot read: an image, an archive,
    #: a spreadsheet, a binary of any kind. Named rather than refused at the
    #: door, because "RAVEL has these bytes and there are no words in them" is
    #: a finding a research task needs to be able to record.
    OTHER = "other"


def format_of(body: bytes, media_type: str | None) -> ReadFormat:
    """How a body will be read, from its media type and then from its bytes.

    The media type decides, with one exception: publishers serve PDFs as
    `application/octet-stream` often enough that a leading `%PDF-` is worth
    more than the header, and a research task that could not read a paper
    because a web server was vague about it would be refusing for no reason.
    The header is part of the PDF format rather than a guess about it, so where
    the two disagree the bytes win — and whatever `document_of` extracts says
    which one it went with.
    """
    if body.startswith(_PDF_MAGIC):
        return ReadFormat.PDF
    kind = (media_type or "").split(";", 1)[0].strip().lower()
    if kind in _PDF_MEDIA_TYPES:
        return ReadFormat.PDF
    if kind in _JSON_MEDIA_TYPES or kind.endswith("+json"):
        return ReadFormat.JSON
    # Before the `+xml` catch-all, because `application/xhtml+xml` is HTML that
    # happens to end in it, and the two rules would otherwise disagree.
    if kind in _HTML_MEDIA_TYPES:
        return ReadFormat.HTML
    if kind in _XML_MEDIA_TYPES or kind.endswith("+xml"):
        return ReadFormat.XML
    if kind.startswith("text/"):
        return ReadFormat.TEXT
    return ReadFormat.OTHER


@dataclass(frozen=True, slots=True)
class Extraction:
    """The text of one document, and what producing it cost.

    `truncated` is about the *document*, not about a read: it says RAVEL
    stopped extracting at `MAX_EXTRACT_CHARS` and there is more text in the
    bytes than this object holds. A caller that sees it knows the last
    character is not the end of the file.
    """

    format: ReadFormat
    text: str = ""
    note: str = ""
    extractable: bool = True
    truncated: bool = False

    @property
    def chars(self) -> int:
        """How long the extracted text is."""
        return len(self.text)


@dataclass(frozen=True, slots=True)
class Slice:
    """A bounded region of a document's text, with where it came from."""

    text: str
    start: int
    end: int
    total_chars: int

    @property
    def truncated(self) -> bool:
        """Whether there is text after this region."""
        return self.end < self.total_chars


@dataclass(frozen=True, slots=True)
class Match:
    """One occurrence of a query, and the words around it."""

    index: int
    offset: int
    context: str
    #: The page the match is on, for a document that has pages.
    page: int | None = None


def window(text: str, start: int, length: int) -> Slice:
    """A region of `text`, clamped to what exists.

    Clamped rather than refused. A caller that asks for more than there is has
    asked a question with an answer — the rest of the document — and a read
    that errored instead would make driving a long document a matter of
    guessing its length. The returned `start`, `end` and `total_chars` say what
    was actually returned.
    """
    total = len(text)
    first = max(0, min(start, total))
    last = max(first, min(first + max(0, length), total))
    return Slice(text=text[first:last], start=first, end=last, total_chars=total)


def find(
    text: str,
    query: str,
    *,
    max_matches: int = DEFAULT_MATCHES,
    context_chars: int = DEFAULT_CONTEXT_CHARS,
) -> tuple[tuple[Match, ...], int, bool]:
    """Every occurrence of `query` in `text`, and the count of them.

    A literal search, case-insensitive where the language's own case rules
    allow it. Not a regular expression, not a fuzzy match, and not a ranking: a
    research task looking for a force field in a methods section is looking for
    a string it already knows, and a matcher that returned near-misses would be
    a matcher whose results have to be checked against the source — which is
    the work a search is for.

    Returns the matches to show, the number found, and whether that number is
    exact. Non-overlapping: a query that occurs inside its own repetition is
    one occurrence.
    """
    if not query:
        return (), 0, True
    haystack, needle = _folded(text, query)
    span = len(query)
    offsets = _offsets(haystack, needle)
    shown: list[Match] = []
    for index, offset in enumerate(offsets):
        if len(shown) >= max(0, max_matches):
            break
        shown.append(
            Match(
                index=index,
                offset=offset,
                context=context_around(text, offset, span, context_chars),
            )
        )
    return tuple(shown), len(offsets), len(offsets) < COUNT_LIMIT


def context_around(text: str, offset: int, span: int, context_chars: int) -> str:
    """The words around one match, with whitespace collapsed.

    Collapsed because the text around a hit in an HTML page is usually newlines
    and indentation, and a window of whitespace tells a reader nothing about
    whether the match is the one they wanted.
    """
    half = max(0, context_chars) // 2
    first = max(0, offset - half)
    last = min(len(text), offset + span + half)
    return " ".join(text[first:last].split())


def _folded(text: str, query: str) -> tuple[str, str]:
    """The text and query to scan, folded as far as offsets survive it.

    Case folding can change a string's length — `ß` becomes `ss`, `İ` becomes
    two code points — and an offset found in a string of a different length
    does not name a position in the original. So folding is tried from the
    gentler rule to the stronger one and kept only while the length holds, and
    when neither holds the search runs over the text as it is. A
    case-sensitive answer about the right characters beats a case-insensitive
    one about the wrong ones: the caller can read the region it points at.
    """
    for fold in (str.lower, str.casefold):
        haystack, needle = fold(text), fold(query)
        if len(haystack) == len(text) and len(needle) == len(query):
            return haystack, needle
    return text, query


def _offsets(text: str, needle: str) -> list[int]:
    """Where `needle` occurs in `text`, non-overlapping, up to the count limit."""
    found: list[int] = []
    start = 0
    while len(found) < COUNT_LIMIT:
        at = text.find(needle, start)
        if at == -1:
            break
        found.append(at)
        start = at + len(needle)
    return found


def document_of(body: bytes, media_type: str | None) -> Extraction:
    """The text of a non-PDF document, as far as RAVEL can read it.

    Every branch says in `note` what transformation it applied, because a
    reader has to be able to tell the document's words from RAVEL's rendering
    of them. Plain text is passed through untouched and says so; the formats
    whose text is markup say which markup was removed.
    """
    kind = format_of(body, media_type)
    if kind is ReadFormat.PDF:
        return Extraction(
            format=kind,
            note=_pdf_note(body, media_type),
        )
    if kind is ReadFormat.OTHER:
        return Extraction(
            format=kind,
            extractable=False,
            note=_unreadable_note(media_type),
        )
    if kind is ReadFormat.TEXT:
        text, note = _decode(body)
        return _extraction(kind, text, note)
    if kind is ReadFormat.HTML:
        text, _ = _decode(body)
        return _extraction(
            kind, html_text(text), "tags, script and style removed; whitespace collapsed"
        )
    if kind is ReadFormat.XML:
        text, _ = _decode(body)
        return _extraction(
            kind,
            xml_text(text),
            "tags, comments and processing instructions removed; whitespace collapsed",
        )
    text, _ = _decode(body)
    return _extraction(kind, *_pretty_json(text))


def _pdf_note(body: bytes, media_type: str | None) -> str:
    """What to say about a PDF, and who said it was one."""
    declared = (media_type or "").split(";", 1)[0].strip().lower()
    note = "this is a PDF, which is read by page rather than by character offset"
    if body.startswith(_PDF_MAGIC) and declared not in _PDF_MEDIA_TYPES:
        note = (
            f"the bytes begin with %PDF- and the server called them "
            f"{media_type or 'nothing at all'}; " + note
        )
    return note


def _unreadable_note(media_type: str | None) -> str:
    return (
        f"RAVEL reads text out of HTML, plain text, JSON, XML and PDF, and this is "
        f"{media_type or 'of an unstated type'}. The bytes are stored and hashed — a "
        "source can be registered from them — and there is no text in them for RAVEL "
        "to quote."
    )


def _extraction(kind: ReadFormat, text: str, note: str) -> Extraction:
    """One extraction, cut to the cap, saying whether it was cut.

    The cut is applied in one place rather than in each branch, so that every
    format reports truncation the same way — a document whose text was cut is a
    document a reader is looking at part of, whatever it is written in.
    """
    if len(text) <= MAX_EXTRACT_CHARS:
        return Extraction(format=kind, text=text, note=note)
    return Extraction(format=kind, text=text[:MAX_EXTRACT_CHARS], note=note, truncated=True)


def _decode(body: bytes) -> tuple[str, str]:
    """Bytes as text, saying so when the bytes were not text.

    `errors="replace"` rather than a refusal: a page whose encoding is
    mis-declared is common, most of it is still readable, and the note is what
    keeps the replacement characters from being read as the source's own.
    """
    try:
        return body.decode("utf-8"), ""
    except UnicodeDecodeError:
        return (
            body.decode("utf-8", errors="replace"),
            "not valid UTF-8; undecodable bytes were replaced, so the text is not "
            "character-for-character what the source sent",
        )


def strip_tags(text: str) -> str:
    """Text with everything between angle brackets removed.

    Deliberately not a parser. A page that is malformed, that nests a comment
    inside a script, or that is not markup at all still produces the text
    around its tags rather than an exception, and the worst case is text that
    reads oddly — which is visible to the reader, unlike a repair that silently
    invents an order the document did not have.
    """
    out: list[str] = []
    inside = False
    for character in text:
        if character == "<":
            inside = True
        elif character == ">":
            inside = False
            out.append(" ")
        elif not inside:
            out.append(character)
    return "".join(out)


def html_text(markup: str) -> str:
    """The readable text of an HTML document.

    The same transformation `fetching.excerpt_of` has always applied, extracted
    here so that there is one implementation of it: an excerpt is a prefix of
    this text, and a match `search_source` finds can be read back with
    `read_source` at the offset it reported.
    """
    text = markup
    for tag in ("script", "style", "noscript"):
        while (start := text.find(f"<{tag}")) != -1:
            end = text.find(f"</{tag}>", start)
            text = text[:start] + (" " if end == -1 else text[end + len(tag) + 3 :])
    return " ".join(strip_tags(text).split())


def xml_text(markup: str) -> str:
    """The text nodes of an XML document, with the markup that carries none."""
    text = re.sub(r"<!--.*?-->", " ", markup, flags=re.DOTALL)
    text = re.sub(r"<\?.*?\?>", " ", text, flags=re.DOTALL)
    return " ".join(strip_tags(text).split())


def _pretty_json(text: str) -> tuple[str, str]:
    """JSON re-indented, or the raw text when it does not parse.

    Re-indented because a JSON document on the wire is one line, and a
    character window over one line is a window over nothing a reader can use.
    The value is preserved exactly — the same object, the same order, the same
    numbers — and when it does not parse the text is passed through as it
    arrived, with a note saying so, rather than being repaired into something
    the source never said.
    """
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return text, f"not valid JSON ({exc}); shown as it arrived, unformatted"
    return json.dumps(parsed, indent=2, ensure_ascii=False), "re-indented; the values are unchanged"


# ── PDFs ────────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class PdfPage:
    """One page of a PDF, and whether there were words on it."""

    number: int
    text: str = ""
    note: str = ""

    @property
    def chars(self) -> int:
        return len(self.text)


@dataclass(frozen=True, slots=True)
class Pages:
    """Pages extracted from one PDF, and what the document itself looks like."""

    requested: tuple[int, ...]
    pages: tuple[PdfPage, ...] = ()
    page_count: int = 0
    #: Empty when the PDF was readable. Otherwise, why it was not — encrypted,
    #: malformed, or a string pypdf raised that RAVEL does not interpret.
    unusable: str = ""

    @property
    def with_text(self) -> tuple[PdfPage, ...]:
        return tuple(page for page in self.pages if page.text.strip())

    @property
    def scanned(self) -> bool:
        """Whether these pages look like a scan.

        True when pages were extracted and none of them carried text. That is
        what a page of page-images looks like: the PDF is valid, the pages
        exist, and there is no text layer for RAVEL to read. Named rather than
        reported as an empty string, because "this page has no words" and "this
        document is a scan, which RAVEL cannot read" call for different next
        steps from a researcher.
        """
        return bool(self.pages) and not self.with_text


@dataclass
class PdfDocument:
    """A parsed PDF, whose pages are extracted one call at a time.

    The reader is built once, when the bytes are handed over, and each read
    extracts only the pages it was asked for. That is the difference between a
    bounded read and a bounded *result*: extracting all five hundred pages and
    then slicing the text would cost the same as reading the whole paper, which
    is the thing this module exists to avoid.

    Nothing here raises for a file that is not a PDF. A research task that
    opened something a publisher mislabelled needs to be told what it has, and
    an exception would reach it as a traceback rather than as an answer.
    """

    body: bytes
    page_count: int = 0
    unusable: str = ""
    _reader: PdfReader | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            reader = PdfReader(BytesIO(self.body), strict=False)
        except Exception as exc:  # a PDF is untrusted input; pypdf's failures are not a closed set
            self.unusable = f"this file did not parse as a PDF: {type(exc).__name__}: {exc}"
            return
        if reader.is_encrypted and not _decrypts(reader):
            self.unusable = (
                "this PDF is encrypted and RAVEL will not guess a password; the bytes "
                "are stored and hashed, and no page can be read"
            )
            return
        try:
            self.page_count = len(reader.pages)
        except Exception as exc:  # encrypted-but-openable files fail here too
            self.unusable = f"this PDF's pages could not be enumerated: {type(exc).__name__}: {exc}"
            return
        self._reader = reader

    @property
    def usable(self) -> bool:
        """Whether pages can be read at all."""
        return self._reader is not None and not self.unusable

    def pages(self, numbers: tuple[int, ...]) -> Pages:
        """Extract the pages named by one-based numbers.

        A page pypdf cannot extract is reported as a page with no text and a
        note saying the extraction failed, rather than failing the read: the
        other pages of the same document are still readable, and a reader that
        lost four good pages because the fifth had a broken font would be a
        reader that failed for the wrong reason.
        """
        if not self.usable:
            return Pages(requested=numbers, page_count=self.page_count, unusable=self.unusable)
        extracted: list[PdfPage] = []
        for number in numbers:
            extracted.append(self._page(number))
        return Pages(requested=numbers, pages=tuple(extracted), page_count=self.page_count)

    def has_text(self, probe: int = PROBE_PAGES) -> bool:
        """Whether a sample of the document carries any text at all.

        Sampling rather than reading everything: this answers "is this a scan"
        for a metadata call, and reading five hundred pages to answer it would
        make asking the question as expensive as reading the paper. The answer
        is therefore about the pages sampled, and the caller is told which.
        """
        if not self.usable or not self.page_count:
            return False
        return bool(self.pages(tuple(range(1, min(self.page_count, probe) + 1))).with_text)

    def _page(self, number: int) -> PdfPage:
        if self._reader is None:  # unreachable while `usable` guards the call
            return PdfPage(number=number, note="this document could not be opened")
        try:
            page = self._reader.pages[number - 1]
            return PdfPage(number=number, text=page.extract_text() or "")
        except Exception as exc:  # one bad page must not lose the good ones
            return PdfPage(
                number=number,
                note=f"this page could not be extracted: {type(exc).__name__}: {exc}",
            )


def _decrypts(reader: PdfReader) -> bool:
    """Whether an encrypted PDF opens with an empty password.

    A PDF that is only owner-encrypted — the common case for a paper a
    publisher marks "no copying" — opens with no password at all, and refusing
    to try would call a readable document unreadable. A user password is a
    different thing, and RAVEL does not guess at one.
    """
    try:
        return bool(reader.decrypt(""))
    except Exception:
        return False


def page_range(
    spec: str | None, page_count: int, *, most: int = MAX_PAGES_PER_READ
) -> tuple[int, ...]:
    """The page numbers a caller asked for, one-based and in order.

    Accepts `3`, `3-5`, and `1,4,9-11`. Refused rather than clamped when it
    names more pages than one read will extract, because a caller that asked
    for forty pages and silently got five would believe it had read the range.

    Raises:
        ValueError: The specification is not one of those forms, the range is
            backwards, a page does not exist in this document, or more pages
            were named than one read will extract.
    """
    if not page_count:
        raise ValueError("this PDF has no readable pages, so there is no page to read")
    if spec is None or not spec.strip():
        return tuple(range(1, min(page_count, most) + 1))

    numbers: list[int] = []
    for part in spec.replace(" ", "").split(","):
        if not part:
            continue
        first, _, last = part.partition("-")
        try:
            start = int(first)
            end = int(last) if last else start
        except ValueError as exc:
            raise ValueError(
                f"{spec!r} is not a page range; write a page (3), a range (3-5), or a "
                "list of either (1,4,9-11)"
            ) from exc
        if start < 1 or end < start:
            raise ValueError(
                f"{part!r} is not a range in this document: pages are numbered from 1 "
                "and a range runs upwards"
            )
        if end > page_count:
            raise ValueError(
                f"this PDF has {page_count} pages, and {part!r} asks for page {end}"
            )
        numbers.extend(range(start, end + 1))

    if not numbers:
        raise ValueError(f"{spec!r} names no page; write a page, a range, or a list of either")
    if len(numbers) > most:
        raise ValueError(
            f"{spec!r} names {len(numbers)} pages and one read extracts at most {most}; "
            "read the pages you need, in ranges of that size"
        )
    return tuple(numbers)


def page_numbers(spec: str | None, page_count: int, *, most: int) -> tuple[int, ...]:
    """A page selection for a search, which may be far wider than a read.

    The same grammar as `page_range`, with its own cap: a search walks pages
    without returning their text, so the number of pages it may cover is set by
    how long a scan should take rather than by how much text a reader can hold.
    """
    return page_range(spec, page_count, most=most)
