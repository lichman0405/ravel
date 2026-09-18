"""RAVEL tool servers, exposed to DeepSeek Harness agents over MCP.

The pinned harness carries no tool-registration surface on its SDK wire
protocol, so MCP over stdio is the supported seam. One server process serves
one `(project, role)` pair; the server learns its authority from the
environment RAVEL launches it with, never from the model.
"""

from ravel.mcp.scope import ToolScope

__all__ = ["ToolScope"]
