"""The harness as the Gateway sees it: one way in, and one way to ask how it is.

`docs/08` gives the administrator a screen of runtime health — DSH health,
runtime status, session state — and the Gateway is the only thing they can
reach. That means the Gateway has to be able to answer for a harness it does
not otherwise touch, which is why this exists rather than the pool living
inside a closure in `conversation.py` where nothing could ask it anything.

**The pool is built on first use, and the Gateway never builds one it does not
need.** A Gateway serving reads is a Gateway whose operator has not configured a
model provider, and refusing to start over a harness nobody asked for would make
the read surface depend on the write surface. So `pool()` answers `None` before
anything has needed a runtime, and the administrator's view says exactly that
rather than reporting zero runtimes as though zero were a measurement.

**Health never raises.** A diagnostic that fails because the thing it is
diagnosing is broken is not a diagnostic. Every field below is answered from
configuration or from the pool's own snapshot, and the one thing that could hang
— asking Temporal whether it is listening — is bounded by a timeout in the route
and reported as unreachable rather than raised.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ravel.config import REPO_ROOT, Settings
from ravel.domain.roles import AgentRole
from ravel.dsh.agents import MasterConversation
from ravel.dsh.pool import DshRuntimePool, PoolStats, create_pool
from ravel.gateway.conversation import HarnessMaster, MasterFactory, MasterPort

#: The record of what is pinned, written when the pin was verified. It is read
#: rather than repeated so that the administrator's screen and the test suite
#: agree about which harness is running, instead of both quoting a literal that
#: a future upgrade would have to remember to change twice.
PIN_PATH = REPO_ROOT / "vendor" / "DSH_PIN.json"


def harness_home(settings: Settings) -> Path:
    """Where the harness home is, without creating it.

    `Settings.dsh_home_path` makes the directory on the way to returning it,
    which is right for a launcher and wrong for a diagnostic: a health check
    that creates the thing it is checking reports `home_exists` as true from
    the second read onwards, which is exactly when the answer has stopped being
    worth having.
    """
    return (REPO_ROOT / settings.dsh_home).resolve()


def read_pin(path: Path | None = None) -> dict[str, Any] | None:
    """What the harness pin file says, or `None` if there is not one.

    `None` rather than an exception: a checkout without the pin file is a
    deployment with something wrong, and the administrator's screen should show
    that as a missing field rather than as a 500.
    """
    try:
        loaded: dict[str, Any] = json.loads((path or PIN_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return loaded


@dataclass
class HarnessRuntime:
    """One process's harness: the conversation per project, and the pool under it.

    Holds no session of its own and no state a restart would need. A
    conversation is kept for a project while its runtime lives, because Master's
    turns have continuity within a project for as long as that lasts and no
    longer — which `vendor/DSH_PIN.json` records as a property of the pinned
    harness rather than a choice made here: a session cannot be reopened across
    a runtime restart, so recovery reads PostgreSQL and nothing depends on this
    dictionary.
    """

    settings: Settings
    _pool: DshRuntimePool | None = field(default=None, init=False, repr=False)
    _conversations: dict[str, HarnessMaster] = field(
        default_factory=dict, init=False, repr=False
    )

    def pool(self) -> DshRuntimePool | None:
        """The live pool, or `None` if no runtime has been needed yet."""
        return self._pool

    def _start_pool(self) -> DshRuntimePool:
        """Build the pool, once, on the first thing that needs a runtime."""
        pool = self._pool
        if pool is None:
            pool = create_pool(self.settings)
            self._pool = pool
        return pool

    def master_of(self, project_id: str) -> MasterPort:
        """A `MasterFactory`: this project's Master, started if it is not running."""
        existing = self._conversations.get(project_id)
        if existing is not None and existing.conversation.live:
            return existing

        started = HarnessMaster(
            conversation=MasterConversation(
                pool=self._start_pool(),
                project_id=project_id,
                role=AgentRole.MASTER,
                # Written into the runtime's brief once, at start. It says which
                # channel opened the scope, because that is a fact about the
                # authority the process was launched under and not something a
                # later turn may revise.
                brief={"channel": "user conversation"},
            )
        )
        self._conversations[project_id] = started
        return started

    def stats(self) -> PoolStats | None:
        """What the pool is doing, or `None` if there is no pool to ask."""
        pool = self._pool
        return None if pool is None else pool.stats()

    def health(self) -> dict[str, Any]:
        """Everything the administrator's screen can be told without waiting.

        The shape is the contract: `pool` is `None` when no runtime has been
        started, and a reader must render that as "none started" rather than as
        a pool with no runtimes in it. The two are different facts about a
        deployment and only one of them is a problem.
        """
        pin = read_pin() or {}
        home = harness_home(self.settings)
        stats = self.stats()
        return {
            "harness": {
                "name": pin.get("harness"),
                "tag": pin.get("pin", {}).get("tag"),
                "commit": pin.get("pin", {}).get("commit"),
                "patches_required": pin.get("patches", {}).get("required"),
                "home": str(home),
                "home_exists": home.is_dir(),
                "provider": self.settings.dsh_provider,
                "model": self.settings.dsh_model,
                "idle_timeout_seconds": self.settings.dsh_idle_timeout_seconds,
                "turn_timeout_seconds": self.settings.dsh_turn_timeout_seconds,
            },
            "pool": None
            if stats is None
            else {
                "live_runtimes": stats.live_runtimes,
                "live_sessions": stats.live_sessions,
                "scopes": [
                    {"project_id": project_id, "role": role}
                    for project_id, role in stats.scopes
                ],
                "total_turns": stats.total_turns,
            },
            "temporal": {
                "host": self.settings.temporal_host,
                "namespace": self.settings.temporal_namespace,
                "task_queue": self.settings.temporal_task_queue,
            },
        }


def harness_runtime(settings: Settings) -> tuple[HarnessRuntime, MasterFactory]:
    """A runtime and the factory that reaches its Master.

    Both out of one call because they are one object: `GatewayState` holds the
    runtime so the administrator can ask it things, and holds the factory so
    every route that reaches Master goes through the same conversation cache.
    Returning them separately would let a caller wire one and forget the other,
    which would mean two pools.
    """
    runtime = HarnessRuntime(settings=settings)
    return runtime, runtime.master_of


__all__ = ["PIN_PATH", "HarnessRuntime", "harness_home", "harness_runtime", "read_pin"]
