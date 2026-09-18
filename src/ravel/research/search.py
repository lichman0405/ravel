"""Real web search, and the refusal to fake it.

A general web search needs a provider, and every provider needs a credential.
When one is configured, this module uses it. When one is not, it raises — it
does not return an empty list, and it does not fall back to a language model's
recollection of the web.

That refusal is the point. `docs/05_RESEARCH_AND_EVIDENCE.md` says web research
must be real, and the failure mode it is guarding against is not a crash: it is
a system that quietly answers from memory and labels the answer as a source.
An unavailable search must be as visible as a failed experiment, so
`SearchUnavailable` travels up to whoever asked, with the setting that would fix
it named in the message.

The providers here are the ones that publish a documented JSON API and return
the URL of each result, because a result RAVEL cannot open is not a lead.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol, runtime_checkable

import httpx

from ravel.config import Settings
from ravel.research.leads import Lead

logger = logging.getLogger(__name__)

#: How long a provider gets to answer. A search that hangs is worse than one
#: that fails: the research task behind it is holding a step open.
_REQUEST_TIMEOUT = 30.0


class SearchUnavailable(RuntimeError):
    """No usable web search provider is configured, or the provider failed.

    Deliberately not caught anywhere that could turn it into an empty result.
    A research task that cannot search the web must say so in its Research
    Record, and it can only say so if the failure reaches it.
    """


@runtime_checkable
class WebSearchProvider(Protocol):
    """A real, credentialed web search API."""

    #: The provider's name, recorded on every lead it returns. A class
    #: attribute rather than an instance one because it identifies the service,
    #: not the client.
    name: ClassVar[str]

    def search(self, query: str, *, limit: int) -> list[Lead]:
        """Web results for a query, as leads."""
        ...


@dataclass(frozen=True, slots=True)
class HttpSearchProvider:
    """Shared HTTP behaviour for the JSON search APIs.

    `name` and `endpoint` are class attributes rather than fields: they are
    properties of *which service this is*, not data a caller supplies. That
    also keeps the only constructor argument the credential, so a provider
    cannot be built for the wrong endpoint by passing the wrong string.
    """

    api_key: str
    timeout_seconds: float = _REQUEST_TIMEOUT
    name: ClassVar[str] = "search"
    endpoint: ClassVar[str] = ""

    def _post(self, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return self._send("POST", payload=payload, headers=headers)

    def _get(self, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
        return self._send("GET", params=params, headers=headers)

    def _send(
        self,
        method: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str],
    ) -> dict[str, Any]:
        """One provider request, with the provider's own words on failure.

        Raises:
            SearchUnavailable: The provider refused, was unreachable, or did
                not answer with a JSON object. Raised rather than returned so
                that a caller cannot mistake a failure for "no results".
        """
        try:
            response = httpx.request(
                method,
                self.endpoint,
                json=payload,
                params=params,
                headers={"Accept": "application/json", **headers},
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise SearchUnavailable(
                f"{self.name} could not be reached: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code in (401, 403):
            raise SearchUnavailable(
                f"{self.name} rejected the credential (HTTP {response.status_code}); "
                "check RAVEL_SEARCH_API_KEY"
            )
        if response.status_code == 429:
            raise SearchUnavailable(f"{self.name} rate limited this request (HTTP 429)")
        if response.status_code >= 400:
            raise SearchUnavailable(
                f"{self.name} answered HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            payload_out = response.json()
        except ValueError as exc:
            raise SearchUnavailable(f"{self.name} did not return JSON") from exc
        if not isinstance(payload_out, dict):
            raise SearchUnavailable(
                f"{self.name} returned {type(payload_out).__name__}, not an object"
            )
        return payload_out


@dataclass(frozen=True, slots=True)
class BraveSearch(HttpSearchProvider):
    """Brave Search API — an independent index, documented and stable."""

    name: ClassVar[str] = "brave"
    endpoint: ClassVar[str] = "https://api.search.brave.com/res/v1/web/search"

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        payload = self._get(
            {"q": query, "count": max(1, min(limit, 20))},
            headers={"X-Subscription-Token": self.api_key},
        )
        results = _nested(payload, "web", "results")
        return [
            Lead(
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                snippet=str(item.get("description") or ""),
                provider=self.name,
                rank=index,
                published=item.get("age") or item.get("page_age"),
            )
            for index, item in enumerate(results)
            if isinstance(item, dict) and item.get("url")
        ]


@dataclass(frozen=True, slots=True)
class TavilySearch(HttpSearchProvider):
    """Tavily — a search API built for agents, returning extracted URLs."""

    name: ClassVar[str] = "tavily"
    endpoint: ClassVar[str] = "https://api.tavily.com/search"

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        payload = self._post(
            {
                "api_key": self.api_key,
                "query": query,
                "max_results": max(1, min(limit, 20)),
                "search_depth": "basic",
            },
            headers={},
        )
        results = payload.get("results")
        return [
            Lead(
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                snippet=str(item.get("content") or ""),
                provider=self.name,
                rank=index,
            )
            for index, item in enumerate(results if isinstance(results, list) else [])
            if isinstance(item, dict) and item.get("url")
        ]


@dataclass(frozen=True, slots=True)
class SerperSearch(HttpSearchProvider):
    """Serper — a Google results API."""

    name: ClassVar[str] = "serper"
    endpoint: ClassVar[str] = "https://google.serper.dev/search"

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        payload = self._post(
            {"q": query, "num": max(1, min(limit, 20))},
            headers={"X-API-KEY": self.api_key},
        )
        results = payload.get("organic")
        return [
            Lead(
                url=str(item.get("link") or ""),
                title=str(item.get("title") or ""),
                snippet=str(item.get("snippet") or ""),
                provider=self.name,
                rank=index,
                published=item.get("date"),
            )
            for index, item in enumerate(results if isinstance(results, list) else [])
            if isinstance(item, dict) and item.get("link")
        ]


@dataclass(frozen=True, slots=True)
class ExaSearch(HttpSearchProvider):
    """Exa — a neural search API over its own index."""

    name: ClassVar[str] = "exa"
    endpoint: ClassVar[str] = "https://api.exa.ai/search"

    def search(self, query: str, *, limit: int = 10) -> list[Lead]:
        payload = self._post(
            {"query": query, "numResults": max(1, min(limit, 20))},
            headers={"x-api-key": self.api_key},
        )
        results = payload.get("results")
        return [
            Lead(
                url=str(item.get("url") or ""),
                title=str(item.get("title") or ""),
                snippet=str(item.get("text") or "")[:2000],
                provider=self.name,
                rank=index,
                published=item.get("publishedDate"),
            )
            for index, item in enumerate(results if isinstance(results, list) else [])
            if isinstance(item, dict) and item.get("url")
        ]


#: The providers RAVEL can be configured with. Adding one is one entry; nothing
#: else enumerates providers by name. Typed as a factory rather than as the base
#: class because each provider supplies its own name and endpoint as defaults,
#: so only the credential is passed in.
PROVIDERS: dict[str, Callable[..., WebSearchProvider]] = {
    "brave": BraveSearch,
    "tavily": TavilySearch,
    "serper": SerperSearch,
    "exa": ExaSearch,
}


def provider_for(settings: Settings) -> WebSearchProvider:
    """The configured web search provider.

    Raises:
        SearchUnavailable: No provider is configured, the named provider is not
            one RAVEL knows, or no credential is present. Each case names what
            is missing, because the operator reading this is the person who can
            fix it.
    """
    configured = (settings.search_provider or "").strip().lower()
    if not configured:
        known = ", ".join(sorted(PROVIDERS))
        raise SearchUnavailable(
            "no web search provider is configured. Set RAVEL_SEARCH_PROVIDER to one "
            f"of {known} and RAVEL_SEARCH_API_KEY to that provider's key. RAVEL does "
            "not answer web research from a model's memory, so an unconfigured "
            "search is reported rather than worked around."
        )
    factory = PROVIDERS.get(configured)
    if factory is None:
        known = ", ".join(sorted(PROVIDERS))
        raise SearchUnavailable(
            f"RAVEL_SEARCH_PROVIDER={configured!r} is not one RAVEL implements; known: {known}"
        )
    secret = settings.search_api_key
    key = secret.get_secret_value().strip() if secret else ""
    if not key:
        raise SearchUnavailable(
            f"RAVEL_SEARCH_PROVIDER is {configured!r} but RAVEL_SEARCH_API_KEY is unset"
        )
    return factory(api_key=key)


def _nested(payload: dict[str, Any], *path: str) -> list[Any]:
    """Walk a provider's nested response without assuming the shape holds."""
    node: Any = payload
    for key in path:
        if not isinstance(node, dict):
            return []
        node = node.get(key)
    return node if isinstance(node, list) else []
