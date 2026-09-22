"""Documents built byte by byte, for the tests that need one to read.

The live suite reads real papers off the open Internet, and it is the only
place that proves RAVEL can reach one. What it cannot do is produce a *scanned*
PDF, an encrypted one, or a document with a known word at a known offset on
demand — and a deep reader has to be tested on exactly those, because they are
the cases where the honest answer is "there is no text here" and the failure
mode is producing text anyway.

So this module builds PDFs the way the format defines them: a header, a
cross-reference table, and one content stream per page. Nothing here is a mock
of a PDF or a stand-in for a parser — `pypdf` reads these files the same way it
reads a publisher's, which is why the same fixtures can be driven through the
real tool server and the real object store in `tests/integration`.

No third-party writer is used, deliberately. A writer would decide the layout,
and the point of several of these documents is the layout: no text layer at
all, a page that carries one phrase, a file that claims to be a PDF and is not.
"""

from __future__ import annotations

from io import BytesIO

#: What every PDF begins with. RAVEL reads it as the format's own declaration.
_HEADER = "%PDF-1.4\n"

#: What a page's content stream looks like when it shows one line. Helvetica is
#: one of the fourteen fonts every reader has, so this needs no font program.
_TEXT_STREAM = "BT /F1 12 Tf 72 720 Td ({text}) Tj ET"

#: A page with no text layer. A filled rectangle: the page exists, it has
#: content, and there is not a word on it — which is what a scan is.
_IMAGE_ONLY_STREAM = "0.5 g 72 500 400 200 re f"


def a_pdf(pages: list[str]) -> bytes:
    """A PDF with one page per string, each showing its string as a line.

    An empty string produces a page whose content stream draws nothing, which
    is how a page with no text layer is built — see `a_scanned_pdf` for the one
    that also draws something.
    """
    streams = [_TEXT_STREAM.format(text=_escape(text)) for text in pages]
    return _build(streams)


def a_scanned_pdf(pages: int, *, text_on: list[int] | None = None) -> bytes:
    """A PDF whose pages are drawings rather than text.

    `text_on` names one-based pages that carry a line of text anyway. A
    document that is mostly a scan and has a text cover page is the real shape
    of a scanned paper, and it is the case that makes "no extractable text" a
    claim about the pages rather than about the file.
    """
    with_text = set(text_on or [])
    streams = [
        _TEXT_STREAM.format(text=f"page {number}")
        if number in with_text
        else _IMAGE_ONLY_STREAM
        for number in range(1, pages + 1)
    ]
    return _build(streams)


def an_encrypted_pdf(pages: list[str], password: str = "secret") -> bytes:
    """A PDF that requires a password, so its pages cannot be read at all.

    Written with `pypdf` because an encrypted file's streams and key are its
    own format, and reimplementing RC4 to make a test fixture would be testing
    the reimplementation. What is being tested is RAVEL's answer to a document
    it cannot open, and that answer does not depend on who wrote the file.
    """
    from pypdf import PdfReader, PdfWriter

    reader = PdfReader(BytesIO(a_pdf(pages)))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(password)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def _escape(text: str) -> str:
    """Text as a PDF string literal, with the three characters that need it."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _build(streams: list[str]) -> bytes:
    """A PDF file around one content stream per page.

    The cross-reference table is written with real byte offsets because a
    reader that trusts them is the reader RAVEL uses; a file whose offsets were
    wrong would be tested by pypdf's recovery path rather than by its parser.
    """
    font = 3
    body: list[tuple[int, bytes]] = []
    kids: list[str] = []
    body.append((1, b"<< /Type /Catalog /Pages 2 0 R >>"))
    for index, stream in enumerate(streams):
        page_number = 4 + 2 * index
        content_number = page_number + 1
        kids.append(f"{page_number} 0 R")
        body.append(
            (
                page_number,
                (
                    f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> "
                    f"/Contents {content_number} 0 R >>"
                ).encode(),
            )
        )
        raw = stream.encode("latin-1")
        body.append(
            (
                content_number,
                f"<< /Length {len(raw)} >>\nstream\n".encode() + raw + b"\nendstream",
            )
        )
    body.append(
        (
            2,
            f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(streams)} >>".encode(),
        )
    )
    body.append((font, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"))

    out = bytearray(_HEADER.encode())
    offsets: dict[int, int] = {}
    for number, content in sorted(body):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode() + content + b"\nendobj\n"
    start = len(out)
    size = max(offsets) + 1
    out += f"xref\n0 {size}\n".encode() + b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {size} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode()
    return bytes(out)
