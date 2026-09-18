"""What every connector needs: an HTTP client that identifies itself, and a way
to fail without inventing an answer.

A connector that cannot reach its service returns no leads. It does not return
a plausible lead, and it does not fall back to another service's answer as
though the two were the same source. `ConnectorError` carries what went wrong
so the gateway can record that a source was unavailable — which is a fact worth
having when a research task comes back thin.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ravel.config import Settings
from ravel.research.fetching import FetchError, user_agent

logger = logging.getLogger(__name__)


class ConnectorError(RuntimeError):
    """A connector could not answer.

    Raised rather than returned so that a caller has to decide what an
    unavailable source means for its task, instead of silently receiving an
    empty list that reads like "nothing exists".
    """

    def __init__(self, connector: str, detail: str) -> None:
        super().__init__(f"{connector}: {detail}")
        self.connector = connector
        self.detail = detail


class HttpConnector:
    """Shared plumbing: one identified client, JSON or text, no fabrication."""

    name: str = "connector"

    def __init__(self, settings: Settings, *, timeout_seconds: float = 30.0) -> None:
        self.settings = settings
        self.timeout_seconds = timeout_seconds
        try:
            self.user_agent = user_agent(settings)
        except FetchError:
            # A connector can still be constructed without a contact address;
            # it simply cannot make a request. Failing here would make the
            # whole connector set unconstructible over one missing setting.
            self.user_agent = "RAVEL/0.1 (research source gateway)"

    @property
    def contact(self) -> str:
        """The contact address, for services that take it as a parameter."""
        return (self.settings.research_contact_email or "").strip()

    def get_json(self, url: str, *, params: dict[str, Any] | None = None) -> Any:
        response = self._get(url, params=params)
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError(
                self.name, f"{url} did not return JSON ({response.status_code})"
            ) from exc

    def get_text(self, url: str, *, params: dict[str, Any] | None = None) -> str:
        return self._get(url, params=params).text

    def _get(self, url: str, *, params: dict[str, Any] | None = None) -> httpx.Response:
        """One identified GET, with the service's own error text preserved.

        Raises:
            ConnectorError: The service refused, was unreachable, or answered
                with a status the connector cannot interpret as a result.
        """
        try:
            response = httpx.get(
                url,
                params=params,
                timeout=self.timeout_seconds,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent, "Accept": "application/json, */*"},
            )
        except httpx.HTTPError as exc:
            raise ConnectorError(self.name, f"{type(exc).__name__}: {exc}"[:300]) from exc

        if response.status_code == 404:
            raise ConnectorError(self.name, f"{url} has no such record")
        if response.status_code >= 400:
            raise ConnectorError(
                self.name, f"{url} answered HTTP {response.status_code}"
            )
        return response
