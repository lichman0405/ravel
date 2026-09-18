"""Drive a RAVEL tool server over real MCP stdio, outside any agent.

Asking a model whether a tool exists is a question about the model. Asking the
server is a question about RAVEL. This module does the latter: it launches the
same `python -m ravel.mcp.server` the harness launches, over the same transport,
with the same environment, and reports what the server actually registered.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

#: How long to wait for a tool server to answer before calling it wedged.
PROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What a tool server exposed to a caller in one scope."""

    tools: tuple[str, ...]
    whoami: dict[str, Any] | None


async def probe(env: dict[str, str], *, call_whoami: bool = True) -> ProbeResult:
    """Launch a tool server with `env` and report the tools it registered."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ravel.mcp.server"],
        env={**os.environ, **env},
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        listing = await session.list_tools()
        names = tuple(tool.name for tool in listing.tools)

        whoami: dict[str, Any] | None = None
        if call_whoami and "whoami" in names:
            result = await session.call_tool("whoami", {})
            whoami = _structured(result)
        return ProbeResult(tools=names, whoami=whoami)


def start_server(env: dict[str, str]) -> tuple[int, str]:
    """Start a tool server with `env` and return its (exit code, stderr).

    Used for the refusal path: a server launched without a usable scope must
    exit rather than serve. `stdio_client` would report that as a broken pipe,
    which says less than the process's own message does.
    """
    completed = subprocess.run(
        [sys.executable, "-m", "ravel.mcp.server"],
        env={**os.environ, **env},
        input="",
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    return completed.returncode, completed.stderr.strip()


def _structured(result: Any) -> dict[str, Any] | None:
    """The tool result's structured payload, if the SDK surfaced one."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for block in getattr(result, "content", ()) or ():
        text = getattr(block, "text", None)
        if text:
            import json

            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return None
