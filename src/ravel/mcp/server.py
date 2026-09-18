"""RAVEL tool server over MCP stdio.

One process serves one `(project, role)`. It reads that scope from its
environment at startup, registers exactly the tools the role's roster grants,
and serves the harness over stdio.

    python -m ravel.mcp.server

The harness spawns this module itself, through the `@deepseek-ai/dsh-mcp-client`
row that `ravel.dsh.composition` writes into the runtime's overlay. It is not
meant to be run by hand except when debugging a role's roster.

**Registration is the authorization.** A tool a role does not hold is not
merely refused when called — it is never registered, so it does not appear in
the model's tool list and there is no handler to reach. The refusal path exists
as well, inside the handlers, but the gate is this one: the roster that reaches
the model is the roster RAVEL granted.

**Registration is also where refusals are made legible.** An exception that
escapes a handler reaches the model as `Error executing tool <name>` and
nothing else, so every handler is wrapped by `report_refusals` on the way in.
Doing it here rather than in each handler means a new tool cannot be added
without it — see `ravel.mcp.errors` for where the line is drawn between a
failure the model should read and a defect that should crash.
"""

from __future__ import annotations

import sys

from mcp.server.mcpserver import MCPServer

from ravel import __version__
from ravel.mcp.context import ToolContext
from ravel.mcp.errors import report_refusals
from ravel.mcp.registry import TOOL_DESCRIPTIONS, tools_for
from ravel.mcp.scope import ScopeError
from ravel.mcp.tools import IMPLEMENTATIONS

__all__ = ["IMPLEMENTATIONS", "create_server", "main"]


def create_server(context: ToolContext) -> MCPServer:
    """Build the server for one scope, registering only that role's tools.

    Raises:
        ScopeError: A tool is granted to this role but has no implementation,
            which is a wiring fault in the registry rather than a runtime
            condition to recover from.
    """
    scope = context.scope
    server = MCPServer(
        name="ravel",
        title="RAVEL",
        version=__version__,
        instructions=(
            f"You serve project {scope.project_id} as {scope.role.display_name}. "
            "Act only through the tools provided, and stay within your role's contract."
        ),
    )

    for name in tools_for(scope.role):
        factory = IMPLEMENTATIONS.get(name)
        if factory is None:
            raise ScopeError(
                f"tool {name!r} is granted to {scope.role.value} but has no implementation"
            )
        # Every handler is registered through `report_refusals`. The transport
        # would otherwise collapse an anticipated refusal into "Error executing
        # tool <name>", and the reason the model needs in order to try
        # something else would stop at the server's log.
        handler = report_refusals(factory(context))
        server.tool(name=name, description=TOOL_DESCRIPTIONS[name])(handler)

    return server


def main() -> int:
    """Serve this role's tools over stdio until the harness closes the pipe."""
    try:
        context = ToolContext.from_environment()
    except ScopeError as exc:
        print(f"ravel tool server: {exc}", file=sys.stderr)
        return 2

    # Validate the brief before serving. A brief that contradicts this
    # process's scope is a wiring fault; failing now is louder and cheaper than
    # failing inside an agent's first tool call.
    try:
        context.scope.brief()
    except ScopeError as exc:
        print(f"ravel tool server: {exc}", file=sys.stderr)
        return 2

    server = create_server(context)
    try:
        server.run(transport="stdio")
    finally:
        context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
