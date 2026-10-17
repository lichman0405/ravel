"""A process saying it is here, and saying when it is going.

`ravel.state.repositories.services` has the two writes; this is the object a
long-running process holds so that it makes them without every one of them
repeating the same four decisions — which name it reports under, when it
started, how often it promises to speak, and what it is holding.

**Two ways to beat, and the difference is the point.** `beat()` writes one
report, now, and is what a process calls from inside its own loop: a supervisor
that beats from the top of a tick reports a supervisor whose tick is happening,
and one whose tick has wedged stops reporting, which is exactly the failure the
row exists to make visible. `pulse()` is the other shape, for a process that
has no such loop — the Gateway and the worker wait on something else's events
and would have nothing to hang a beat on — and it beats on a timer of its own.

**Retiring is not optional and it is not a delete.** A process that is told to
stop records the moment it stopped, because the difference between a service
that was shut down and one that died is the difference between a deploy and an
incident, and the row is the only place either is legible. The row stays: the
table refuses deletes (it is in `UPDATABLE_TABLES`, so its trigger rejects
them), and a service that could remove its own row could also remove somebody
else's.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from ravel.domain.clock import utcnow
from ravel.state.database import Database
from ravel.state.repositories.services import ServiceRepository

logger = logging.getLogger(__name__)

#: How often a pulsing service speaks. A minute is short enough that an
#: operator notices a death within a couple of them and long enough that the
#: write is not a meaningful share of a quiet deployment's database traffic.
DEFAULT_PULSE_SECONDS = 60.0


@dataclass
class Heartbeat:
    """One process's liveness, as it reports it.

    Attributes:
        database: Where the report goes.
        service: The name from `ravel.domain.services` this process reports
            under.
        started_at: When *this process* started, which travels with every beat
            so that a restart is visible as a new start rather than as an
            unusually long uptime. Taken by default when the object is built,
            since an object that exists is a process that has got this far.
        detail: What this process wants to say about itself beyond being alive.
            Called at each beat rather than stored, so that a report says what
            is true *now* — a supervisor's held projects change between beats
            and a copy taken at construction would describe the first tick
            forever.
        pulse_seconds: How often `pulse` beats, and what this process promises
            a reader. It is put on every report as `poll_seconds`, because that
            is the field `_silence_budget` in `routes/admin.py` measures the
            silence against: a service that beat every five minutes and was
            called stale after fifteen seconds would make the screen useless on
            exactly the deployments that need it.
    """

    database: Database
    service: str
    started_at: datetime = field(default_factory=utcnow)
    detail: Callable[[], dict[str, object]] = dict
    pulse_seconds: float = DEFAULT_PULSE_SECONDS
    _pulsing: asyncio.Task[None] | None = field(default=None, init=False, repr=False)

    @property
    def instance(self) -> str:
        """Which process this is, as `host:pid`, read from this machine."""
        from ravel.state.repositories.services import instance_name

        return instance_name()

    def beat(self) -> bool:
        """Write one report. `True` if it landed, `False` if it could not.

        **A failure to report is reported and not raised.** A database that
        went away mid-beat is the same class of problem as one that went away
        mid-scan, and every caller here already answers that by trying again;
        the alternative — taking a service down because a liveness report
        failed — would make the report the least reliable thing in the process.
        """
        try:
            with self.database.transaction() as session:
                ServiceRepository(session).report(
                    self.service,
                    started_at=self.started_at,
                    detail={"poll_seconds": self.pulse_seconds, **self.detail()},
                )
        except Exception:
            logger.exception("could not record the %s heartbeat; continuing", self.service)
            return False
        return True

    def start(self) -> None:
        """Begin beating on a timer, until `stop`.

        Called by a process that has nothing of its own to hang a beat on. A
        process that has a loop should call `beat` from it instead — see the
        module docstring for why the two are not the same.
        """
        if self._pulsing is not None:
            return
        self.beat()
        self._pulsing = asyncio.create_task(self.pulse(), name=f"ravel-heartbeat-{self.service}")

    async def pulse(self) -> None:
        """Beat every `pulse_seconds` until cancelled."""
        while True:
            await asyncio.sleep(self.pulse_seconds)
            # The swallowing is in `beat`; this is here so that a failing beat
            # does not end the pulse and leave the service reporting nothing at
            # all, which is a worse state than reporting late.
            self.beat()

    async def stop(self) -> None:
        """Stop beating, and record that this process is stopping.

        Idempotent, because shutdown paths are called from more than one place:
        a signal handler and a `finally` both reach here on an ordinary Ctrl-C,
        and a second retire must not be an error.
        """
        if self._pulsing is not None:
            self._pulsing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._pulsing
            self._pulsing = None
        self.retire()

    def retire(self) -> bool:
        """Record the moment this process stopped, without beating again.

        For the caller that has already stopped and wants the record to say so
        — a `finally` after an exception, or a signal handler that has finished
        closing things. Returns whether the row was written.
        """
        try:
            with self.database.transaction() as session:
                ServiceRepository(session).retire(self.service)
        except Exception:
            logger.exception("could not record the %s shutdown", self.service)
            return False
        return True


__all__ = ["DEFAULT_PULSE_SECONDS", "Heartbeat"]
