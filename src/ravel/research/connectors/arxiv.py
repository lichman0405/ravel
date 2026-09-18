"""arXiv: preprints, in the form their authors deposited them.

arXiv answers with Atom XML rather than JSON, and the difference is worth
stating: an arXiv entry is a record of a *deposit*, not of a publication. It
carries the abstract the authors submitted, the version, and — when the work
was later published — a journal reference and a DOI as separate fields. RAVEL
keeps them separate too: a lead from here is a preprint lead, and the published
version is a different source at a different URL, reached through the DOI.

The API asks callers to make no more than one request every three seconds.
RAVEL complies, because a research runtime that hammers a free service is a
research runtime that gets the lab's address blocked. The wait is applied
between requests from the same connector and is skipped for the first one.
"""

from __future__ import annotations

import time
from typing import Any

import lxml.etree as etree

from ravel.research.connectors.base import ConnectorError, HttpConnector
from ravel.research.leads import Lead

API = "https://export.arxiv.org/api/query"

#: The interval arXiv's API documentation asks for, in seconds.
MINIMUM_INTERVAL_SECONDS = 3.0

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"

#: A parser configured for untrusted input. All three flags matter and none is
#: the default in every case:
#:
#: - `resolve_entities=False` stops entity expansion, which is what makes a
#:   small document able to expand into an arbitrarily large one.
#: - `load_dtd=False` means a document type definition is not processed at all,
#:   so entity definitions inside one never take effect.
#: - `no_network=True` stops the parser fetching anything a document points at,
#:   so a crafted response cannot make RAVEL issue requests of the sender's
#:   choosing from inside the lab's network.
#:
#: This is a connector reading a response from the open internet. The parser is
#: configured for that, rather than for the case where the sender is trusted.
_PARSER = etree.XMLParser(
    resolve_entities=False,
    load_dtd=False,
    no_network=True,
    huge_tree=False,
    recover=False,
)


class ArxivConnector(HttpConnector):
    """Search and identifier lookup against the arXiv Atom API."""

    name = "arxiv"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._last_request_at: float | None = None

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        """Preprints matching a query.

        The query is passed as an `all:` field search, which is arXiv's own
        broadest field, rather than being rewritten into its field syntax:
        guessing that a term is an author or a category would narrow the search
        using RAVEL's opinion instead of the caller's words.

        Raises:
            ConnectorError: arXiv was unreachable, refused, or answered with
                something that is not Atom.
        """
        return self._query(
            {
                "search_query": f"all:{query}",
                "max_results": max(1, min(limit, 100)),
            }
        )

    def lookup(self, identifier: str) -> Lead | None:
        """The record for one arXiv id.

        Returns None when arXiv has no such preprint.

        Raises:
            ConnectorError: arXiv was unreachable or refused.
        """
        key = identifier.strip()
        for prefix in ("https://arxiv.org/abs/", "arxiv.org/abs/", "arXiv:", "arxiv:"):
            key = key.removeprefix(prefix)
        key = key.split("v")[0] if key[:1].isdigit() else key
        if not key:
            return None
        leads = self._query({"id_list": key, "max_results": 1})
        return leads[0] if leads else None

    def _query(self, params: dict[str, Any]) -> list[Lead]:
        self._wait()
        text = self.get_text(API, params=params)
        return self._parse(text)

    def _wait(self) -> None:
        """Honour arXiv's requested interval between requests."""
        now = time.monotonic()
        if self._last_request_at is not None:
            remaining = MINIMUM_INTERVAL_SECONDS - (now - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def _parse(self, text: str) -> list[Lead]:
        try:
            root = etree.fromstring(text.encode("utf-8"), parser=_PARSER)
        except etree.XMLSyntaxError as exc:
            raise ConnectorError(self.name, f"the response was not Atom XML: {exc}") from exc
        return [self._lead(entry) for entry in root.findall(f"{_ATOM}entry")]

    def _lead(self, entry: etree._Element) -> Lead:
        # arXiv's feed writes its own links as `http://`. The scheme is
        # rewritten to https because that is the URL RAVEL will actually read
        # and the one worth recording: storing the plaintext form would put a
        # URL in the Evidence Ledger that RAVEL never fetched and would not
        # choose to.
        identifier = _text(entry, f"{_ATOM}id").replace("http://", "https://", 1)
        arxiv_id = identifier.rsplit("/abs/", 1)[-1] if "/abs/" in identifier else identifier
        doi = _text(entry, f"{_ARXIV}doi")
        journal = _text(entry, f"{_ARXIV}journal_ref")
        return Lead(
            url=identifier or f"https://arxiv.org/abs/{arxiv_id}",
            title=" ".join(_text(entry, f"{_ATOM}title").split()),
            snippet=" ".join(_text(entry, f"{_ATOM}summary").split())[:4000],
            provider=self.name,
            published=_text(entry, f"{_ATOM}published") or None,
            identifiers={
                **({"arxiv_id": arxiv_id} if arxiv_id else {}),
                **({"doi": doi} if doi else {}),
                # arXiv is a preprint server, so what it holds is by definition
                # not yet the version of record. A journal reference says the
                # work was published *somewhere else*; it does not make this
                # record the published article.
                "declared_type": "posted-content",
                **({"journal_ref": journal} if journal else {}),
            },
        )


def _text(entry: etree._Element, path: str) -> str:
    element = entry.find(path)
    return (element.text or "").strip() if element is not None else ""
