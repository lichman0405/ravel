"""What a tool handler is handed: its authority, and the state behind it.

Two things a handler must never take from its caller: the project it is serving
and the role it is serving as. Both come from this process's environment, fixed
before any agent existed, which is why neither appears in a tool's parameters.

The database is opened here rather than per call. A tool server serves one
project for the life of the session, so a connection per call would pay for
setup on every question an agent asks; the engine's pool is what makes the
handlers cheap.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from ravel.domain.roles import AgentRole
from ravel.mcp.scope import ToolScope
from ravel.state.database import Database


@dataclass(frozen=True, slots=True)
class ToolContext:
    """The scope a tool server was launched with, and the database it reads.

    Held in one object so a handler cannot reach the database without also
    holding the scope that says which project's rows it may touch.
    """

    scope: ToolScope
    database: Database

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> ToolContext:
        """Build the context this process was launched with.

        Raises:
            ScopeError: The scope environment is missing or names an unknown role.
        """
        return cls(
            scope=ToolScope.from_environment(environ),
            database=Database.from_settings(),
        )

    @property
    def project_id(self) -> str:
        """The one project every handler in this process is confined to."""
        return self.scope.project_id

    @property
    def role(self) -> AgentRole:
        """The role every handler in this process acts as."""
        return self.scope.role

    def read(self) -> AbstractContextManager[Session]:
        """A read-only session.

        Read-only is not a formality: a handler that needs to write takes
        `write()`, so an accidental write in a query path fails loudly instead
        of silently committing.
        """
        return self.database.read_only()

    def write(self) -> AbstractContextManager[Session]:
        """A transaction that commits when the handler returns.

        One tool call is one transaction. A mutation that raises part way
        through therefore leaves nothing behind, which is what makes an
        agent's failed attempt to change the plan a no-op rather than a
        half-applied one.
        """
        return self.database.transaction()

    def close(self) -> None:
        """Release the connection pool."""
        self.database.dispose()


def require_role(context: ToolContext, tool: str, *roles: AgentRole) -> None:
    """Refuse a role-scoped tool to a session serving anything else.

    The roster already keeps a tool out of the wrong server, so this is the
    second lock on the same door: a handler that is reachable only because
    someone mis-edited the registry still refuses to act. The check is on the
    process scope, not on an argument, so no caller can talk around it.

    Takes the roles rather than one role because one tool is legitimately held
    by two: the two Workers ask their contracts the same question, and the
    answer is the same for both.

    Raises:
        PermissionError: The scope's role is not one of these.
    """
    if context.role not in roles:
        permitted = " or ".join(role.display_name for role in roles)
        raise PermissionError(
            f"{tool} is a {permitted} tool; this session serves "
            f"{context.role.value} in {context.project_id}"
        )


def require_master(context: ToolContext, tool: str) -> None:
    """Refuse a Master-only tool to anything else.

    Raises:
        PermissionError: The scope's role is not Master.
    """
    require_role(context, tool, AgentRole.MASTER)


def require_review(context: ToolContext, tool: str) -> None:
    """Refuse a Review-only tool to anything else.

    A verdict is Review's act, and a handler that is reachable only because
    someone mis-edited the registry still refuses to record one.

    Raises:
        PermissionError: The scope's role is not Review.
    """
    require_role(context, tool, AgentRole.REVIEW)


def require_research(context: ToolContext, tool: str) -> None:
    """Refuse a Research-only tool to anything else.

    Everything Research writes goes into the Evidence Ledger, which is the
    record every later acceptance criterion's provenance is read from. No other
    role writes to it, so no other role may reach the tools that do.

    Raises:
        PermissionError: The scope's role is not Research.
    """
    require_role(context, tool, AgentRole.RESEARCH)


def require_worker(context: ToolContext, tool: str) -> None:
    """Refuse a Worker tool to anything that is not a Worker.

    Both Workers, because both work under a frozen Execution Contract and both
    may find that the contract does not name what they have been asked for.
    The question is the same one and so is the answer, which is why this is one
    guard rather than two.

    Raises:
        PermissionError: The scope's role is neither Worker.
    """
    require_role(context, tool, AgentRole.COMPUTE_WORKER, AgentRole.EXPERIMENTAL_WORKER)


def as_json(value: Any) -> Any:
    """A JSON-safe view of a record, or of a sequence of them.

    Pydantic's JSON mode is what makes enums and datetimes survive the trip:
    the transport would otherwise receive Python objects and stringify them by
    its own rules, which differ between the stdio SDK and the WebSocket
    projection and would make one record read two ways.
    """
    if isinstance(value, tuple | list):
        return [as_json(item) for item in value]
    dump = getattr(value, "model_dump", None)
    return dump(mode="json") if callable(dump) else value
