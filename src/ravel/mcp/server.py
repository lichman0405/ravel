"""RAVEL tool server over MCP stdio.

One process serves one `(project, role)`. It reads that scope from its
environment at startup, registers exactly the tools the role's roster grants,
and serves the harness over stdio.

    python -m ravel.mcp.server

The harness spawns this module itself, through the `@deepseek-ai/dsh-mcp-client`
row that `ravel.dsh.composition` writes into the runtime's overlay. It is not
meant to be run by hand except when debugging a role's roster.
"""

from __future__ import annotations

import sys
from typing import Any

from mcp.server.mcpserver import MCPServer

from ravel import __version__
from ravel.mcp.registry import TOOL_DESCRIPTIONS, tools_for
from ravel.mcp.scope import ScopeError, ToolScope

_scope: ToolScope | None = None


def _active_scope() -> ToolScope:
    """The scope this process was launched with.

    Raises:
        ScopeError: Called before `main` established the scope.
    """
    if _scope is None:
        raise ScopeError("tool scope is not established; the server was not started correctly")
    return _scope


# ── Tools ───────────────────────────────────────────────────────────────────
#
# Each tool declares its own parameters so the schema the model receives is
# derived from a real signature. The scope is never a parameter: it comes from
# the process, which is the only reason the model cannot forge it.


async def whoami() -> dict[str, Any]:
    """Report this session's project, role, and available tools."""
    scope = _active_scope()
    return {
        "project_id": scope.project_id,
        "role": scope.role.value,
        "role_name": scope.role.display_name,
        "tools": list(tools_for(scope.role)),
        "may_mutate_dag": scope.is_master,
    }


async def read_project_state() -> dict[str, Any]:
    """Read the authoritative project state for this scope."""
    scope = _active_scope()
    brief = scope.brief()
    return {
        "project_id": brief.project_id,
        "project_title": brief.project_title,
        "objective": brief.objective,
        "phase": brief.phase,
        "bindings": list(brief.bindings),
    }


#: Tool name -> implementation. Registration is driven by `TOOL_DESCRIPTIONS`
#: and `registry.TOOL_ROLES`, so a tool missing here fails at startup rather
#: than silently vanishing from a role's roster.
IMPLEMENTATIONS: dict[str, Any] = {
    "whoami": whoami,
    "read_project_state": read_project_state,
}


def create_server(scope: ToolScope) -> MCPServer:
    """Build the server for one scope, registering only that role's tools."""
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
        implementation = IMPLEMENTATIONS.get(name)
        if implementation is None:
            raise ScopeError(
                f"tool {name!r} is granted to {scope.role.value} but has no implementation"
            )
        server.tool(name=name, description=TOOL_DESCRIPTIONS[name])(implementation)

    return server


def main() -> int:
    """Serve this role's tools over stdio until the harness closes the pipe."""
    global _scope

    try:
        scope = ToolScope.from_environment()
    except ScopeError as exc:
        print(f"ravel tool server: {exc}", file=sys.stderr)
        return 2

    # Validate the brief before serving. A brief that contradicts this
    # process's scope is a wiring fault; failing now is louder and cheaper than
    # failing inside an agent's first tool call.
    try:
        scope.brief()
    except ScopeError as exc:
        print(f"ravel tool server: {exc}", file=sys.stderr)
        return 2

    _scope = scope
    server = create_server(scope)
    server.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
