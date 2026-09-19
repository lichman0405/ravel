"""Enumerating what an application actually serves, which is not `app.routes`.

Under FastAPI 0.141 a route added by `include_router` is not appended to
`app.routes` as an `APIRoute`. It is appended as a lazy wrapper that resolves
its children on demand, so `app.routes` is four documentation routes, one
wrapper per included router, and whatever was declared on the application
itself. A loop over `app.routes` looking for `route.path` finds the docs and the
health check and nothing else.

That is worth a module of its own because of how it fails. The probe in
`tests/integration/gateway/test_projects.py` exists to assert that no route lets
a user change the Scientific DAG, and it was written to enumerate the routes
from the application so that a route added later would be probed without anyone
remembering to add it. It iterated `app.routes`, found nothing with a path, made
no requests, and passed — for as long as it existed. A test that checks nothing
reports the same colour as a test that checks everything, and the only thing
that distinguishes them is whether anybody looked.

Two consequences for what follows. The wrapper is detected by its shape —
something with children to resolve and no path of its own — rather than by
importing the private class, because a private name that moves would take this
module back to finding nothing, which is the bug. And callers assert on how many
routes they got, because the floor is what turns "found nothing" from a pass
into a failure. See `EXPECTED_ROUTE_FLOOR`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI

#: Fewer routes than this means the enumeration has broken again rather than
#: that the application shrank. The Gateway declares thirty-odd; a change that
#: removed half of them would be a deliberate rewrite with a test to update, and
#: this number being wrong is a much louder failure than a probe that silently
#: examines nothing. Raise it when routes are added; never lower it to make a
#: test pass.
EXPECTED_ROUTE_FLOOR = 30


@dataclass(frozen=True, slots=True)
class ProbedRoute:
    """One thing the application serves, as a caller can reach it."""

    path: str
    #: Empty for a WebSocket, which has no method — it is a route a client
    #: *connects* to, and a probe that sent it an HTTP verb would be probing a
    #: route that does not exist.
    methods: frozenset[str]
    websocket: bool

    @property
    def is_http(self) -> bool:
        return not self.websocket


def _original(route: Any) -> Any:
    """The declared route behind a wrapper, or the route itself."""
    return getattr(route, "original_route", None) or route


def _children(route: Any) -> list[Any] | None:
    """What this route resolves to, if it is a wrapper rather than a route.

    `None` means "this is a route". The test is shape-based on purpose: a
    wrapper has children to resolve and no path of its own, and asking it that
    way survives the private class being renamed.
    """
    if getattr(route, "path", None):
        return None
    resolve: Callable[[], Iterable[Any]] | None = getattr(
        route, "effective_candidates", None
    )
    if not callable(resolve):
        return None
    return list(resolve())


def _path_of(route: Any) -> str:
    """A route's full path, including any prefix the inclusion added."""
    own = getattr(route, "path", "") or ""
    prefix = getattr(route, "frontend_prefix", "") or ""
    return f"{prefix}{own or getattr(_original(route), 'path', '')}"


def _methods_of(route: Any) -> frozenset[str]:
    found = getattr(route, "methods", None)
    if not found:
        found = getattr(_original(route), "methods", None)
    return frozenset(found or ())


def _is_websocket(route: Any) -> bool:
    for candidate in (route, _original(route)):
        if type(candidate).__name__ == "APIWebSocketRoute":
            return True
    return False


def effective_routes(app: FastAPI) -> list[ProbedRoute]:
    """Everything the application serves, with inclusion wrappers flattened.

    Returns the routes a real client can reach, in declaration order. Nesting is
    walked rather than assumed away: a router that included another router would
    otherwise contribute a wrapper whose children were never visited.
    """
    found: list[ProbedRoute] = []

    def walk(routes: list[Any]) -> None:
        for route in routes:
            children = _children(route)
            if children is not None:
                walk(children)
                continue
            path = _path_of(route)
            if not path:
                # Not a route a caller can reach — Starlette's `Mount` and the
                # lifespan wrapper land here, and so would anything else with no
                # path of its own. Skipped rather than guessed at, and the floor
                # the caller asserts is what keeps this from hiding the whole
                # application.
                continue
            found.append(
                ProbedRoute(
                    path=path,
                    methods=_methods_of(route),
                    websocket=_is_websocket(route),
                )
            )

    walk(list(app.routes))
    return found


def fill(path: str, **values: str) -> str:
    """A route path with its placeholders replaced, for probing.

    Leftover placeholders are the caller's problem and are deliberately left
    visible: a path still containing `{something}` is a route this helper did
    not know how to address, and a probe that quietly sent a literal `{x}` would
    be probing a different URL.
    """
    filled = path
    for name, value in values.items():
        filled = filled.replace(f"{{{name}}}", value)
    return filled


__all__ = ["EXPECTED_ROUTE_FLOOR", "ProbedRoute", "effective_routes", "fill"]
