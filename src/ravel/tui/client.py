"""The TUI's half of the wire, and the whole of its knowledge about it.

`docs/08` §2 is a list of things the TUI does not do — it runs no scientific
logic, calls DSH never, mutates the DAG never, holds no authoritative state.
What is left is display, input and control, which means this module's job is to
turn HTTP and WebSocket into Python and to have no opinions past that. Every
method here is one route; none of them composes two, caches anything, or
decides what a response means.

**The event stream is resumed from a sequence number, and the number is the
client's.** §7 says the TUI stores the last event sequence and, on reconnect,
authenticates, fetches the current projection and replays events after that
sequence. The gateway supports exactly that — `after_seq` on the socket — and
the discipline that makes it work is that the sequence advances only as frames
are *yielded to the caller*, so a stream that dies mid-page resumes from the
last event that actually reached the screen rather than from the last one the
socket happened to receive.

**A refusal is data.** A 404 on a project is how a member learns they are not
one; a 401 is how the TUI learns to re-authenticate. Raising an exception for
each and letting the screen decide would put the same `if` in every screen, so
`GatewayError` carries the status and the detail and the screens read it.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import httpx
import websockets

#: How long a plain request may take. Long enough for a project projection
#: assembled by `ProjectAudit`, short enough that a person does not sit looking
#: at a spinner wondering whether the program is alive.
REQUEST_TIMEOUT_SECONDS = 20.0

#: How long to wait for the socket to open. Separate from the request timeout
#: because a Gateway that is up but whose socket is wedged is a different
#: problem from one that is not answering at all.
CONNECT_TIMEOUT_SECONDS = 10.0

#: The application close codes the events route uses. Named here so a screen
#: can say "sign in again" rather than "closed with code 4401".
CLOSE_UNAUTHENTICATED = 4401
CLOSE_NO_SUCH_PROJECT = 4404
CLOSE_BAD_REQUEST = 4400


class GatewayError(Exception):
    """A refusal from the Gateway, with enough in it to act on.

    Attributes:
        status: The HTTP status, or `None` for a transport failure.
        detail: What the Gateway said, or the transport's own message.
    """

    def __init__(self, status: int | None, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail

    @property
    def is_authentication(self) -> bool:
        """Whether signing in again would help. 401 and 403, and nothing else.

        A 404 is deliberately not one: it is how this Gateway says "not yours",
        and re-authenticating would not change the answer.
        """
        return self.status in {401, 403}


@dataclass
class Credentials:
    """What a signed-in session holds. §7's list, and it is a short one.

    The refresh token is the credential worth keeping; the access token is
    short-lived and is re-derived. Nothing about a project is here, because the
    Gateway is the source of truth for all of it and a TUI that remembered a
    DAG would be a second one.
    """

    username: str = ""
    access_token: str = ""
    refresh_token: str = ""
    #: The last event sequence this client has *shown*. Resuming is from here.
    last_event_seq: int = 0

    @property
    def signed_in(self) -> bool:
        return bool(self.access_token)


@dataclass
class GatewayClient:
    """One HTTP client and one place where the base URL lives."""

    base_url: str = "http://127.0.0.1:8000"
    credentials: Credentials = field(default_factory=Credentials)
    _http: httpx.AsyncClient | None = field(default=None, init=False, repr=False)

    # ── Plumbing ────────────────────────────────────────────────────────────

    def http(self) -> httpx.AsyncClient:
        """The underlying client, built once and reused.

        Reused because a connection pool per request would re-do TLS for every
        panel on the screen, and because one client is one place to close.
        """
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"), timeout=REQUEST_TIMEOUT_SECONDS
            )
        return self._http

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _headers(self) -> dict[str, str]:
        if not self.credentials.access_token:
            return {}
        return {"Authorization": f"Bearer {self.credentials.access_token}"}

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Send one request and return its JSON, or raise `GatewayError`.

        `GatewayError(status=None, ...)` for a transport failure, which is the
        case a screen most needs to distinguish: "the Gateway did not answer" is
        a different message from "the Gateway said no".

        A caller's own headers are merged with the credential rather than
        passed alongside it. `httpx.request` takes one mapping, so a call that
        supplied a second `headers=` would raise a `TypeError` about duplicate
        keyword arguments — a message that says nothing about headers, on a
        path only the upload reaches, which is how the upload came to be the
        one method here that had never once worked.
        """
        headers = {**self._headers(), **kwargs.pop("headers", {})}
        try:
            response = await self.http().request(method, path, headers=headers, **kwargs)
        except httpx.HTTPError as failed:
            raise GatewayError(None, f"{type(failed).__name__}: {failed}") from failed

        if response.status_code >= 400:
            raise GatewayError(response.status_code, _detail_of(response))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    async def _get(self, path: str, **params: Any) -> Any:
        wanted = {key: value for key, value in params.items() if value is not None}
        return await self._request("GET", path, params=wanted)

    async def _post(self, path: str, **body: Any) -> Any:
        return await self._request("POST", path, json=body or {})

    # ── Identity ────────────────────────────────────────────────────────────

    async def login(self, username: str, password: str) -> Credentials:
        """Sign in, and keep the pair the Gateway hands back."""
        pair = await self._request(
            "POST", "/auth/login", json={"username": username, "password": password}
        )
        self.credentials = Credentials(
            username=username,
            access_token=pair["access_token"],
            refresh_token=pair["refresh_token"],
            last_event_seq=self.credentials.last_event_seq,
        )
        return self.credentials

    async def refresh(self) -> Credentials:
        """Rotate the chain, keeping the sequence number.

        The sequence survives because it is a fact about what this person has
        seen and not about the credential — dropping it on refresh would make
        the next reconnect replay the whole project.
        """
        pair = await self._request(
            "POST", "/auth/refresh", json={"refresh_token": self.credentials.refresh_token}
        )
        self.credentials.access_token = pair["access_token"]
        self.credentials.refresh_token = pair["refresh_token"]
        return self.credentials

    async def logout(self) -> None:
        """Revoke the chain. Idempotent, and the Gateway says nothing either way."""
        await self._request(
            "POST", "/auth/logout", json={"refresh_token": self.credentials.refresh_token}
        )
        self.credentials = Credentials(last_event_seq=self.credentials.last_event_seq)

    async def me(self) -> dict[str, Any]:
        return await self._request("GET", "/auth/me")

    # ── Projects ────────────────────────────────────────────────────────────

    async def projects(self) -> list[dict[str, Any]]:
        return await self._request("GET", "/projects")

    async def projection(self, project_id: str) -> dict[str, Any]:
        """The authoritative state, which is what a screen opens on."""
        return await self._request("GET", f"/projects/{project_id}")

    async def dag(self, project_id: str) -> list[dict[str, Any]]:
        """The Scientific DAG, read-only. There is no method here that writes it."""
        return await self._request("GET", f"/projects/{project_id}/dag")

    async def decisions(self, project_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/projects/{project_id}/decisions")

    async def reviews(self, project_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/projects/{project_id}/reviews")

    async def executions(self, project_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/projects/{project_id}/executions")

    async def evidence(self, project_id: str, node_id: str | None = None) -> list[dict[str, Any]]:
        """Claims, each with the sources it cites.

        The sources arrive joined into the same answer rather than as a second
        request per reference, which is what makes "where did this come from"
        answerable while somebody is reading a line rather than after they have
        clicked it.
        """
        return await self._get(f"/projects/{project_id}/evidence", node_id=node_id)

    async def approvals(self, project_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/projects/{project_id}/approvals")

    async def envelope(self, project_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{project_id}/envelope")

    async def messages(self, project_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/projects/{project_id}/messages")

    async def events(self, project_id: str, after_seq: int = 0, limit: int | None = None):
        return await self._get(
            f"/projects/{project_id}/events", after_seq=after_seq, limit=limit
        )

    # ── Control ─────────────────────────────────────────────────────────────

    async def say(self, project_id: str, text: str) -> dict[str, Any]:
        """Talk to Master. The answer is a message in the transcript, not a DAG edit.

        Master may decide to change the DAG as a result, through its own tools
        and its own session — but that decision is Master's and is recorded as
        a Decision, which is why this returns a message and not a graph.
        """
        # `body`, which is the field `MessageRequest` declares. The route is
        # named for what a person does rather than for the field, so this is
        # the one place the two names differ and the one place worth saying so.
        return await self._post(f"/projects/{project_id}/messages", body=text)

    async def pause(self, project_id: str, reason: str = "") -> dict[str, Any]:
        return await self._post(f"/projects/{project_id}/pause", reason=reason)

    async def resume(self, project_id: str) -> dict[str, Any]:
        return await self._post(f"/projects/{project_id}/resume")

    async def resolve_approval(
        self, project_id: str, approval_id: str, approved: bool, note: str = ""
    ) -> dict[str, Any]:
        return await self._post(
            f"/projects/{project_id}/approvals/{approval_id}/resolve",
            approved=approved,
            note=note,
        )

    async def set_envelope(self, project_id: str, **envelope: Any) -> dict[str, Any]:
        """Narrow or widen what Master may do without asking.

        The one control that is about authority rather than about progress, and
        the reason it is a controlled command rather than a free-text field:
        the envelope is a schema, and a TUI that let somebody type one would be
        a way to set authority without knowing what was set.
        """
        return await self._post(f"/projects/{project_id}/envelope", **envelope)

    # ── Membership ──────────────────────────────────────────────────────────

    async def open_project(self, title: str, objective: str) -> dict[str, Any]:
        """Open a project, owned by whoever opened it.

        Not registration: the account already exists. This is the one
        membership nobody confers, which is why it is reachable by any
        authenticated caller — there is nobody to be an owner of yet.
        """
        return await self._post("/projects", title=title, objective=objective)

    async def members(self, project_id: str) -> list[dict[str, Any]]:
        """Who is in this project now. Only an owner may read it."""
        return await self._request("GET", f"/projects/{project_id}/members")

    async def add_member(
        self, project_id: str, username: str, role: str
    ) -> dict[str, Any]:
        """Give somebody who already has an account a role in this project.

        By username rather than by identifier, because the person adding
        somebody knows their name and a client that had to look an identifier
        up first would need a user directory this Gateway deliberately does
        not have.
        """
        return await self._post(
            f"/projects/{project_id}/members", username=username, role=role
        )

    async def revoke_member(self, project_id: str, user_id: str) -> dict[str, Any]:
        """Withdraw somebody's role. The row stays; the authority goes.

        Takes effect on that person's next request rather than when their
        token expires, which is the property the owner screen's confirmation
        is written against.
        """
        return await self._post(f"/projects/{project_id}/members/{user_id}/revoke")

    # ── Lab ─────────────────────────────────────────────────────────────────

    async def lab_tasks(self, project_id: str) -> list[dict[str, Any]]:
        return await self._request("GET", f"/projects/{project_id}/lab/tasks")

    async def lab_task(self, project_id: str, task_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{project_id}/lab/tasks/{task_id}")

    async def report_deviation(
        self, project_id: str, task_id: str, requested_action: str, description: str
    ) -> dict[str, Any]:
        return await self._post(
            f"/projects/{project_id}/lab/tasks/{task_id}/deviations",
            requested_action=requested_action,
            description=description,
        )

    async def send_output(
        self,
        project_id: str,
        task_id: str,
        output: str,
        content: bytes,
        *,
        filename: str = "",
        media_type: str = "",
    ) -> dict[str, Any]:
        """Send the file that answers one output the bench was asked to produce.

        **The output is named, and that is the difference between this and
        `upload`.** The artifact door below files bytes as an artifact and
        nothing more; this one files them *against a handover*, under a name
        the contract required, and tells the waiting run. The run then decides
        from what is recorded whether it has everything — which is why a file
        that answers nothing is refused here with the names that are owed, and
        why the answer says which of them are still missing.

        A name rather than a guess from the filename: which file answers which
        output is the contract's statement, and a screen that inferred it would
        be deciding what a person's data is.
        """
        return await self._request(
            "POST",
            f"/projects/{project_id}/lab/tasks/{task_id}/uploads",
            params={
                "output": output,
                "filename": filename,
                "media_type": media_type,
            },
            content=content,
            headers={"Content-Type": media_type or "application/octet-stream"},
        )

    async def upload(
        self,
        project_id: str,
        content: bytes,
        *,
        name: str = "",
        filename: str = "",
        media_type: str = "",
        artifact_id: str | None = None,
    ) -> dict[str, Any]:
        """Send bytes to the Gateway, which stores them and registers them.

        Not presigned, and §6 allows either: the Gateway checks membership on
        this request, so a link that leaked would be a link to nothing.
        """
        params = {"name": name, "filename": filename, "media_type": media_type}
        if artifact_id is not None:
            params["artifact_id"] = artifact_id
        return await self._request(
            "POST",
            f"/projects/{project_id}/artifacts",
            params={key: value for key, value in params.items() if value},
            content=content,
            headers={**self._headers(), "Content-Type": media_type or "application/octet-stream"},
        )

    # ── Admin ───────────────────────────────────────────────────────────────

    async def runtime(self, project_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{project_id}/runtime")

    async def temporal_health(self, project_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{project_id}/runtime/temporal")

    async def harness_health(self, project_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/projects/{project_id}/runtime/harness")

    async def service_health(self, project_id: str) -> dict[str, Any]:
        """Whether the long-running processes are still reporting.

        One panel is not like the others on the admin screen and this is it:
        the answer is a process's own account of itself, because a supervisor
        that died leaves nothing else behind. What the Gateway adds is the
        judgement — how long the silence has been, against the cadence the
        service promised.
        """
        return await self._request("GET", f"/projects/{project_id}/runtime/services")

    async def backend_health(self, project_id: str) -> dict[str, Any]:
        """Which backends have run this project's work, and the cluster's setup.

        Not which backends are *registered*: the registry belongs to the
        worker's command line, in another process. What this answers is a fact
        in the database — what the work was actually handed to — plus the Slurm
        configuration with the secret represented by the name of the setting
        that holds it.
        """
        return await self._request("GET", f"/projects/{project_id}/runtime/backends")

    async def jobs(self, project_id: str) -> dict[str, Any]:
        """Backend jobs, split into the ones still open and the ones that ended."""
        return await self._request("GET", f"/projects/{project_id}/runtime/jobs")

    async def reconciliations(self, project_id: str) -> dict[str, Any]:
        """Runs RAVEL found dead, and what it did about them."""
        return await self._request(
            "GET", f"/projects/{project_id}/runtime/reconciliations"
        )

    # ── The live view ───────────────────────────────────────────────────────

    async def stream(
        self, project_id: str, after_seq: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Follow a project's events, resuming after the last one shown.

        Yields the anchor frame first — which says where the stream is and how
        far the project got — then events, then heartbeats. The heartbeat is
        yielded rather than swallowed: it is the only evidence a screen has that
        the socket is alive, and a UI that hides it cannot tell "nothing has
        happened" from "the connection died ten minutes ago".

        The caller's `last_event_seq` advances as frames leave this generator,
        so an exception anywhere downstream resumes from the last frame that
        was actually delivered.
        """
        start = self.credentials.last_event_seq if after_seq is None else after_seq
        url = (
            f"{self.base_url.rstrip('/').replace('http://', 'ws://').replace('https://', 'wss://')}"
            f"/projects/{project_id}/events/stream?after_seq={start}"
        )
        socket = await websockets.connect(
            url,
            additional_headers={"Authorization": f"Bearer {self.credentials.access_token}"},
            open_timeout=CONNECT_TIMEOUT_SECONDS,
        )
        try:
            async for raw in socket:
                frame = json.loads(raw)
                if frame.get("type") == "event" and isinstance(frame.get("seq"), int):
                    self.credentials.last_event_seq = max(
                        self.credentials.last_event_seq, frame["seq"]
                    )
                yield frame
        finally:
            await socket.close()


def _detail_of(response: httpx.Response) -> str:
    """What the Gateway said went wrong, in whatever shape it said it.

    FastAPI answers a validation failure with a list of objects and everything
    else with a string, so both are handled. The list is summarised rather than
    dumped: it is going on one line of a terminal panel.
    """
    try:
        body = response.json()
    except ValueError:
        return response.text[:200] or f"HTTP {response.status_code}"

    detail = body.get("detail") if isinstance(body, dict) else None
    if isinstance(detail, str):
        return detail
    if isinstance(detail, list):
        parts = [
            f"{'.'.join(str(piece) for piece in item.get('loc', ()))}: {item.get('msg', '')}"
            for item in detail
            if isinstance(item, dict)
        ]
        return "; ".join(parts) or f"HTTP {response.status_code}"
    return f"HTTP {response.status_code}"


__all__ = [
    "CLOSE_BAD_REQUEST",
    "CLOSE_NO_SUCH_PROJECT",
    "CLOSE_UNAUTHENTICATED",
    "CONNECT_TIMEOUT_SECONDS",
    "REQUEST_TIMEOUT_SECONDS",
    "Credentials",
    "GatewayClient",
    "GatewayError",
]
