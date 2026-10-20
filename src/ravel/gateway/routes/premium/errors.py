"""One error shape for `/v1`, and the legacy shape everywhere else.

`CONTRACTS.md` §8 requires every v1 error to be
``{code, message, request_id, details}``. The handlers here are installed on
the application *after* the legacy mapping and check the request path. A v1
request gets the envelope; a request to a legacy route gets exactly the answer
it got before this module existed. That split is deliberate: the TUI is a
client of the legacy routes, and making its refusals change shape to satisfy a
contract written for the versioned surface would break the compatibility this
task is required to keep.

**Nothing secret is echoed.** Validation errors are reduced to their location,
message and type: pydantic's raw error carries the submitted input, and a login
body's input is a password. The same rule applies to the domain's own
`ValidationError`: its errors are sanitized before they become `details`.
"""

from __future__ import annotations

import logging
from typing import Any, cast

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.exception_handlers import (
    http_exception_handler,
    request_validation_exception_handler,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from ravel.domain.actions import ActionError
from ravel.domain.state_machines import TransitionError
from ravel.state.repositories.base import NotFound, ProjectScopeError

logger = logging.getLogger(__name__)

#: The generic code that belongs to each status, for refusals raised with a
#: plain message. A route that needs a more specific code — `STALE_VERSION`,
#: `APPROVAL_REQUIRED`, `UNSUPPORTED` — raises `problem()` instead.
_CODE_BY_STATUS: dict[int, str] = {
    status.HTTP_400_BAD_REQUEST: "INVALID_REQUEST",
    status.HTTP_401_UNAUTHORIZED: "UNAUTHENTICATED",
    status.HTTP_403_FORBIDDEN: "FORBIDDEN",
    status.HTTP_404_NOT_FOUND: "NOT_FOUND",
    status.HTTP_409_CONFLICT: "CONFLICT",
    status.HTTP_410_GONE: "GONE",
    status.HTTP_413_CONTENT_TOO_LARGE: "PAYLOAD_TOO_LARGE",
    status.HTTP_422_UNPROCESSABLE_ENTITY: "INVALID_REQUEST",
    status.HTTP_429_TOO_MANY_REQUESTS: "RATE_LIMITED",
    status.HTTP_501_NOT_IMPLEMENTED: "UNSUPPORTED",
    status.HTTP_502_BAD_GATEWAY: "UPSTREAM_FAILED",
    status.HTTP_503_SERVICE_UNAVAILABLE: "UNAVAILABLE",
}


def request_id_of(request: Request) -> str:
    """This request's identifier, or one generated for an error raised early."""
    from uuid import uuid4

    found = getattr(request.state, "request_id", None)
    return str(found) if found else uuid4().hex


def problem(
    status_code: int,
    code: str,
    message: str,
    *,
    details: dict[str, Any] | None = None,
) -> HTTPException:
    """An HTTP refusal carrying the v1 code the route chose.

    Returned rather than raised so the call site reads as a value, and shaped as
    a dict because `HTTPException` has nowhere else to carry the code without
    registering a new exception type for every refusal.
    """
    return HTTPException(
        status_code=status_code,
        detail={"code": code, "message": message, "details": details or {}},
    )


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> JSONResponse:
    identifier = request_id_of(request)
    return JSONResponse(
        status_code=status_code,
        content={
            "code": code,
            "message": message,
            "request_id": identifier,
            "details": details or {},
        },
        headers={"X-Request-ID": identifier},
    )


def _is_v1(request: Request) -> bool:
    path = request.url.path
    return path == "/v1" or path.startswith("/v1/")


def _plain_errors(error: ValidationError | RequestValidationError) -> list[dict[str, Any]]:
    """Validation errors with the submitted values removed.

    `input` and `ctx` are what pydantic adds to explain a failure by quoting
    what failed. On this API the input may be a password or a bearer token, and
    an error envelope is the last place either should be written down.

    FastAPI's `RequestValidationError` carries a different `errors()` signature
    in this version — it takes no `include_*` keywords — while a domain
    `ValidationError` does. The union is real, so the two are asked in the way
    each answers; only `loc`, `msg`, and `type` are kept either way, which is
    what makes the difference invisible in the response.
    """
    if isinstance(error, RequestValidationError):
        items = error.errors()
    else:
        items = error.errors(include_url=False, include_context=False)
    return [
        {
            "loc": [str(part) for part in item.get("loc", ())],
            "message": str(item.get("msg", "")),
            "type": str(item.get("type", "")),
        }
        for item in items
    ]


def _action_status(error: ActionError) -> tuple[int, str]:
    """The status and code an Action refusal carries.

    The domain's own `code` attribute is authoritative: it is the vocabulary
    `CONTRACTS.md` §8 lists (`STALE_VERSION`, `APPROVAL_REQUIRED`,
    `BUDGET_EXCEEDED`, `UNKNOWN_EXTERNAL_RESULT`, `UNSUPPORTED`). The base
    class's `ACTION_REFUSED` is not one of those, so it becomes `CONFLICT` —
    the refusal is still a conflict with the Action's state rather than a
    malformed request.
    """
    code = getattr(error, "code", "ACTION_REFUSED")
    return status.HTTP_409_CONFLICT, ("CONFLICT" if code == "ACTION_REFUSED" else code)


def install_v1_error_handlers(app: FastAPI) -> None:
    """Install the v1 envelope, preserving legacy behaviour for legacy paths."""

    @app.exception_handler(HTTPException)
    async def _http(request: Request, error: HTTPException) -> Any:
        if not _is_v1(request):
            return await http_exception_handler(request, error)
        detail = error.detail
        if isinstance(detail, dict) and "code" in detail:
            # FastAPI accepts any detail; Starlette's stub types it as text, so
            # the isinstance narrows to an intersection whose `str` half would
            # decide `__getitem__`. The cast selects the dict half, which is the
            # shape `problem()` puts here and the branch above has just checked.
            payload = cast("dict[str, Any]", detail)
            return _envelope(
                request,
                status_code=error.status_code,
                code=str(payload["code"]),
                message=str(payload.get("message", payload["code"])),
                details=(
                    payload.get("details")
                    if isinstance(payload.get("details"), dict)
                    else {}
                ),
            )
        code = _CODE_BY_STATUS.get(error.status_code, "REFUSED")
        return _envelope(
            request,
            status_code=error.status_code,
            code=code,
            message=str(detail),
            details={},
        )

    @app.exception_handler(RequestValidationError)
    async def _body(request: Request, error: RequestValidationError) -> Any:
        if not _is_v1(request):
            return await request_validation_exception_handler(request, error)
        return _envelope(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="INVALID_REQUEST",
            message="the request body or parameters are not valid",
            details={"errors": _plain_errors(error)},
        )

    @app.exception_handler(NotFound)
    async def _absent(request: Request, error: NotFound) -> Any:
        if not _is_v1(request):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(error)}
            )
        return _envelope(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            code="NOT_FOUND",
            message=str(error),
        )

    @app.exception_handler(ProjectScopeError)
    async def _foreign(request: Request, error: ProjectScopeError) -> Any:
        if not _is_v1(request):
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content={"detail": "no such record in this project"},
            )
        return _envelope(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            code="NOT_FOUND",
            message="no such record in this project",
        )

    @app.exception_handler(TransitionError)
    async def _illegal_move(request: Request, error: TransitionError) -> Any:
        if not _is_v1(request):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT, content={"detail": str(error)}
            )
        return _envelope(
            request,
            status_code=status.HTTP_409_CONFLICT,
            code="CONFLICT",
            message=str(error),
        )

    @app.exception_handler(PermissionError)
    async def _forbidden(request: Request, error: PermissionError) -> Any:
        if not _is_v1(request):
            return JSONResponse(
                status_code=status.HTTP_403_FORBIDDEN, content={"detail": str(error)}
            )
        return _envelope(
            request,
            status_code=status.HTTP_403_FORBIDDEN,
            code="FORBIDDEN",
            message=str(error),
        )

    @app.exception_handler(ValidationError)
    async def _malformed(request: Request, error: ValidationError) -> Any:
        if not _is_v1(request):
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={
                    "detail": error.errors(include_url=False, include_context=False)
                },
            )
        return _envelope(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            code="INVALID_REQUEST",
            message="a value in the request was rejected by the domain",
            details={"errors": _plain_errors(error)},
        )

    @app.exception_handler(IntegrityError)
    async def _conflict(request: Request, error: IntegrityError) -> Any:
        logger.warning("a write conflicted with a constraint: %s", error)
        if not _is_v1(request):
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"detail": "the request conflicted with existing state"},
            )
        return _envelope(
            request,
            status_code=status.HTTP_409_CONFLICT,
            code="CONFLICT",
            message="the request conflicted with existing state",
        )

    @app.exception_handler(ActionError)
    async def _action(request: Request, error: ActionError) -> Any:
        code_status, code = _action_status(error)
        if not _is_v1(request):
            return JSONResponse(status_code=code_status, content={"detail": str(error)})
        return _envelope(
            request,
            status_code=code_status,
            code=code,
            message=str(error),
            details={"error": type(error).__name__},
        )


__all__ = ["install_v1_error_handlers", "problem", "request_id_of"]
