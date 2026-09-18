"""The authority a tool server process holds.

An MCP `tools/call` carries only a tool name and its arguments — no session, no
project, no role. A server therefore cannot ask the wire who is calling, and
must never accept a caller-supplied project id, because a model could name one
it has no authority over.

RAVEL resolves this structurally: the runtime process is scoped to one
`(project_id, role)` pair, so the scope arrives through the environment and is
fixed before any agent exists.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from ravel.dsh.roles import AgentRole

ENV_PROJECT_ID = "RAVEL_PROJECT_ID"
ENV_ROLE = "RAVEL_ROLE"
ENV_BRIEF_FILE = "RAVEL_BRIEF_FILE"


class ScopeError(RuntimeError):
    """The tool server was launched without a usable authority scope."""


@dataclass(frozen=True, slots=True)
class RuntimeBrief:
    """The authority-scoped view RAVEL hands a runtime at launch.

    Deliberately narrow: it carries what the role needs to orient itself and
    nothing that belongs to another project. It is a snapshot, not the
    authoritative record — PostgreSQL holds that.
    """

    project_id: str
    project_title: str
    role: AgentRole
    objective: str | None = None
    phase: str | None = None
    bindings: tuple[dict[str, object], ...] = ()

    @classmethod
    def from_file(cls, path: Path, role: AgentRole, project_id: str) -> RuntimeBrief:
        """Read a brief written by RAVEL, rejecting one that names another scope.

        The identity check is the point: a brief that disagrees with this
        process's own project or role is a wiring fault, and continuing would
        hand an agent authority it was not launched with.
        """
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return cls(project_id=project_id, project_title=project_id, role=role)
        except json.JSONDecodeError as exc:
            raise ScopeError(f"runtime brief at {path} is not valid JSON: {exc}") from exc

        if not isinstance(payload, dict):
            raise ScopeError(f"runtime brief at {path} must be a JSON object")

        brief_project = payload.get("project_id")
        brief_role = payload.get("role")
        if brief_project is not None and brief_project != project_id:
            raise ScopeError(
                f"runtime brief at {path} names project {brief_project!r}, "
                f"but this process serves {project_id!r}"
            )
        if brief_role is not None and brief_role != role.value:
            raise ScopeError(
                f"runtime brief at {path} names role {brief_role!r}, "
                f"but this process serves {role.value!r}"
            )

        # An absent key and an empty list mean the same thing: no sessions are
        # bound yet. Only a present-but-wrong-typed value is a fault.
        bindings = payload.get("bindings")
        if bindings is None:
            bindings = []
        if not isinstance(bindings, list):
            raise ScopeError(f"runtime brief at {path} has a non-list 'bindings'")

        return cls(
            project_id=project_id,
            project_title=str(payload.get("project_title") or project_id),
            role=role,
            objective=payload.get("objective"),
            phase=payload.get("phase"),
            bindings=tuple(bindings),
        )


@dataclass(frozen=True, slots=True)
class ToolScope:
    """Everything a tool handler may assume about its caller."""

    project_id: str
    role: AgentRole
    brief_path: Path | None = None

    @classmethod
    def from_environment(cls, environ: dict[str, str] | None = None) -> ToolScope:
        """Build the scope from the environment RAVEL launched this process with.

        Raises:
            ScopeError: A required variable is absent or names an unknown role.
        """
        env = os.environ if environ is None else environ
        project_id = (env.get(ENV_PROJECT_ID) or "").strip()
        if not project_id:
            raise ScopeError(
                f"{ENV_PROJECT_ID} is unset; a RAVEL tool server must be launched "
                "with an explicit project scope"
            )

        raw_role = (env.get(ENV_ROLE) or "").strip()
        try:
            role = AgentRole(raw_role)
        except ValueError as exc:
            known = ", ".join(r.value for r in AgentRole)
            raise ScopeError(
                f"{ENV_ROLE}={raw_role!r} is not a RAVEL role; known roles: {known}"
            ) from exc

        raw_brief = (env.get(ENV_BRIEF_FILE) or "").strip()
        return cls(
            project_id=project_id,
            role=role,
            brief_path=Path(raw_brief) if raw_brief else None,
        )

    @property
    def is_master(self) -> bool:
        """Whether this scope may change the Scientific DAG."""
        return self.role.mutates_dag

    def brief(self) -> RuntimeBrief:
        """The launch brief, or an empty one when none was written."""
        if self.brief_path is None:
            return RuntimeBrief(
                project_id=self.project_id, project_title=self.project_id, role=self.role
            )
        return RuntimeBrief.from_file(self.brief_path, self.role, self.project_id)
