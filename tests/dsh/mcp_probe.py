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
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

#: How long to wait for a tool server to answer before calling it wedged.
PROBE_TIMEOUT_SECONDS = 30.0


@dataclass(frozen=True, slots=True)
class ToolCall:
    """What one `tools/call` produced, verbatim.

    `error` is the text the model would read. It is kept as text rather than as
    an exception because that is the whole question these tests ask: a refusal
    that reaches the model is a message, and a crash is a message too — the
    difference is whether the message explains anything.
    """

    tool: str
    failed: bool
    payload: dict[str, Any] | None
    error: str | None


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """What a tool server exposed to a caller in one scope."""

    tools: tuple[str, ...]
    whoami: dict[str, Any] | None
    calls: tuple[ToolCall, ...] = ()


#: A call's arguments, or a function of the calls made before it.
#:
#: Some tools take a value only the previous tool can produce: `register_source`
#: needs the reference `open_source` issued, and `record_evidence` needs the
#: identifier `register_source` returned. Those references are held in the
#: server's own memory and are deliberately not derivable from the node id, so
#: a test that has to pass one must read it off the earlier reply — and it must
#: do so *within one server process*, because that is the only thing that holds
#: them. Hence a callable: the sequence still runs in one session, and the
#: dependency between two calls is stated where the calls are.
type CallArguments = dict[str, Any] | Callable[[tuple[ToolCall, ...]], dict[str, Any]]


async def probe(
    env: dict[str, str],
    *,
    call_whoami: bool = True,
    calls: tuple[tuple[str, CallArguments], ...] = (),
) -> ProbeResult:
    """Launch a tool server with `env` and report what it exposed.

    `calls` are made after listing, in order, and their outcomes are reported
    rather than raised: a test asserting that a role *cannot* do something is
    asserting about the refusal, so the refusal has to survive to the assertion.
    """
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

        outcomes = []
        for name, arguments in calls:
            resolved = arguments(tuple(outcomes)) if callable(arguments) else arguments
            try:
                result = await session.call_tool(name, resolved)
            except Exception as exc:  # the transport refused to carry the call
                outcomes.append(
                    ToolCall(
                        tool=name,
                        failed=True,
                        payload=None,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                )
                continue
            failed = bool(getattr(result, "is_error", getattr(result, "isError", False)))
            outcomes.append(
                ToolCall(
                    tool=name,
                    failed=failed,
                    payload=None if failed else _structured(result),
                    error=_text(result) if failed else None,
                )
            )
        return ProbeResult(tools=names, whoami=whoami, calls=tuple(outcomes))


def _text(result: Any) -> str:
    """The text blocks of a tool result, joined."""
    blocks = getattr(result, "content", ()) or ()
    return " ".join(
        text for text in (getattr(block, "text", None) for block in blocks) if text
    )


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
