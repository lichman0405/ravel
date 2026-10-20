"""The versioned API surface: one router, mounted at `/v1`.

`CONTRACTS.md` §8 lists the routes. They are assembled here so that
`create_app` adds one object and so that a reader can see the whole versioned
surface in one place. Every module behind this one follows the same rule: the
legacy route stays and keeps the TUI working; the v1 route either reads the
same service the legacy route reads or calls the legacy handler itself. No v1
route writes the Scientific DAG, and the tests assert that against the OpenAPI
schema as well as by reading back a DAG after probing every path.

WebSocket routes are included too. FastAPI will not put them in the OpenAPI
document — that is a property of the schema, not of this mount — but they are
served beside the HTTP routes under the same prefix and checked by the same
authentication functions.
"""

from __future__ import annotations

from fastapi import APIRouter

from ravel.gateway.routes.premium import (
    actions,
    auth,
    control,
    conversation,
    events,
    lab,
    mail,
    projects,
    research,
)

router = APIRouter(prefix="/v1")
router.include_router(auth.router)
router.include_router(projects.router)
router.include_router(conversation.router)
router.include_router(research.router)
router.include_router(actions.router)
router.include_router(control.router)
router.include_router(lab.router)
router.include_router(mail.router)
router.include_router(events.router)

__all__ = ["router"]
