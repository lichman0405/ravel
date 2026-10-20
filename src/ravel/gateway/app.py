"""The Research Gateway: the only thing the internet is allowed to talk to.

`docs/08` draws the boundary as `TUI -> Gateway -> Runtime -> DSH`, and the
first arrow is the one this module is. `docs/09` states the negative half: DSH
has no end-user identity responsibility and is not directly exposed. There is
no route here that forwards a request to the harness, and no route that
returns a harness session identifier to a caller who could use it as one —
the Master conversation is answered by RAVEL, inside a request, and what the
caller gets back is a message in a transcript.

**No route changes the Scientific DAG.** That is not an omission to be filled
in later. The DAG is mutated by Master, an agent role, through tools that
resolve their authority from a server-side session binding; `docs/09` says an
owner cannot direct-mutate the DAG, and the way to honour that is for no user
credential to reach a DAG mutation at all. `tests/e2e/test_tui.py` asserts it
both by probing every route with an owner's token and by reading the source of
this package for an import that would make it possible.

The factory takes its collaborators as arguments so that tests can supply a
database and a settings object of their own, and defaults them so that the
Makefile's `uvicorn ravel.gateway.app:create_app --factory` works with no
arguments at all.
"""

from __future__ import annotations

import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from ravel.config import Settings, get_settings
from ravel.domain.services import GATEWAY
from ravel.execution.node_runs import ExecutionService, ExternalResultPort
from ravel.gateway.auth.limits import LoginRateLimiter, TrustedProxies
from ravel.gateway.auth.policy import exact_origins, validate_gateway_settings
from ravel.gateway.auth.tokens import TokenService
from ravel.gateway.conversation import MasterFactory
from ravel.gateway.deps import GatewayState
from ravel.gateway.errors import install_error_handlers
from ravel.gateway.routes import (
    admin,
    artifacts,
    auth,
    control,
    conversation,
    events,
    lab,
    members,
    projects,
)
from ravel.gateway.routes.premium import router as v1_router
from ravel.gateway.routes.premium.errors import install_v1_error_handlers
from ravel.gateway.runtime import HarnessRuntime
from ravel.gateway.stream import Cadence
from ravel.service import Heartbeat
from ravel.state.database import Database

#: What the Gateway calls itself. Versioned because a TUI written against V0
#: has to be able to notice that it is talking to something else.
TITLE = "RAVEL Research Gateway"
API_VERSION = "0.1.0"

#: How often this process says it is here. A Gateway waits on sockets rather
#: than polling anything, so it has no natural beat to hang a report on and
#: pulses on this interval instead.
GATEWAY_PULSE_SECONDS = 60.0

#: The shape an incoming request identifier may have if it is to be echoed.
#: A client-supplied value is echoed so one action can be followed across a
#: proxy and the Gateway's own logs, but it is not trusted as text: no spaces,
#: no newlines, and a bounded length, so a log line cannot be forged with it.
REQUEST_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    tokens: TokenService | None = None,
    master_of: MasterFactory | None = None,
    deliver_external: ExternalResultPort | None = None,
    cadence: Cadence | None = None,
    limiter: LoginRateLimiter | None = None,
    heartbeat_seconds: float | None = GATEWAY_PULSE_SECONDS,
    event_retention: int | None = None,
) -> FastAPI:
    """Build the Gateway.

    Args:
        settings: Configuration. Defaults to the process's own.
        database: Authoritative state. Defaults to one built from `settings`.
        tokens: The access-token service. Defaults to one built from the
            configured secret.
        master_of: How a project's Master is reached. Defaults to the harness,
            through a factory that starts no runtime until somebody speaks to
            one — so a Gateway that only serves reads costs nothing extra, and
            a test can script Master without a model being involved.
        deliver_external: How a waiting run is told that something outside
            RAVEL happened. Defaults to the real one over Temporal, which
            connects on the first delivery and not before — so a Gateway whose
            laboratory surface is never used opens no connection, and a test
            that is about what an upload *records* can supply its own.
        cadence: How often the event stream looks for new events and how often
            it speaks when there are none. Defaults to the production rates;
            a test lowers them rather than sleeping through them.
        limiter: The per-address, per-account login failure budget. Defaults to
            a fresh one; a test injects one with its own clock so that a
            fifteen-minute lock can be observed without waiting fifteen
            minutes. The default is per process, which is what a single-Gateway
            V0 deployment is.
        heartbeat_seconds: How often this process reports itself alive, or
            `None` to have it report nothing. On by default, because a Gateway
            that did not report would be a service the administrator's screen
            could not tell from a dead one — and off only for a caller that has
            no liveness row to write to, which is a test holding a stub in place
            of a database rather than a deployment.
        event_retention: How many of the most recent event sequences the
            versioned stream promises to replay. `None` — the default, and what
            V0 actually retains — replays everything and never resynchronizes.
            A deployment may declare a smaller window; a cursor older than it
            is answered with a fresh snapshot and `RESYNC_REQUIRED` rather than
            with silence. The legacy socket is unaffected in either case.

    Raises:
        ValueError: The settings are not fit for the environment they name —
            a development credential in production, a public bind that was not
            asked for, a missing or malformed browser origin, or a stream
            reauthorization interval outside its bound. Checked before the
            database is opened and whether or not a token service was injected,
            so a deployment that would be forgeable or reachable fails to start
            rather than running.
    """
    resolved = settings or get_settings()
    if event_retention is not None and event_retention < 1:
        raise ValueError("event_retention must hold at least one sequence, or be None")
    # Before the database: a Gateway that cannot be safely reached should not
    # first establish that it can reach its state. The check is unconditional
    # rather than tied to `tokens is None` — a caller may inject a token service
    # for a reason that has nothing to do with the database credentials, and
    # that must not be a way around every other production rule.
    validate_gateway_settings(resolved)
    resolved_database = database or Database.from_settings(resolved)

    if tokens is None:
        secret = resolved.gateway_jwt_secret.get_secret_value()
        tokens = TokenService(secret=secret, ttl_seconds=resolved.gateway_token_ttl_seconds)

    app = FastAPI(
        title=TITLE,
        version=API_VERSION,
        summary="The runtime's user-facing surface. It is not a DSH proxy.",
        # The docs are served because this is a developer-preview product and
        # the TUI is not the only client a person may want to write. They
        # describe no route that changes the DAG, so publishing them publishes
        # nothing an owner may not already do.
        docs_url="/docs",
        openapi_url="/openapi.json",
        lifespan=_lifespan(resolved_database, resolved, heartbeat_seconds),
    )
    # One runtime, built here and handed to both halves of the application: the
    # factory every route reaches Master through, and the state the
    # administrator's screen asks how the harness is doing. Building it twice
    # would be two pools, and the second one would start runtimes the first
    # could not see.
    runtime = HarnessRuntime(settings=resolved)

    app.state.ravel = GatewayState(
        settings=resolved,
        database=resolved_database,
        tokens=tokens,
        master_of=master_of or runtime.master_of,
        runtime=runtime,
        deliver_external=deliver_external
        or ExecutionService(database=resolved_database, settings=resolved),
        cadence=cadence,
        limiter=limiter or LoginRateLimiter(),
        trusted_proxies=TrustedProxies(resolved.gateway_trusted_proxies),
        allowed_origins=exact_origins(resolved),
        stream_reauthorize_seconds=resolved.gateway_stream_reauthorize_seconds,
    )

    install_error_handlers(app)
    # After the legacy mapping, and path-aware: `/v1` gets the envelope
    # `CONTRACTS.md` §8 requires, every other path gets exactly the answer it
    # got before this module existed.
    install_v1_error_handlers(app)

    @app.middleware("http")
    async def request_identifier(request: Request, call_next: Any) -> Response:
        """Give every request an identifier, and answer with it.

        A caller's own identifier is echoed when it has a safe shape, so one
        action can be followed across a proxy; otherwise the server chooses
        one. The value is bounded and character-checked because it reaches a
        response header and a log line, and neither should accept caller text
        unexamined.
        """
        presented = request.headers.get("x-request-id")
        request.state.request_id = (
            presented if presented and REQUEST_ID.fullmatch(presented) else uuid4().hex
        )
        response: Response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    app.state.event_retention = event_retention

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(control.router)
    app.include_router(members.router)
    app.include_router(conversation.router)
    app.include_router(events.router)
    app.include_router(artifacts.router)
    app.include_router(lab.router)
    app.include_router(admin.router)
    app.include_router(v1_router)

    @app.get("/healthz", tags=["meta"], summary="Whether this process is answering")
    def healthz() -> dict[str, str]:
        """Liveness only, and deliberately uninformative.

        An unauthenticated caller learns that something is listening and
        nothing else — not which version, not whether the database is up, not
        whether any project exists. Anything more would be reachable by anyone
        who can open a socket.
        """
        return {"status": "ok"}

    return app


def _lifespan(
    database: Database, settings: Settings, heartbeat_seconds: float | None
) -> Any:
    """What the Gateway does between accepting its first request and its last.

    Three things, and the order is the point on both ends.

    **Up.** Nothing: a beat is written on the first pulse, which `Heartbeat
    .start` does immediately rather than after one interval, so the row appears
    as the server becomes ready rather than a minute later. What is *not* done
    here is touching the harness, the queue or the object store — a Gateway
    that connected to everything it might need would make a service that is
    only serving reads depend on all of them being up.

    **Down.** The pulse stops and the row records that this process stopped,
    before the database is disposed. A `SIGTERM` from a service manager reaches
    this path, which is what makes a deploy legible as a deploy: the row says
    the Gateway was shut down rather than leaving a last beat that recedes.

    A `heartbeat_seconds` of `None` yields a lifespan that does nothing at all,
    which is what a caller with no database to report to asks for. The
    function still exists, because an application that installed no lifespan
    handler and one whose handler does nothing are the same to uvicorn and
    different to anybody reading this file.
    """
    heartbeat: Heartbeat | None = None
    if heartbeat_seconds is not None:
        heartbeat = Heartbeat(
            database=database,
            service=GATEWAY,
            detail=lambda: {
                "api_version": API_VERSION,
                "address": f"{settings.gateway_host}:{settings.gateway_port}",
            },
            pulse_seconds=heartbeat_seconds,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
        if heartbeat is not None:
            heartbeat.start()
        try:
            yield
        finally:
            if heartbeat is not None:
                await heartbeat.stop()

    return lifespan


__all__ = ["API_VERSION", "GATEWAY_PULSE_SECONDS", "TITLE", "create_app"]
