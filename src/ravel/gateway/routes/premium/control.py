"""The owner's controls on the versioned surface.

Nothing here decides anything. Pause, resume and the Authority Envelope are the
three things a person may change about a running project, and all three already
exist under `/projects`; these routes call the same handlers, so there is one
transition table, one envelope-versioning rule and one audit trail rather than
two that resemble each other.

A v1 path is added because `CONTRACTS.md` §8 names one and because a generated
client should not have to know that half the product is versioned and half is
not. It is deliberately not an opportunity to restate the rules: a second
implementation of "pause" would be a second answer to what pause means.
"""

from __future__ import annotations

from fastapi import APIRouter

from ravel.gateway.deps import (
    DirectorDep,
    GatewayStateDep,
    PrincipalDep,
)
from ravel.gateway.routes import control as legacy
from ravel.gateway.routes.control import EnvelopeRequest, PauseRequest

router = APIRouter(tags=["v1-control"])


@router.post(
    "/projects/{project_id}/pause",
    response_model=dict[str, str],
    summary="Stop the project where it stands",
)
def pause(
    standing: DirectorDep,
    state: GatewayStateDep,
    request: PauseRequest | None = None,
) -> dict[str, str]:
    """The same transition, recorded with the same reason and event."""
    return legacy.pause(standing, state, request)


@router.post(
    "/projects/{project_id}/resume",
    response_model=dict[str, str],
    summary="Let the project move on again",
)
def resume(standing: DirectorDep, state: GatewayStateDep) -> dict[str, str]:
    """The same transition; a project that is not paused is refused by it."""
    return legacy.resume(standing, state)


@router.get(
    "/projects/{project_id}/authority-envelope",
    response_model=dict[str, object] | None,
    summary="What Master may decide alone",
)
def read_envelope(
    standing: PrincipalDep, state: GatewayStateDep
) -> dict[str, object] | None:
    """The envelope in force, or `null` when none has been set.

    `null` is an ordinary state and not a 404, exactly as on the legacy route.
    """
    return legacy.read_envelope(standing, state)


@router.post(
    "/projects/{project_id}/authority-envelope",
    response_model=dict[str, object],
    summary="Append a new Authority Envelope version",
)
def write_envelope(
    standing: DirectorDep, state: GatewayStateDep, request: EnvelopeRequest
) -> dict[str, object]:
    """Append, never edit. The server chooses the version number."""
    return legacy.write_envelope(standing, state, request)


__all__ = ["router"]
