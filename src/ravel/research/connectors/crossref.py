"""Crossref: the DOI registration agency's own record of a work.

Crossref is the strongest structured source RAVEL has, for one reason: a
depositor registers a DOI by handing Crossref the metadata itself. The type, the
container, the publication date, and the identifiers on a Crossref record are
therefore statements by the publisher, not inferences from a title.

What Crossref does *not* provide is the article. A record here is a lead, and
the gateway opens the DOI to find out whether RAVEL can read the work itself —
which for most publishers it cannot, and the retrieval says so.
"""

from __future__ import annotations

from typing import Any

from ravel.research.connectors.base import ConnectorError, HttpConnector
from ravel.research.leads import Lead

API = "https://api.crossref.org/works"

#: Fields RAVEL actually records. Asking Crossref for a narrow `select` keeps
#: the polite pool's bandwidth reasonable and the parsing honest: everything
#: read here is a field the depositor supplied.
_SELECT = ",".join(
    [
        "DOI",
        "URL",
        "type",
        "title",
        "container-title",
        "publisher",
        "issued",
        "published",
        "abstract",
        "author",
        "subject",
        "is-referenced-by-count",
    ]
)


class CrossrefConnector(HttpConnector):
    """Search and identifier lookup against the Crossref REST API."""

    name = "crossref"

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        """Works matching a bibliographic query.

        Raises:
            ConnectorError: Crossref was unreachable or refused.
        """
        payload = self.get_json(
            API,
            params={
                "query.bibliographic": query,
                "rows": max(1, min(limit, 100)),
                "select": _SELECT,
                "mailto": self.contact,
            },
        )
        return [self._lead(item) for item in self._items(payload)]

    def lookup(self, identifier: str) -> Lead | None:
        """The record for one DOI.

        Returns None when Crossref has no such DOI, which is a statement about
        the identifier rather than a failure.

        Raises:
            ConnectorError: Crossref was unreachable or refused.
        """
        doi = identifier.removeprefix("doi:").removeprefix("https://doi.org/").strip()
        if not doi:
            return None
        try:
            payload = self.get_json(f"{API}/{doi}", params={"mailto": self.contact})
        except ConnectorError as exc:
            if "no such record" in exc.detail:
                return None
            raise
        message = payload.get("message")
        return self._lead(message) if isinstance(message, dict) else None

    @staticmethod
    def _items(payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, dict):
            return []
        message = payload.get("message")
        items = message.get("items") if isinstance(message, dict) else None
        return [item for item in items or [] if isinstance(item, dict)]

    def _lead(self, item: dict[str, Any]) -> Lead:
        """A lead from one Crossref item.

        The URL is the DOI's resolution URL, not the publisher's landing page:
        the lead should point at the identifier so that opening it exercises
        the resolution the reader would exercise.
        """
        doi = _first(item.get("DOI")) or ""
        return Lead(
            url=str(item.get("URL") or (f"https://doi.org/{doi}" if doi else "")) or API,
            title=_first(item.get("title")) or "",
            snippet=_abstract(item),
            provider=self.name,
            published=_issued(item),
            identifiers={
                **({"doi": doi} if doi else {}),
                **(
                    {"declared_type": str(item["type"])}
                    if item.get("type")
                    else {}
                ),
            },
        )


def _first(value: Any) -> str | None:
    """The first string of a Crossref list-valued field.

    Crossref writes most scalar fields as one-element lists. Taking the first
    is right; taking the whole list would put a Python repr in a citation.
    """
    if isinstance(value, list):
        for entry in value:
            if isinstance(entry, str) and entry.strip():
                return entry.strip()
        return None
    if isinstance(value, str):
        return value.strip() or None
    return None


def _issued(item: dict[str, Any]) -> str | None:
    for key in ("issued", "published"):
        stamp = item.get(key)
        if not isinstance(stamp, dict):
            continue
        parts = stamp.get("date-parts")
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], list):
            continue
        numbers = [int(part) for part in parts[0] if isinstance(part, int)]
        if numbers:
            # Crossref date-parts are [year, month, day] with the trailing
            # components often absent, so only the parts actually present are
            # joined. Padding stops at the year.
            return "-".join(
                str(number) if index == 0 else f"{number:02d}"
                for index, number in enumerate(numbers)
            )
    return None


def _abstract(item: dict[str, Any]) -> str:
    """Crossref's abstract, tag-stripped, or an empty string.

    Never generated. A work with no deposited abstract has none, and RAVEL
    records that by leaving the field empty rather than by summarizing the
    title.
    """
    raw = item.get("abstract")
    if not isinstance(raw, str) or not raw.strip():
        return ""
    text: list[str] = []
    inside = False
    for character in raw:
        if character == "<":
            inside = True
        elif character == ">":
            inside = False
            text.append(" ")
        elif not inside:
            text.append(character)
    return " ".join("".join(text).split())[:2000]
