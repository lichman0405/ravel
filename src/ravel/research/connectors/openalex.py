"""OpenAlex: the scholarly graph, and the abstract Crossref often lacks.

OpenAlex indexes works, their venues, and their citation links. For RAVEL it
adds two things Crossref does not: coverage of records whose publisher never
deposited an abstract with Crossref, and a venue's *type* — journal, conference,
repository — which is a statement about where a work appeared rather than about
the work.

The abstract arrives as an inverted index: a map from each word to the
positions it occupies. Reassembling it is not generation — every word and its
place come from the record, and the result is what the depositor wrote. When
the positions have gaps, the gaps are left as gaps rather than closed up,
because silently joining two halves of a sentence would produce a quotation
that was never made.
"""

from __future__ import annotations

from typing import Any

from ravel.research.connectors.base import ConnectorError, HttpConnector
from ravel.research.leads import Lead

API = "https://api.openalex.org/works"

#: What RAVEL records, in OpenAlex's own vocabulary.
_SELECT = ",".join(
    [
        "id",
        "doi",
        "title",
        "display_name",
        "publication_year",
        "publication_date",
        "type",
        "primary_location",
        "abstract_inverted_index",
        "authorships",
        "referenced_works_count",
        "cited_by_count",
    ]
)

#: OpenAlex's work types, mapped into the vocabulary `tiers` classifies by.
#: OpenAlex says `article` where Crossref says `journal-article`; the tier rules
#: are written once, so the names are translated here rather than duplicated
#: there. An unmapped type is passed through unchanged and simply matches no
#: hint, which leaves the decision to the domain.
_TYPE_MAP = {
    "article": "journal-article",
    "preprint": "posted-content",
    "book-chapter": "book-chapter",
    "book": "book",
    "dataset": "dataset",
    "report": "report",
    "standard": "standard",
    "peer-review": "peer-review",
    "proceedings-article": "proceedings-article",
    "dissertation": "dissertation",
}


class OpenAlexConnector(HttpConnector):
    """Search and identifier lookup against the OpenAlex API."""

    name = "openalex"

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        """Works matching a free-text query.

        Raises:
            ConnectorError: OpenAlex was unreachable or refused.
        """
        payload = self.get_json(
            API,
            params={
                "search": query,
                "per-page": max(1, min(limit, 100)),
                "select": _SELECT,
                "mailto": self.contact,
            },
        )
        results = payload.get("results") if isinstance(payload, dict) else None
        return [self._lead(item) for item in results or [] if isinstance(item, dict)]

    def lookup(self, identifier: str) -> Lead | None:
        """The record for one DOI or OpenAlex id.

        Returns None when OpenAlex has no such record.

        Raises:
            ConnectorError: OpenAlex was unreachable or refused.
        """
        key = identifier.strip()
        for prefix in ("https://doi.org/", "doi:", "https://openalex.org/"):
            key = key.removeprefix(prefix)
        if not key:
            return None
        # A DOI and an OpenAlex id are addressed differently and a caller may
        # hold either, so both are tried. Only "OpenAlex has no such record"
        # moves on to the second; anything else is a real failure and is raised
        # rather than being turned into a confident "not found".
        for path in (f"https://doi.org/{key}", key):
            try:
                payload = self.get_json(f"{API}/{path}", params={"mailto": self.contact})
            except ConnectorError as exc:
                if "no such record" in exc.detail:
                    continue
                raise
            if isinstance(payload, dict):
                return self._lead(payload)
        return None

    def _lead(self, item: dict[str, Any]) -> Lead:
        doi = str(item.get("doi") or "").removeprefix("https://doi.org/") or None
        declared = str(item.get("type") or "")
        return Lead(
            url=str(item.get("doi") or item.get("id") or "") or API,
            title=str(item.get("title") or item.get("display_name") or ""),
            snippet=_abstract(item.get("abstract_inverted_index")),
            provider=self.name,
            published=(
                str(item.get("publication_date") or item.get("publication_year") or "") or None
            ),
            identifiers={
                **({"doi": doi} if doi else {}),
                **({"declared_type": _TYPE_MAP.get(declared, declared)} if declared else {}),
                **({"openalex_id": str(item["id"])} if item.get("id") else {}),
            },
        )


def _abstract(inverted: Any) -> str:
    """Rebuild an abstract from OpenAlex's inverted index.

    The index maps each word to the positions it holds. Reconstruction places
    each word at its own position, which is exact: the words and their order
    are the depositor's.

    Two details are deliberate. A repeated word appears at several positions
    and is placed at all of them. A gap — positions nothing claims — is left as
    a gap in the output rather than closed, so that a reader can see the
    abstract is incomplete instead of reading a sentence that was never written.
    """
    if not isinstance(inverted, dict) or not inverted:
        return ""
    positioned: dict[int, str] = {}
    for word, places in inverted.items():
        if not isinstance(places, list):
            continue
        for place in places:
            if isinstance(place, int) and place not in positioned:
                positioned[place] = str(word)
    if not positioned:
        return ""
    highest = max(positioned)
    words = [positioned.get(index, "") for index in range(highest + 1)]
    return " ".join(words).strip()[:4000]
