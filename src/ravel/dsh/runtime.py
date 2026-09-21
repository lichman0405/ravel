"""One DeepSeek Harness runtime process, serving one project and one role.

RAVEL launches a runtime per `(project_id, role)` rather than sharing one
across roles, because the pinned harness carries no session identity on an MCP
tool call: a shared tool server would have to trust whatever project the model
named. Scoping the process fixes the authority before any agent exists, and the
role's tool roster is decided by the composition the process boots with.

A runtime serves turns one at a time. The harness SDK's turn call blocks until
the session goes idle, so overlapping turns on one process would interleave on a
single transport; the lock makes that impossible rather than unlikely.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig
from deepseek_harness.models import Notification

from ravel.config import Settings
from ravel.dsh.composition import BASE_PROFILE, RoleComposition
from ravel.dsh.roles import AgentRole, definition_for

logger = logging.getLogger(__name__)

#: Name of the generated overlay inside a runtime's working directory.
ROLE_PATCH_NAME = "ravel-role.cordis.patch.yml"


class HarnessRuntimeError(RuntimeError):
    """A role runtime could not be started or used."""


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    """What one agent turn produced."""

    session_id: str
    response: str
    finish_reason: str | None
    events: tuple[dict[str, Any], ...]

    @property
    def tool_calls(self) -> tuple[dict[str, Any], ...]:
        """Every tool the agent invoked, in order, as the session log recorded it."""
        return tuple(e for e in self.events if e.get("type") == "tool/call")

    @property
    def completed(self) -> bool:
        """Whether the turn ended because the agent finished rather than failed."""
        return self.finish_reason == "completed"


@dataclass(slots=True)
class RoleRuntime:
    """A live harness process bound to one `(project, role)`."""

    project_id: str
    role: AgentRole
    work_dir: Path
    brief_path: Path
    harness: DeepSeekHarness
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_used_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    turns: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _closed: bool = field(default=False, repr=False)

    @property
    def scope_key(self) -> tuple[str, AgentRole]:
        """The pair this runtime is scoped to."""
        return (self.project_id, self.role)

    @property
    def is_closed(self) -> bool:
        """Whether the process has been shut down."""
        return self._closed

    def new_session_id(self, task_id: str | None = None) -> str:
        """Mint a session identifier the harness has never seen.

        The pinned SDK creates a session implicitly on first prompt and has no
        way to reopen one across processes, so identifiers are generated fresh
        rather than derived from anything durable.
        """
        suffix = f"-{task_id}" if task_id else ""
        return f"ravel-{self.project_id}-{self.role.value}{suffix}-{uuid.uuid4().hex[:12]}"

    def run_turn(
        self,
        session_id: str,
        prompt: str,
        *,
        on_notification: Callable[[Notification], None] | None = None,
    ) -> TurnOutcome:
        """Run one turn to completion and return what it produced.

        Args:
            session_id: A session this runtime created; the harness creates it
                on first use and continues it on later turns.
            prompt: The turn's input text.
            on_notification: Called for every harness notification as it
                arrives, for streaming progress to a caller.

        Raises:
            HarnessRuntimeError: The runtime was already closed.
        """
        with self._lock:
            if self._closed:
                raise HarnessRuntimeError(
                    f"runtime for ({self.project_id}, {self.role.value}) is closed"
                )
            self.last_used_at = datetime.now(UTC)
            result = self.harness.run(
                session_id=session_id, input=prompt, on_notification=on_notification
            )
            self.turns += 1
            self.last_used_at = datetime.now(UTC)

        return TurnOutcome(
            session_id=result.session_id,
            response=result.final_response,
            finish_reason=result.finish_reason,
            events=tuple(result.events),
        )

    def close(self) -> None:
        """Shut the runtime down, tolerating an already-dead process."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                self.harness.close()
            except Exception:
                logger.warning(
                    "runtime for (%s, %s) did not shut down cleanly",
                    self.project_id,
                    self.role.value,
                    exc_info=True,
                )

    @classmethod
    def create(
        cls,
        settings: Settings,
        project_id: str,
        role: AgentRole,
        brief: dict[str, Any] | None = None,
    ) -> RoleRuntime:
        """Write the runtime brief and prepare a harness process for one scope.

        The process starts on its first turn rather than here, so a runtime
        that is created and never used costs nothing.
        """
        definition = definition_for(role)
        if not definition.prompt_file.is_file():
            raise HarnessRuntimeError(f"role prompt is missing: {definition.prompt_file}")

        work_dir = settings.dsh_runtime_cwd(project_id, role.slug)
        brief_path = work_dir / "brief.json"
        brief_path.write_text(
            json.dumps({"project_id": project_id, "role": role.value, **(brief or {})}, indent=2),
            encoding="utf-8",
        )

        patch_path = RoleComposition.for_scope(
            project_id=project_id,
            role=role,
            mcp_command=_interpreter(),
            brief_path=brief_path,
            # The tool server is a separate process and reads the environment
            # rather than these settings, so the database it must read is
            # passed to it here. Without this it would fall back to `.env` and
            # could serve a project out of a database this process is not
            # driving — the same project id, the same role, and none of the
            # state.
            tool_server_env=settings.tool_server_env(),
        ).write(work_dir / ROLE_PATCH_NAME)

        api_key = (
            settings.deepseek_api_key.get_secret_value()
            if settings.deepseek_api_key is not None
            else None
        )

        config = DeepSeekHarnessConfig(
            provider=settings.dsh_provider,
            model=settings.dsh_model,
            reasoning_effort=settings.dsh_reasoning_effort,
            # The agent's working directory is RAVEL-owned and empty. It is
            # never a workspace: the harness reads `<cwd>/.env` as a credential
            # fallback, so it must be a directory nothing else can write to.
            cwd=str(work_dir),
            runtime_cwd=str(work_dir),
            dsh_bin=settings.dsh_bin,
            profile=BASE_PROFILE,
            patches=(str(patch_path),),
            dsh_home=str(settings.dsh_home_path()),
            api_key=api_key,
            request_timeout_seconds=settings.dsh_turn_timeout_seconds,
        )

        return cls(
            project_id=project_id,
            role=role,
            work_dir=work_dir,
            brief_path=brief_path,
            harness=DeepSeekHarness(config),
        )


def _interpreter() -> str:
    """The Python that serves RAVEL tool servers.

    The running interpreter, so a tool server always imports the same RAVEL
    that launched the harness.
    """
    import sys

    return sys.executable
