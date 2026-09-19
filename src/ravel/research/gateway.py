"""The one way in: searching, opening, and registering a source.

`docs/05_RESEARCH_AND_EVIDENCE.md` states the rule this module implements, and
the rule is a sequence rather than a permission:

    search result -> lead -> open the original -> verify -> register evidence

Everything about the design follows from refusing to shorten it. There is no
method here that turns a search result into evidence, because that is the step
that would let a snippet become a citation. There is no method that registers a
source without a retrieval, because that is the step that would let a title
become a reading. `search` returns leads, `open` returns a retrieval, and
`register` accepts only a retrieval — so the shortest path from a query to the
Evidence Ledger is one that actually opened something.

Three further rules are enforced here rather than documented and hoped for:

- **A registration carries the bytes that were read.** The gateway snapshots
  the body into the artifact store and hashes what the store wrote, so the
  `content_hash` on an `EvidenceSource` describes a stored object rather than a
  number computed on the way past. `verify` re-opens the source later and
  compares, which is what makes the hash worth recording.
- **Restricted is a status, not a failure.** A paywall is recorded as a
  `PAYWALLED` source with no hash and no body. It never becomes a source with
  an empty body, and it never quietly disappears.
- **The tier comes with its reason.** `tiers.assign` states which rule fired,
  and that reason is written into the source's notes, so a decision resting on
  a Tier A source can be reviewed by someone who disagrees with the rule.

A gateway is scoped to one project at construction. Nothing it writes can
belong to another one, and no caller supplies a `project_id` per call — the
repository layer would refuse it anyway, but the shape here means the question
never comes up.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from ravel.config import Settings
from ravel.domain.enums import AccessStatus
from ravel.domain.evidence import EvidenceSource
from ravel.research import addressing, tiers
from ravel.research.browser import BrowserNavigator, BrowserUnavailable
from ravel.research.connectors import Connector, default_connectors
from ravel.research.fetching import Fetcher
from ravel.research.leads import Lead, Retrieval
from ravel.research.search import SearchUnavailable, WebSearchProvider, provider_for
from ravel.state.repositories.research import (
    ArtifactRepository,
    EvidenceSourceRepository,
)
from ravel.state.store import ArtifactStore, hash_chunks

logger = logging.getLogger(__name__)

#: Recorded on every snapshot's artifact, so a later reader can tell what a
#: stored object is without consulting the evidence row that points at it.
SNAPSHOT_KIND = "source-snapshot"

#: A rendered page whose own text is shorter than this is treated as a script
#: shell rather than a document, and the browser is tried. Set low: the cost of
#: a needless browser navigation is seconds, and the cost of registering an
#: empty shell as a source is a fabricated-looking citation.
_SHELL_CHARS = 200


class SourceRefused(RuntimeError):
    """A source could not be registered, and the reason is not about access.

    Distinct from a `Retrieval` whose status is restricted — that is a fact
    about the source and is registered. This is RAVEL declining to write a row
    that would overstate what it did.
    """


@dataclass(frozen=True, slots=True)
class SourceRequest:
    """What RAVEL is trying to open, and what it already knows about it.

    Assembled from a lead when there is one, so that the title and DOI a
    provider supplied travel as *claims from the provider* rather than being
    re-derived. `declared_type` is the provider's own classification, which is
    the only thing that can outrank the domain when the tier is assigned.
    """

    url: str
    title: str = ""
    publisher: str = ""
    doi: str | None = None
    declared_type: str | None = None
    provider: str = ""
    node_id: str | None = None

    @classmethod
    def from_lead(cls, lead: Lead) -> SourceRequest:
        """Turn a lead into a request to open it.

        The lead's identifiers are carried across unchanged; nothing is
        inferred from the URL's shape, because a URL that contains something
        DOI-shaped is a URL, not a registration.
        """
        return cls(
            url=lead.url,
            title=lead.title,
            doi=lead.declared_doi,
            declared_type=lead.identifiers.get("declared_type"),
            provider=lead.provider,
        )

    @classmethod
    def of(cls, url: str, **knowledge: Any) -> SourceRequest:
        """A request for a URL RAVEL already knows something about."""
        return cls(url=url, **knowledge)


@dataclass(frozen=True, slots=True)
class SearchOutcome:
    """Leads from a search, and which sources could not answer.

    Both halves are returned because collapsing them loses the interesting
    case: a search that returned three leads from one connector and nothing
    from another is not the same as a search that returned three leads, and a
    research record that cannot tell the difference cannot report a gap.
    """

    query: str
    leads: tuple[Lead, ...] = ()
    unavailable: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return not self.leads


@dataclass(frozen=True, slots=True)
class Registration:
    """A source that is now in the Evidence Ledger, and how it got there."""

    source: EvidenceSource
    retrieval: Retrieval
    tier: tiers.TierAssignment
    snapshot_ref: str | None = None

    @property
    def was_read(self) -> bool:
        return self.source.was_read


@dataclass(frozen=True, slots=True)
class Verification:
    """Whether a source still says what was recorded.

    For a source that was read, this compares content hashes, which is a real
    check: the bytes changed or they did not. For a source that was registered
    as restricted, there is no hash to compare, so the access status is
    compared instead — and `matches` means "still restricted in the same way",
    which is the strongest statement available about something RAVEL was never
    able to read.
    """

    source_id: str
    url: str
    matches: bool
    detail: str
    checked_at: datetime
    recorded_hash: str | None = None
    observed_hash: str | None = None
    access_status: AccessStatus | None = None


@dataclass
class ResearchSourceGateway:
    """Search, open, register, verify — for one project."""

    session: Session
    project_id: str
    settings: Settings
    store: ArtifactStore | None = None
    browser: BrowserNavigator | None = None
    fetcher: Fetcher | None = None
    #: How hostnames are resolved when deciding whether a URL may be fetched.
    #: Injectable so a test can state the addresses under test instead of
    #: depending on DNS; production uses the real resolver.
    resolver: addressing.Resolver = addressing.resolve
    _connectors: dict[str, Connector] | None = field(default=None, repr=False)
    _provider: WebSearchProvider | None = field(default=None, repr=False)
    _provider_resolved: bool = field(default=False, repr=False)

    # ── searching ─────────────────────────────────────────────────────────

    @property
    def connectors(self) -> dict[str, Connector]:
        """The structured connectors, built on first use.

        Built lazily because construction needs the configuration to be valid,
        and a gateway that is only ever used to open a known URL should not
        require the connectors to be constructible.
        """
        if self._connectors is None:
            self._connectors = default_connectors(self.settings)
        return self._connectors

    def search(
        self,
        query: str,
        *,
        sources: tuple[str, ...] | None = None,
        limit: int = 10,
    ) -> SearchOutcome:
        """Ask the structured connectors what they have.

        A connector that fails is recorded in `unavailable` with its own
        explanation and does not stop the others. That is deliberate: Crossref
        being down says nothing about arXiv, and a research task should be able
        to proceed on what answered while reporting what did not.

        Args:
            query: Free text, in whatever form the sources accept.
            sources: Connector names to consult; all of them by default.
            limit: The most leads to return in total.
        """
        chosen = self.connectors if sources is None else {
            name: self.connectors[name] for name in sources if name in self.connectors
        }
        leads: list[Lead] = []
        unavailable: list[str] = []
        for name, connector in chosen.items():
            try:
                leads.extend(connector.search(query, limit=limit))
            # One unavailable service says nothing about the others, and a
            # research task that stops at the first failure reports less than
            # it could have found. The failure is kept and returned in
            # `unavailable` rather than swallowed.
            except Exception as exc:
                logger.info("connector %s failed for %r: %s", name, query, exc)
                unavailable.append(f"{name}: {exc}")
        return SearchOutcome(
            query=query,
            leads=tuple(_dedupe(leads)[:limit]),
            unavailable=tuple(unavailable),
        )

    def lookup(self, identifier: str, *, source: str = "crossref") -> Lead | None:
        """The record for one known identifier, when a connector has it.

        Raises:
            KeyError: No connector by that name is configured.
        """
        return self.connectors[source].lookup(identifier)

    def search_web(self, query: str, *, limit: int = 10) -> SearchOutcome:
        """Search the open web through the configured provider.

        Raises:
            SearchUnavailable: No provider is configured, or it refused. This
                is raised rather than returned as an empty result, because a
                research task must report that it could not search rather than
                conclude that nothing exists.
        """
        return SearchOutcome(
            query=query,
            leads=tuple(_dedupe(self.web_provider.search(query, limit=limit))[:limit]),
        )

    @property
    def web_provider(self) -> WebSearchProvider:
        """The configured search provider, resolved once.

        Raises:
            SearchUnavailable: Nothing is configured, or the credential is
                missing. Raised on each access rather than cached as a failure,
                so that configuring a key mid-session starts working.
        """
        if not self._provider_resolved:
            self._provider_resolved = True
            try:
                self._provider = provider_for(self.settings)
            except SearchUnavailable:
                self._provider = None
        if self._provider is None:
            # Raised again by the same call that would have built it, so the
            # message names the exact setting that is missing.
            return provider_for(self.settings)
        return self._provider

    # ── opening ───────────────────────────────────────────────────────────

    def open(self, request: SourceRequest | str, *, rendered: bool = False) -> Retrieval:
        """Open a URL and report what came back.

        Plain HTTP first, because it is faster and it is what a reader with
        `curl` would see. When the response is a document with no readable text
        — a page that fills itself in with script — the browser is tried, and
        the retrieval's note says which path produced it.

        Args:
            request: What to open.
            rendered: Skip HTTP and go straight to the browser.
        """
        target = request if isinstance(request, SourceRequest) else SourceRequest(url=str(request))
        if rendered:
            return self.navigator.open(target.url)

        retrieval = self.fetcher_for.get(target.url)
        if retrieval.was_read and _is_a_shell(retrieval):
            rendered_retrieval = self._try_browser(target.url)
            if rendered_retrieval is not None:
                return rendered_retrieval
        return retrieval

    def open_lead(self, lead: Lead, *, rendered: bool = False) -> Retrieval:
        """Open a lead. The only way a search result becomes readable content."""
        return self.open(SourceRequest.from_lead(lead), rendered=rendered)

    def links(self, url: str, *, limit: int = 50) -> list[str]:
        """The links on a page, for walking to a source's own material.

        Raises:
            BrowserUnavailable: No browser is available. There is no HTTP
                fallback that would answer the same question.
        """
        return self.navigator.links(url, limit=limit)

    @property
    def fetcher_for(self) -> Fetcher:
        if self.fetcher is None:
            self.fetcher = Fetcher(self.settings, resolver=self.resolver)
        return self.fetcher

    @property
    def navigator(self) -> BrowserNavigator:
        if self.browser is None:
            self.browser = BrowserNavigator(self.settings, resolver=self.resolver)
        return self.browser

    def _try_browser(self, url: str) -> Retrieval | None:
        """The rendered page, or None when no browser could produce one.

        Logged rather than raised when the browser is unavailable, because the
        HTTP retrieval is still a true record of what the server sent; the
        caller gets that and the note explains that rendering was attempted.
        """
        try:
            return self.navigator.open(url)
        except BrowserUnavailable as exc:
            logger.info("no rendered retrieval for %s: %s", url, exc)
            return None

    # ── registering ───────────────────────────────────────────────────────

    def register(
        self,
        request: SourceRequest,
        retrieval: Retrieval,
        *,
        actor_id: str,
        node_id: str | None = None,
        research_task_ref: str | None = None,
        snapshot: bool = True,
    ) -> Registration:
        """Write a source into the Evidence Ledger.

        This is the only path from the outside world into the ledger, and it
        takes a `Retrieval` rather than a URL precisely so that nothing can be
        registered that was not opened first.

        Raises:
            SourceRefused: The retrieval does not support registration — it was
                a HEAD, which establishes that a URL exists without reading
                anything from it, or it succeeded without yielding a hash.
        """
        if retrieval.access_status is AccessStatus.OK and retrieval.content_hash is None:
            raise SourceRefused(
                f"{retrieval.final_url} was not read: this retrieval established that "
                "the URL answers, but no bytes were hashed, so there is nothing to "
                "register as evidence. Opening it with GET produces a retrieval that "
                "can be registered."
            )

        # The hash on the row has to describe bytes RAVEL holds, not just bytes
        # it was told about.
        #
        # When a snapshot is written, the store's own hash of the object is what
        # is recorded, and it is compared with the retrieval's — that check is
        # in `_snapshot`. It cannot run when there is no store or no snapshot
        # was asked for, and then the retrieval's own hash is recorded on the
        # strength of the retrieval saying so. `register` accepts any
        # `Retrieval`, including one an agent assembled, so "the retrieval says
        # so" is not a property this ledger can rest on. Recomputing the hash
        # here costs one pass over a body already in memory and makes the
        # recorded hash something RAVEL computed from bytes it has.
        if retrieval.body is not None and retrieval.content_hash is not None:
            observed, _ = hash_chunks([retrieval.body])
            if observed != retrieval.content_hash:
                raise SourceRefused(
                    f"the retrieval of {retrieval.final_url} carries the hash "
                    f"{retrieval.content_hash} but its body hashes to {observed}; the "
                    "hash does not describe the bytes it came with, so it cannot be "
                    "recorded as what was read"
                )

        tier = tiers.assign(
            retrieval.final_url,
            declared_type=request.declared_type,
        )
        artifact_ref, snapshot_ref, content_hash = self._snapshot(
            request,
            retrieval,
            actor_id=actor_id,
            node_id=node_id or request.node_id,
            enabled=snapshot,
        )
        source = EvidenceSource(
            project_id=self.project_id,
            url=retrieval.final_url,
            title=retrieval.title or request.title,
            publisher=request.publisher,
            doi=request.doi,
            access_status=retrieval.access_status,
            retrieved_at=retrieval.retrieved_at if retrieval.was_read else None,
            content_hash=content_hash,
            media_type=retrieval.media_type,
            artifact_ref=artifact_ref,
            snapshot_ref=snapshot_ref,
            # Stored as fields as well as described in `notes`: a claim's tier
            # and access are derived from its sources, and a derivation that
            # read them back out of a sentence would depend on the sentence.
            tier=tier.tier,
            declared_type=request.declared_type,
            notes=_notes(request, retrieval, tier, research_task_ref),
        )
        return Registration(
            source=self.sources.record(source),
            retrieval=retrieval,
            tier=tier,
            snapshot_ref=snapshot_ref,
        )

    def obtain(
        self,
        request: SourceRequest,
        *,
        actor_id: str,
        node_id: str | None = None,
        research_task_ref: str | None = None,
        snapshot: bool = True,
        rendered: bool = False,
    ) -> Registration:
        """Open a request and register the result, restricted or not.

        The common path, and the one named in the design rule. A restricted
        retrieval is registered too: "this source exists and RAVEL could not
        read it" is a finding, and a research task that hit a paywall needs to
        be able to say so.
        """
        retrieval = self.open(request, rendered=rendered)
        return self.register(
            request,
            retrieval,
            actor_id=actor_id,
            node_id=node_id,
            research_task_ref=research_task_ref,
            snapshot=snapshot,
        )

    def _snapshot(
        self,
        request: SourceRequest,
        retrieval: Retrieval,
        *,
        actor_id: str,
        node_id: str | None,
        enabled: bool,
    ) -> tuple[str | None, str | None, str | None]:
        """Store the bytes that were read, and hash what was stored.

        The hash comes from the store's own record of the object it wrote, not
        from the retrieval, and the two are compared. They are computed over
        the same bytes by the same algorithm, so a mismatch means the bytes
        that reached the store are not the bytes that were read — which is
        exactly the failure a verifiable snapshot exists to catch.
        """
        if not enabled or self.store is None or retrieval.body is None:
            return None, None, retrieval.content_hash

        artifact, version = self.artifacts.register(
            name=f"snapshot: {retrieval.title or request.url}"[:200],
            chunks=[retrieval.body],
            created_by=actor_id,
            filename=_filename(retrieval),
            media_type=retrieval.media_type or "",
            kind=SNAPSHOT_KIND,
            provenance=retrieval.final_url,
            node_id=node_id,
            note=f"retrieved {retrieval.retrieved_at.isoformat()}",
        )
        if retrieval.content_hash and version.content_hash != retrieval.content_hash:
            raise SourceRefused(
                f"the snapshot of {retrieval.final_url} hashed to "
                f"{version.content_hash} but the retrieval hashed to "
                f"{retrieval.content_hash}; the stored bytes are not the read bytes"
            )
        return artifact.artifact_id, version.storage_key, version.content_hash

    @property
    def sources(self) -> EvidenceSourceRepository:
        return EvidenceSourceRepository(self.session, self.project_id)

    @property
    def artifacts(self) -> ArtifactRepository:
        if self.store is None:
            raise SourceRefused(
                "this gateway has no artifact store, so a snapshot cannot be "
                "written; construct it with a store, or register without a snapshot"
            )
        return ArtifactRepository(self.session, self.project_id, self.store)

    # ── verifying ─────────────────────────────────────────────────────────

    def verify(self, source: EvidenceSource, *, actor_id: str | None = None) -> Verification:
        """Re-open a registered source and compare it with what was recorded.

        For a source that was read, this is a hash comparison: the bytes are
        either the same bytes or they are not. A source that no longer matches
        is not corrected here — the record of what was read at the time stands,
        and the change is reported so that whoever relies on it can decide what
        to do.
        """
        checked_at = datetime.now(UTC)
        try:
            retrieval = self.fetcher_for.get(source.url)
        # A source that cannot be re-opened is a result of the check, not a
        # fault in it: "this URL no longer answers" is exactly what a caller
        # wants to learn from verifying it.
        except Exception as exc:
            return Verification(
                source_id=source.source_id,
                url=source.url,
                matches=False,
                detail=f"could not be re-opened: {type(exc).__name__}: {exc}",
                checked_at=checked_at,
                recorded_hash=source.content_hash,
            )

        if source.content_hash is None:
            same = retrieval.access_status is source.access_status
            return Verification(
                source_id=source.source_id,
                url=source.url,
                matches=same,
                detail=(
                    f"recorded as {source.access_status.value} with no content hash; "
                    f"it now answers {retrieval.access_status.value}"
                ),
                checked_at=checked_at,
                access_status=retrieval.access_status,
            )

        if not retrieval.was_read:
            return Verification(
                source_id=source.source_id,
                url=source.url,
                matches=False,
                detail=(
                    f"was read when registered; it now answers "
                    f"{retrieval.access_status.value} ({retrieval.note or 'no reason given'})"
                ),
                checked_at=checked_at,
                recorded_hash=source.content_hash,
                access_status=retrieval.access_status,
            )

        matches = retrieval.content_hash == source.content_hash
        return Verification(
            source_id=source.source_id,
            url=source.url,
            matches=matches,
            detail=(
                "the source is unchanged"
                if matches
                else "the source has changed since it was read; the recorded hash "
                "describes what was read then, not what is served now"
            ),
            checked_at=checked_at,
            recorded_hash=source.content_hash,
            observed_hash=retrieval.content_hash,
            access_status=retrieval.access_status,
        )


def _dedupe(leads: list[Lead]) -> list[Lead]:
    """One lead per URL, keeping the first.

    The first is kept rather than the best-ranked, because ranks are not
    comparable across providers and "first" is at least a rule that can be
    stated. Which provider supplied it is on the lead either way.
    """
    seen: set[str] = set()
    unique: list[Lead] = []
    for lead in leads:
        key = lead.url.split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        unique.append(lead)
    return unique


def _is_a_shell(retrieval: Retrieval) -> bool:
    """Whether a successful retrieval is a page with nothing in it yet.

    Judged on the excerpt, which is the source's own text with the tags taken
    out. A short excerpt from an HTML response means the document did not carry
    its content in the markup — the case a browser exists to handle.
    """
    if retrieval.media_type not in ("text/html", "application/xhtml+xml"):
        return False
    return len(retrieval.excerpt) < _SHELL_CHARS


def _filename(retrieval: Retrieval) -> str:
    """A filename for a snapshot, taken from the URL that answered."""
    path = retrieval.final_url.split("?")[0].split("#")[0].rstrip("/")
    tail = path.rsplit("/", 1)[-1] or "index"
    cleaned = "".join(character for character in tail if character.isalnum() or character in "._-")
    return (cleaned or "index")[:120]


def _notes(
    request: SourceRequest,
    retrieval: Retrieval,
    tier: tiers.TierAssignment,
    research_task_ref: str | None,
) -> str:
    """What a later reader needs in order to trust this row.

    The tier's rule is written down because a tier without its reason is a
    number to be taken on faith, and the retrieval's own note is carried
    through because it may say that the text came from a rendered document
    rather than from the bytes the server sent.
    """
    parts = [f"tier {tier.tier.value} ({tier.rule}): {tier.detail}"]
    if request.provider:
        parts.append(f"lead from {request.provider}")
    if request.title and not retrieval.title:
        parts.append("title from the lead; the source itself supplied none")
    if retrieval.note:
        parts.append(retrieval.note)
    if research_task_ref:
        parts.append(f"research task {research_task_ref}")
    return "; ".join(parts)
