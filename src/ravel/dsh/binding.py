"""The mapping from a harness session to the work it is doing.

A DSH session is not a Project, and a DSH session identifier is opaque. Every
session RAVEL starts is recorded here against the `(project_id, role, task_id)`
it was created to serve, and the harness session id is what the harness
actually keys its own state on. That binding is the only way RAVEL can tell
which project a session's output belongs to.

The registry describes *live* sessions, so it is deliberately in memory: a
harness session does not survive its runtime process, and a binding to a dead
session would be a lie. Durable records of what a session produced live in
PostgreSQL, keyed by the project, not by the session.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime

from ravel.dsh.roles import AgentRole


class BindingError(RuntimeError):
    """A session binding was requested or used in a way its contract forbids."""


@dataclass(frozen=True, slots=True)
class SessionBinding:
    """One harness session, and the authority it was created under."""

    session_id: str
    project_id: str
    role: AgentRole
    task_id: str | None
    created_at: datetime

    def as_dict(self) -> dict[str, object]:
        """A JSON-safe view, for runtime briefs and the API."""
        return {
            "session_id": self.session_id,
            "project_id": self.project_id,
            "role": self.role.value,
            "role_name": self.role.display_name,
            "task_id": self.task_id,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class SessionBindingRegistry:
    """Thread-safe registry of live session bindings."""

    _bindings: dict[str, SessionBinding] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def bind(
        self,
        session_id: str,
        project_id: str,
        role: AgentRole,
        task_id: str | None = None,
    ) -> SessionBinding:
        """Record a session against the work it serves.

        A session identifier is globally unique in the harness, so rebinding
        one to a different project or role is a fault rather than an update.

        Raises:
            BindingError: The session is already bound to a different scope.
        """
        binding = SessionBinding(
            session_id=session_id,
            project_id=project_id,
            role=role,
            task_id=task_id,
            created_at=datetime.now(UTC),
        )
        with self._lock:
            existing = self._bindings.get(session_id)
            if existing is not None and (
                existing.project_id != project_id
                or existing.role != role
                or existing.task_id != task_id
            ):
                raise BindingError(
                    f"session {session_id!r} is already bound to "
                    f"({existing.project_id}, {existing.role.value}, {existing.task_id}); "
                    f"refusing to rebind it to ({project_id}, {role.value}, {task_id})"
                )
            self._bindings[session_id] = binding
        return binding

    def get(self, session_id: str) -> SessionBinding | None:
        """The binding for a session, or None when it is not RAVEL's."""
        with self._lock:
            return self._bindings.get(session_id)

    def require(self, session_id: str) -> SessionBinding:
        """The binding for a session.

        Raises:
            BindingError: The session is not bound to any project.
        """
        binding = self.get(session_id)
        if binding is None:
            raise BindingError(f"session {session_id!r} is not bound to a project")
        return binding

    def for_project(self, project_id: str) -> tuple[SessionBinding, ...]:
        """Every live session serving a project, oldest first."""
        with self._lock:
            return tuple(
                sorted(
                    (b for b in self._bindings.values() if b.project_id == project_id),
                    key=lambda b: b.created_at,
                )
            )

    def for_scope(
        self, project_id: str, role: AgentRole, task_id: str | None = None
    ) -> tuple[SessionBinding, ...]:
        """Live sessions for one project and role, optionally narrowed to a task."""
        return tuple(
            b
            for b in self.for_project(project_id)
            if b.role is role and (task_id is None or b.task_id == task_id)
        )

    def release(self, session_id: str) -> SessionBinding | None:
        """Forget a session, returning what it was bound to."""
        with self._lock:
            return self._bindings.pop(session_id, None)

    def release_project(self, project_id: str) -> int:
        """Forget every session serving a project, returning how many were dropped."""
        with self._lock:
            doomed = [sid for sid, b in self._bindings.items() if b.project_id == project_id]
            for session_id in doomed:
                del self._bindings[session_id]
        return len(doomed)

    def release_scope(self, project_id: str, role: AgentRole) -> int:
        """Forget every session serving one project and role.

        Called when a runtime is reaped: its sessions died with the process, so
        leaving their bindings behind would let RAVEL attribute a later
        session's output to work that no longer exists.
        """
        with self._lock:
            doomed = [
                sid
                for sid, b in self._bindings.items()
                if b.project_id == project_id and b.role is role
            ]
            for session_id in doomed:
                del self._bindings[session_id]
        return len(doomed)

    def __len__(self) -> int:
        with self._lock:
            return len(self._bindings)
