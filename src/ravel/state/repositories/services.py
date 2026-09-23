"""Long-running processes, and whether they are still there.

`docs/08` gives the administrator a screen of runtime health, and one of the
things on it has no other source: **whether the process that drives projects is
running**. Everything else on that screen is answerable from a record — the
harness pin, the queue, the jobs — but a supervisor that died leaves no trace
in any of them. A project it was driving simply stops moving, and a project
that is idle because nothing is left to do looks exactly the same.

So the supervisor says so itself, once per tick, into one row. This module is
the two halves of that: `report` is what a process calls to say it is alive,
and `all` is what a reader calls to find out who is.

**Nothing here decides how stale is stale.** That is a judgement about a
particular service's cadence, and it lives in `ravel.domain.services` beside
the vocabulary rather than here — a repository that decided when a heartbeat
had expired would be a repository with an opinion about a process it does not
run.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ravel.domain.clock import utcnow
from ravel.domain.services import ServiceReport
from ravel.state.mapping import from_row
from ravel.state.tables import RuntimeServiceRow


class ServiceRepository:
    """The liveness rows. Not project-scoped: a service serves the deployment."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def report(
        self,
        service: str,
        *,
        started_at: datetime,
        detail: dict[str, object] | None = None,
        instance: str | None = None,
    ) -> ServiceReport:
        """Say that this process is alive, now, and return the row it wrote.

        An upsert rather than an insert, and deliberately not a record: the row
        is the current answer, so a restart takes it over. `started_at` travels
        with the beat rather than being written once, because it is a fact about
        *the process that is beating* — a supervisor that came back a minute
        ago should say so, and a row that kept the first start would report
        hours of uptime for a process minutes old.

        `instance` is derived rather than demanded, so a caller cannot forget
        it: `host:pid` is what a reader needs in order to go and look at the
        process, and a service that reported without one would be a row saying
        only that something, somewhere, is alive.
        """
        beat = utcnow()
        statement = insert(RuntimeServiceRow).values(
            service=service,
            instance=instance or instance_name(),
            started_at=started_at,
            heartbeat_at=beat,
            detail=detail or {},
        )
        written = self.session.execute(
            statement.on_conflict_do_update(
                index_elements=[RuntimeServiceRow.service],
                set_={
                    "instance": statement.excluded.instance,
                    "started_at": statement.excluded.started_at,
                    "heartbeat_at": statement.excluded.heartbeat_at,
                    "detail": statement.excluded.detail,
                },
            ).returning(*RuntimeServiceRow.__table__.columns)
        ).one()
        return ServiceReport.model_validate(written._mapping)

    def get(self, service: str) -> ServiceReport | None:
        """What this service last said, or `None` if it never has."""
        row = self.session.get(RuntimeServiceRow, service)
        return None if row is None else from_row(ServiceReport, row)

    def all(self) -> list[ServiceReport]:
        """Every service that has ever reported, by name.

        Named order rather than insertion order, so that two reads a second
        apart compare — which is what lets the administrator's screen redraw
        without its panels moving under the reader.
        """
        rows = self.session.execute(
            select(RuntimeServiceRow).order_by(RuntimeServiceRow.service)
        ).scalars()
        return [from_row(ServiceReport, row) for row in rows]

    def named(self, services: Iterable[str]) -> dict[str, ServiceReport]:
        """The reports for these services, keyed by name.

        A mapping rather than a list because the caller is asking about a fixed
        set of *names* — the services this deployment runs — and some of them
        may never have reported. A list would silently omit those, and a reader
        iterating it could not tell a service that is missing from the answer
        from one that is missing from the deployment.
        """
        wanted = list(services)
        if not wanted:
            return {}
        found = self.session.execute(
            select(RuntimeServiceRow).where(RuntimeServiceRow.service.in_(wanted))
        ).scalars()
        return {row.service: from_row(ServiceReport, row) for row in found}


def instance_name() -> str:
    """Which process this is, as `host:pid`.

    Both halves are facts about the caller's own machine, which is the only
    machine it can speak for — a row naming a host it had merely been told
    about would be a row asserting something it has no way to know.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


__all__ = ["ServiceRepository", "instance_name"]
