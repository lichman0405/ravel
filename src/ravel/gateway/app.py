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

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI

from ravel.config import Settings, get_settings
from ravel.domain.services import GATEWAY
from ravel.execution.node_runs import ExecutionService, ExternalResultPort
from ravel.gateway.auth.tokens import TokenService, require_a_real_secret
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


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    tokens: TokenService | None = None,
    master_of: MasterFactory | None = None,
    deliver_external: ExternalResultPort | None = None,
    cadence: Cadence | None = None,
    heartbeat_seconds: float | None = GATEWAY_PULSE_SECONDS,
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
        heartbeat_seconds: How often this process reports itself alive, or
            `None` to have it report nothing. On by default, because a Gateway
            that did not report would be a service the administrator's screen
            could not tell from a dead one — and off only for a caller that has
            no liveness row to write to, which is a test holding a stub in place
            of a database rather than a deployment.

    Raises:
        RuntimeError: In production, the configured token secret is the
            development placeholder or shorter than the algorithm requires.
            Checked here rather than at first login so that a deployment which
            would issue forgeable tokens fails to start instead of running.
    """
    resolved = settings or get_settings()
    resolved_database = database or Database.from_settings(resolved)

    if tokens is None:
        secret = resolved.gateway_jwt_secret.get_secret_value()
        require_a_real_secret(secret, environment=resolved.env)
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
    )

    install_error_handlers(app)

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(control.router)
    app.include_router(members.router)
    app.include_router(conversation.router)
    app.include_router(events.router)
    app.include_router(artifacts.router)
    app.include_router(lab.router)
    app.include_router(admin.router)

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
