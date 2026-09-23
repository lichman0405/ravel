"""The processes that are supposed to be running, and what they last said.

RAVEL's state is in PostgreSQL and its durability is Temporal's, so there is
nothing here that either of them could not hold. What neither holds is the
answer to **"is the thing that drives projects actually running"** — a
supervisor that died leaves a project that is idle, and an idle project is a
state a working supervisor also produces.

So a long-running process reports itself. This module is the vocabulary for
that: the closed list of service names, one record per report, and a
`heartbeat_is_stale` question with the two numbers it needs to answer it.

**What is deliberately absent.** There is no `Process` entity and no registry
of instances: RAVEL does not supervise a fleet in V0, it has three named
processes on one host, and a data model for a fleet would be a data model for
a system that does not exist. There is also no restart counter and no history —
a report is the *current* state of a process, not a record of what happened to
it. What happened is in the log, which is where an operator goes next.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from ravel.domain.base import Record
from ravel.domain.clock import utcnow

#: The processes this deployment runs, by the name each reports itself under.
#:
#: Closed, and closed for the reason every vocabulary here is: two spellings of
#: `supervisor` would be two rows, and a screen reading one of them would
#: report the service as absent while it is running. A process whose name is not
#: in this tuple is a process the screen has no panel for, and adding one is a
#: deliberate act rather than a typo.
SUPERVISOR = "supervisor"
TEMPORAL_WORKER = "temporal-worker"
GATEWAY = "gateway"

SERVICE_NAMES: tuple[str, ...] = (SUPERVISOR, TEMPORAL_WORKER, GATEWAY)


class ServiceReport(Record):
    """One process's account of itself, as of its last beat.

    Overwritten rather than appended, which is why nothing here is an
    identifier: a report is not a record of something that happened, it is the
    current answer to a question about now. `instance` and `started_at` move
    together when a process restarts — a supervisor that came back a minute ago
    says so, and the row does not accumulate uptime it did not have.

    `detail` is the process's own summary of itself, in whatever shape that
    process finds useful. It is *not* project-scoped and must not be handed to
    a caller as it stands: the routes that read this filter it down to the
    project being asked about, because a supervisor's report names every
    project it holds.
    """

    service: str = Field(min_length=1, max_length=64)
    #: Which process, as `host:pid`. What a reader needs to go and look.
    instance: str = Field(min_length=1, max_length=255)
    started_at: datetime
    heartbeat_at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)

    def age_seconds(self, *, now: datetime | None = None) -> float:
        """How long ago this process last spoke.

        Negative when the clock has gone backwards, which is left as it is:
        clamping would hide a host whose time moved, and the callers compare
        this against a budget rather than printing it.
        """
        return ((now or utcnow()) - self.heartbeat_at).total_seconds()


def heartbeat_is_stale(
    report: ServiceReport | None, *, budget_seconds: float, now: datetime | None = None
) -> bool:
    """Whether a service has gone quiet for longer than it is allowed to.

    `None` — a service that has never reported — is not stale, and the
    distinction is the whole point of taking an optional: "nothing has ever
    said it was running here" and "something said it was running and then
    stopped" are different facts, and only the second is a process that died.
    A screen that rendered them the same would report a service as crashed on a
    deployment that never started it.

    The budget is the caller's because it is a fact about a particular
    service's cadence rather than about heartbeats: what counts as silence for
    a loop that ticks every five seconds is not what counts for one that may
    wait minutes on a queue.
    """
    if report is None:
        return False
    return report.age_seconds(now=now) > budget_seconds


__all__ = [
    "GATEWAY",
    "SERVICE_NAMES",
    "SUPERVISOR",
    "TEMPORAL_WORKER",
    "ServiceReport",
    "heartbeat_is_stale",
]
