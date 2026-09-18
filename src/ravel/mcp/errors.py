"""How a refusal reaches the model.

An MCP tool call can fail in two ways and they are not equally useful. A
handler that raises `ToolError` produces a result flagged as an error whose
text the model reads. A handler that raises anything else is treated as a
crash: the model is told only `Error executing tool <name>`, and the sentence
explaining what was wrong with the request never leaves the server's log.

Every refusal RAVEL means a model to understand — a node outside the planning
horizon, a confidence that is not a level, a dependency that does not exist —
is therefore routed through `report_refusals`, which converts the domain's own
exceptions into `ToolError` with their message intact.

The line is drawn at *anticipated* failures: the ones `REFUSALS` names are
conditions the domain has words for and a caller can act on. Anything else — a
`KeyError`, an `AttributeError`, a database error — is a defect in RAVEL, and
is left to crash loudly rather than dressed up as an explanation the model
would believe and act on.
"""

from __future__ import annotations

import functools
from typing import Any

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import ValidationError

from ravel.mcp.scope import ScopeError
from ravel.state.repositories.base import NotFound, ProjectScopeError

#: The failures a tool call may report to the model. Each entry names a
#: condition the domain already has a vocabulary for:
#:
#: - `ScopeError` — this process was launched with an unusable authority scope.
#: - `ProjectScopeError` — the record belongs to another project.
#: - `PermissionError` — the role does not hold the authority the call needs.
#: - `NotFound` — the node, contract, or record named does not exist.
#: - `ValueError` — the arguments do not describe a valid request, a domain
#:   invariant refuses it, or (`TransitionError`, `HorizonError`, pydantic's
#:   own `ValidationError`, all of which subclass it) the step is illegal.
REFUSALS: tuple[type[BaseException], ...] = (
    ScopeError,
    ProjectScopeError,
    NotFound,
    PermissionError,
    ValueError,
)


def _message(exc: BaseException) -> str:
    """The sentence the model reads.

    Pydantic's `str()` spans a dozen lines of boxed detail written for a
    developer reading a terminal. A model acting on the refusal needs the list
    of what was wrong, so validation errors are flattened to one clause per
    field and everything else is passed through as it was written.
    """
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(part) for part in error['loc']) or '<model>'}: {error['msg']}"
            for error in exc.errors()
        )
    return str(exc) or type(exc).__name__


def report_refusals(handler: Any) -> Any:
    """Wrap a tool handler so its anticipated failures reach the model.

    Applied at the registration site rather than inside each handler, so that
    a handler cannot forget it: the roster and the reporting are two decisions
    made once, in `ravel.mcp.server`.

    `functools.wraps` is load-bearing here. The transport derives each tool's
    input schema from the handler's signature, so a wrapper that replaced the
    signature with `(**arguments)` would advertise a tool that takes no
    arguments at all.
    """

    @functools.wraps(handler)
    async def reported(**arguments: Any) -> dict[str, Any]:
        try:
            return await handler(**arguments)
        except ToolError:
            # Already a reported failure; wrapping it again would only add a
            # layer to a message that is on its way to the model intact.
            raise
        except REFUSALS as exc:
            raise ToolError(_message(exc)) from exc

    return reported
