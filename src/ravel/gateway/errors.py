"""Turning a domain refusal into a status code, once.

Without this, every route would wrap its body in the same `try` and pick the
same codes, and the first route written by somebody in a hurry would answer
500 for a state machine rejecting a move. The mapping is installed on the
application instead, so a route body reads as the thing it does.

**The mapping is deliberately lossy in one direction: nothing here becomes a
500 for a reason a caller could have caused.** A malformed request is 4xx even
when it arrives as a `ValueError` from deep inside the domain, because a
`ValueError` raised while parsing what somebody sent is a statement about what
they sent.

What is *not* mapped is as deliberate. An exception with no entry falls
through to the server error handler, which is the correct answer for a bug:
the alternative — a catch-all that renders any exception as 400 — would turn
every defect into a plausible-looking complaint about the caller.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from ravel.domain.state_machines import TransitionError
from ravel.state.repositories.base import NotFound, ProjectScopeError

logger = logging.getLogger(__name__)


def install_error_handlers(app: FastAPI) -> None:
    """Attach the domain-to-HTTP mapping to an application."""

    @app.exception_handler(NotFound)
    async def _absent(_request: Request, error: NotFound) -> JSONResponse:
        """404. A record this caller's project does not have.

        `ProjectScopeError` is answered the same way and by the same handler
        below, because a record in another project is indistinguishable from
        one that does not exist — which is the answer that leaks least.
        """
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)}
        )

    @app.exception_handler(ProjectScopeError)
    async def _foreign(_request: Request, error: ProjectScopeError) -> JSONResponse:
        """404, with the message replaced rather than passed through.

        The exception is raised inside the repository that refused, so its text
        names both the record's project and the one that asked. A caller has no
        business being told either, so they are told neither.
        """
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"detail": "no such record in this project"},
        )

    @app.exception_handler(TransitionError)
    async def _illegal_move(_request: Request, error: TransitionError) -> JSONResponse:
        """409. The request was well-formed and the state machine refused it."""
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)}
        )

    @app.exception_handler(PermissionError)
    async def _forbidden(_request: Request, error: PermissionError) -> JSONResponse:
        """403. The caller was identified and may not do this."""
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(error)}
        )

    @app.exception_handler(ValidationError)
    async def _malformed(_request: Request, error: ValidationError) -> JSONResponse:
        """422. A domain type refused a value that a route constructed.

        Request bodies are validated by FastAPI before a route runs and never
        reach this. What reaches it is a value built inside RAVEL — a duration,
        a budget, a contract — that the domain rejected, and the caller asked
        for it, so it is theirs to hear about.
        """
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={"detail": error.errors(include_url=False, include_context=False)},
        )

    @app.exception_handler(IntegrityError)
    async def _conflict(_request: Request, error: IntegrityError) -> JSONResponse:
        """409. A constraint refused the write.

        The constraint's own text names tables and columns, so it stays in the
        log. A caller learns that the request conflicted with something that
        already exists, which is as much as they need in order to stop.
        """
        logger.warning("a write conflicted with a constraint: %s", error)
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": "the request conflicted with existing state"},
        )


__all__ = ["install_error_handlers"]
