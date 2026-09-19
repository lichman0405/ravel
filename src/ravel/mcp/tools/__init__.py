"""The tool implementations, keyed by the name the model sees.

Each entry is a *factory*: it takes the context the server was launched with
and returns the handler that will be registered. The indirection is what keeps
a handler from reaching a database or a scope it was not given — there is no
module-level state to reach for instead.

The registry decides who may see a tool. This package only decides what the
tool does, and a name that appears in one and not the other is refused at
startup by `ravel.mcp.server` rather than discovered by an agent whose roster
advertised something that does not exist.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ravel.mcp.context import ToolContext
from ravel.mcp.tools import dag, execution, research, review, state

#: What a registered tool looks like: an async callable whose parameters are
#: the arguments the model supplies. Declared rather than inferred because the
#: signature belongs to the inner function, which is what the transport reads.
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]

#: A factory: given the process context, produce the handler to register.
ToolFactory = Callable[[ToolContext], ToolHandler]

IMPLEMENTATIONS: dict[str, ToolFactory] = {
    **state.IMPLEMENTATIONS,
    **dag.IMPLEMENTATIONS,
    **review.IMPLEMENTATIONS,
    **research.IMPLEMENTATIONS,
    **execution.IMPLEMENTATIONS,
}

__all__ = ["IMPLEMENTATIONS", "ToolFactory", "ToolHandler"]
