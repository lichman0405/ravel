"""The Research Source Gateway: the only path by which evidence becomes real.

A model can say anything. An Evidence Ledger can only hold what RAVEL actually
reached, and the difference between the two is this package. Everything here is
organized around one rule, stated in `docs/05_RESEARCH_AND_EVIDENCE.md`:

    search result -> lead -> open original source -> verify -> register evidence

A search result is therefore not a weak form of evidence — it is a different
kind of thing, with its own type (`Lead`), and no function in RAVEL will turn
one into an `EvidenceSource`. Opening a lead produces a `Retrieval`, which
carries the bytes, the hash, and the moment they were read; only that can be
registered. A source RAVEL could not open is registered as restricted
(`PAYWALLED`, `AUTH_REQUIRED`, `ACCESS_LIMITED`, `POLICY_BLOCKED`) — which is a
finding about the world, not a failure of the gateway.

Nothing in this package fabricates. Where a capability is unavailable — no
search-provider credential, no browser libraries — it says so and stops, rather
than substituting a plausible answer.
"""

from ravel.research.gateway import ResearchSourceGateway, SourceRequest
from ravel.research.leads import Lead, Retrieval

__all__ = ["Lead", "ResearchSourceGateway", "Retrieval", "SourceRequest"]
