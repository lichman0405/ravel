"""The set of live harness runtimes.

A runtime is expensive to start and cheap to keep, so RAVEL keeps one per
`(project_id, role)` that is actually in use and reaps it once it has been idle
long enough. The pool is the only place that creates a harness process, which
keeps the authority each process was launched with in one auditable place.

Reaping is explicit rather than timer-driven: `reap_idle` is called on every
acquire and can be called by an operator or a test at a chosen moment. Nothing
reaps a runtime out from under a turn, because a turn holds the runtime's lock.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from ravel.config import Settings
from ravel.dsh.binding import SessionBinding, SessionBindingRegistry
from ravel.dsh.roles import AgentRole
from ravel.dsh.runtime import HarnessRuntimeError, RoleRuntime

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PoolStats:
    """A snapshot of the pool, for health reporting and tests."""

    live_runtimes: int
    live_sessions: int
    scopes: tuple[tuple[str, str], ...]
    total_turns: int


@dataclass
class DshRuntimePool:
    """Lazily started, idle-reaped harness runtimes, keyed by `(project, role)`."""

    settings: Settings
    bindings: SessionBindingRegistry = field(default_factory=SessionBindingRegistry)
    _runtimes: dict[tuple[str, AgentRole], RoleRuntime] = field(default_factory=dict)
    #: Turns served by runtimes that are no longer live, per scope. Reaping is
    #: routine and silent — a scope idle past the timeout is closed and a fresh
    #: runtime starts at `turns = 0` on its next turn — so without this the
    #: only honest answer to "has this seat done anything" is "not recently".
    #: Entries are never removed: they are the record of which seats have
    #: worked on which projects, they cost a tuple and an int each, and the
    #: number of scopes a deployment has ever had is not a number that grows
    #: without bound in any way that matters.
    _retired_turns: dict[tuple[str, AgentRole], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ── Runtimes ───────────────────────────────────────────────────────────

    def runtime(
        self,
        project_id: str,
        role: AgentRole,
        brief: dict[str, Any] | None = None,
    ) -> RoleRuntime:
        """The runtime for a scope, starting one if none is live.

        The brief is only written when a runtime is created: it describes the
        authority the process launched with, and rewriting it under a running
        agent would let the agent's view drift from its grant.

        Idle scopes are reaped first, which is what makes a Worker's session
        dormant rather than resident: a compute runtime that has not been asked
        for anything in `dsh_idle_timeout_seconds` is closed here, and the next
        turn for it starts a new one. That costs the session its short-term
        memory and nothing else — the authoritative state is PostgreSQL — and
        it is why this is safe to do on the acquire path. Reaping before the
        lock rather than inside it, because `reap_idle` takes the same lock.
        """
        self.reap_idle()
        key = (project_id, role)
        with self._lock:
            existing = self._runtimes.get(key)
            if existing is not None and not existing.is_closed:
                return existing
            if existing is not None:
                self._retire_locked(existing)
                del self._runtimes[key]
            runtime = RoleRuntime.create(self.settings, project_id, role, brief)
            self._runtimes[key] = runtime
            logger.info(
                "started harness runtime for project=%s role=%s work_dir=%s",
                project_id,
                role.value,
                runtime.work_dir,
            )
            return runtime

    def live_runtime(self, project_id: str, role: AgentRole) -> RoleRuntime | None:
        """The runtime for a scope, if one is currently live."""
        with self._lock:
            runtime = self._runtimes.get((project_id, role))
        return runtime if runtime is not None and not runtime.is_closed else None

    def turns_served(self, project_id: str, role: AgentRole) -> int:
        """How many turns a scope's sessions have taken, live runtime included.

        Counted across every runtime the scope has had, not just the one that
        happens to be alive now. That distinction is the whole reason this
        method exists rather than a caller reading `live_runtime(...).turns`:
        reaping is routine, so a seat that worked for an hour and has since
        been idle for `dsh_idle_timeout_seconds` has no live runtime and would
        otherwise be indistinguishable from a seat that was never asked to do
        anything.
        """
        key = (project_id, role)
        with self._lock:
            retired = self._retired_turns.get(key, 0)
            runtime = self._runtimes.get(key)
        return retired + (runtime.turns if runtime is not None else 0)

    def _retire_locked(self, runtime: RoleRuntime) -> None:
        """Fold a departing runtime's turns into its scope's running total.

        Called with `_lock` held, at the single moment a runtime leaves
        `_runtimes` — whichever of the five paths removed it — so that a turn is
        counted exactly once whether it was served a second ago or an hour ago.
        """
        key = (runtime.project_id, runtime.role)
        self._retired_turns[key] = self._retired_turns.get(key, 0) + runtime.turns

    def start_session(
        self,
        project_id: str,
        role: AgentRole,
        task_id: str | None = None,
        brief: dict[str, Any] | None = None,
    ) -> tuple[RoleRuntime, SessionBinding]:
        """Start a session and record the work it serves.

        Returns the runtime that owns the session and the binding that records
        what the session is for. Callers must hold the binding to attribute the
        session's output; the harness session id alone says nothing.
        """
        runtime = self.runtime(project_id, role, brief)
        session_id = runtime.new_session_id(task_id)
        session_binding = self.bindings.bind(session_id, project_id, role, task_id)
        return runtime, session_binding

    # ── Session bindings ───────────────────────────────────────────────────

    def binding_for(self, session_id: str) -> SessionBinding | None:
        """The work a session serves, or None when it is not RAVEL's."""
        return self.bindings.get(session_id)

    def binding_or_raise(self, session_id: str) -> SessionBinding:
        """The work a session serves.

        Raises:
            BindingError: The session is not bound to any project.
        """
        return self.bindings.require(session_id)

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def reap_idle(self, now: datetime | None = None) -> int:
        """Close runtimes idle past the configured timeout, returning how many.

        A runtime mid-turn holds its own lock, so a turn already in flight is
        never closed here; it becomes eligible on the next call.
        """
        cutoff = (now or datetime.now(UTC)) - timedelta(
            seconds=self.settings.dsh_idle_timeout_seconds
        )
        doomed: list[RoleRuntime] = []
        with self._lock:
            for key, runtime in list(self._runtimes.items()):
                if runtime.is_closed:
                    self._retire_locked(runtime)
                    del self._runtimes[key]
                    continue
                if runtime.last_used_at < cutoff and runtime._lock.acquire(blocking=False):
                    if runtime.last_used_at < cutoff:
                        self._retire_locked(runtime)
                        del self._runtimes[key]
                        doomed.append(runtime)
                    runtime._lock.release()

        for runtime in doomed:
            logger.info(
                "reaping idle harness runtime for project=%s role=%s after %d turn(s)",
                runtime.project_id,
                runtime.role.value,
                runtime.turns,
            )
            runtime.close()
            self.bindings.release_scope(runtime.project_id, runtime.role)
        return len(doomed)

    def close_scope(self, project_id: str, role: AgentRole) -> bool:
        """Shut down one scope's runtime, returning whether one was live."""
        with self._lock:
            runtime = self._runtimes.pop((project_id, role), None)
            if runtime is not None:
                self._retire_locked(runtime)
        if runtime is None:
            return False
        runtime.close()
        self.bindings.release_scope(project_id, role)
        return True

    def close_project(self, project_id: str) -> int:
        """Shut down every runtime serving a project, returning how many closed."""
        with self._lock:
            doomed = [key for key in self._runtimes if key[0] == project_id]
            runtimes = [self._runtimes.pop(key) for key in doomed]
            for runtime in runtimes:
                self._retire_locked(runtime)
        for runtime in runtimes:
            runtime.close()
        self.bindings.release_project(project_id)
        return len(runtimes)

    def close(self) -> None:
        """Shut down every runtime. Safe to call more than once."""
        with self._lock:
            runtimes = list(self._runtimes.values())
            for runtime in runtimes:
                self._retire_locked(runtime)
            self._runtimes.clear()
        for runtime in runtimes:
            try:
                runtime.close()
            except Exception:
                logger.warning(
                    "failed to close runtime for (%s, %s)",
                    runtime.project_id,
                    runtime.role.value,
                    exc_info=True,
                )

    def __enter__(self) -> DshRuntimePool:
        return self

    def __exit__(self, _exc_type: object, _exc: object, _tb: object) -> None:
        self.close()

    # ── Introspection ──────────────────────────────────────────────────────

    def stats(self) -> PoolStats:
        """A snapshot of live runtimes and sessions, and the turns served.

        `total_turns` is every turn the pool has served, including those served
        by runtimes it has since reaped; the live counts beside it answer the
        different question of what is running right now.
        """
        with self._lock:
            live = [r for r in self._runtimes.values() if not r.is_closed]
            scopes = tuple(sorted((r.project_id, r.role.value) for r in live))
            turns = sum(self._retired_turns.values()) + sum(
                r.turns for r in self._runtimes.values()
            )
        return PoolStats(
            live_runtimes=len(live),
            live_sessions=len(self.bindings),
            scopes=scopes,
            total_turns=turns,
        )


def create_pool(settings: Settings | None = None) -> DshRuntimePool:
    """Build a pool from settings."""
    if settings is None:
        from ravel.config import get_settings

        settings = get_settings()
    if not settings.dsh_home_path().is_dir():
        raise HarnessRuntimeError("DSH home could not be created")
    return DshRuntimePool(settings=settings)
