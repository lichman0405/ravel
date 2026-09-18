"""Structured connectors: the sources that answer with a record, not a page.

A connector talks to a service that publishes structured metadata about real
things — a DOI deposit, a chemical registry entry, a preprint. That structure
is what makes a connector worth more than a web search: a lead from Crossref
carries the work's own declared type, its DOI, and its publication date, and
none of those have to be inferred from a title.

**A connector returns leads, and a lead is not evidence.** Every connector here
answers a query with pointers. The gateway opens what the pointer names, and
what RAVEL read is what gets registered. Keeping that boundary in the type
system is what stops a rich metadata record from being mistaken for having read
the paper.

Each connector is one module and one entry in `CONNECTORS`. The set is not
fixed: `register` adds to it, and nothing else in RAVEL enumerates the
connectors by name.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ravel.config import Settings
from ravel.research.leads import Lead


@runtime_checkable
class Connector(Protocol):
    """A source of leads that answers with records rather than pages."""

    #: The name recorded on every lead this connector produces.
    name: str

    def search(self, query: str, *, limit: int) -> list[Lead]:
        """Leads matching a free-text query, best first."""
        ...

    def lookup(self, identifier: str) -> Lead | None:
        """The lead for one known identifier, or None when the source has none."""
        ...


def register(connector: Connector) -> Connector:
    """Add a connector to the default set."""
    CONNECTORS[connector.name] = connector
    return connector


def default_connectors(settings: Settings) -> dict[str, Connector]:
    """Every connector RAVEL ships, built for this configuration.

    Imported here rather than at module scope so that a connector which is
    expensive to construct — or which needs configuration this deployment does
    not have — does not have to exist for the others to work.
    """
    from ravel.research.connectors.arxiv import ArxivConnector
    from ravel.research.connectors.crossref import CrossrefConnector
    from ravel.research.connectors.openalex import OpenAlexConnector
    from ravel.research.connectors.pubchem import PubChemConnector

    built: list[Connector] = [
        CrossrefConnector(settings),
        OpenAlexConnector(settings),
        ArxivConnector(settings),
        PubChemConnector(settings),
    ]
    return {connector.name: connector for connector in built}


#: Connectors registered at runtime, beyond the shipped set.
CONNECTORS: dict[str, Connector] = {}

__all__ = ["CONNECTORS", "Connector", "default_connectors", "register"]
