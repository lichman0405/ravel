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

from fastapi import FastAPI

from ravel.config import Settings, get_settings
from ravel.gateway.auth.tokens import TokenService, require_a_real_secret
from ravel.gateway.conversation import MasterFactory, harness_master
from ravel.gateway.deps import GatewayState
from ravel.gateway.errors import install_error_handlers
from ravel.gateway.routes import auth, control, conversation, projects
from ravel.state.database import Database

#: What the Gateway calls itself. Versioned because a TUI written against V0
#: has to be able to notice that it is talking to something else.
TITLE = "RAVEL Research Gateway"
API_VERSION = "0.1.0"


def create_app(
    *,
    settings: Settings | None = None,
    database: Database | None = None,
    tokens: TokenService | None = None,
    master_of: MasterFactory | None = None,
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
    )
    app.state.ravel = GatewayState(
        settings=resolved,
        database=resolved_database,
        tokens=tokens,
        master_of=master_of or harness_master(resolved),
    )

    install_error_handlers(app)

    app.include_router(auth.router)
    app.include_router(projects.router)
    app.include_router(control.router)
    app.include_router(conversation.router)

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


__all__ = ["API_VERSION", "TITLE", "create_app"]
