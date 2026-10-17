"""Per-project leases and the fencing epoch a runtime holds.

The database row is the authority. A process acquires the row before it starts
a project's DSH seats, renews it while it is driving, and releases it on a
clean stop. A takeover of an expired or released row increments ``epoch``; a
stale owner can no longer renew, release, or pass a fencing check.

Expiry and the lease timestamps are read and written with PostgreSQL's
``now()``, not a host clock, so two processes with skewed clocks still agree
about whether a lease is alive.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from ravel.state.database import Database
from ravel.state.tables import ProjectLeaseRow

__all__ = [
    "LeaseGuard",
    "LeaseHeld",
    "LeaseLost",
    "LeaseService",
    "ProjectLease",
]


class LeaseHeld(RuntimeError):
    """Another live owner holds the project."""


class LeaseLost(RuntimeError):
    """This owner/epoch no longer holds the project."""


@dataclass(frozen=True)
class ProjectLease:
    project_id: str
    owner_instance_id: str
    epoch: int
    expires_at: datetime
    renewed_at: datetime
    released_at: datetime | None

    @classmethod
    def from_row(cls, row: ProjectLeaseRow) -> ProjectLease:
        return cls(
            project_id=row.project_id,
            owner_instance_id=row.owner_instance_id,
            epoch=row.epoch,
            expires_at=row.expires_at,
            renewed_at=row.renewed_at,
            released_at=row.released_at,
        )


@dataclass
class LeaseService:
    """Acquire, renew, fence and release one project's runtime lease."""

    database: Database
    ttl_seconds: float = 60.0
    renew_seconds: float = 15.0

    def _now(self, session: Session) -> datetime:
        return session.execute(text("SELECT now()")).scalar_one()

    def _expiry(self, now: datetime) -> datetime:
        return now + timedelta(seconds=self.ttl_seconds)

    def acquire(self, project_id: str, owner_instance_id: str) -> ProjectLease:
        """Take the lease, renew it if this owner already holds it, or raise.

        A released row keeps its epoch history: the next owner receives
        ``epoch + 1``. An expired row is taken over the same way. A live lease
        held by another owner is refused rather than waited on; the supervisor
        retries on its next discovery tick.
        """
        with self.database.transaction() as session:
            row = session.get(ProjectLeaseRow, project_id, with_for_update=True)
            now = self._now(session)
            if row is None:
                row = ProjectLeaseRow(
                    project_id=project_id,
                    owner_instance_id=owner_instance_id,
                    epoch=1,
                    expires_at=self._expiry(now),
                    renewed_at=now,
                    released_at=None,
                )
                session.add(row)
                session.flush()
                return ProjectLease.from_row(row)

            if row.owner_instance_id == owner_instance_id and row.released_at is None:
                if row.expires_at <= now:
                    raise LeaseLost(
                        f"project {project_id} lease expired; re-acquire to take over"
                    )
                row.expires_at = self._expiry(now)
                row.renewed_at = now
                session.flush()
                return ProjectLease.from_row(row)

            if row.released_at is not None or row.expires_at <= now:
                row.owner_instance_id = owner_instance_id
                row.epoch += 1
                row.expires_at = self._expiry(now)
                row.renewed_at = now
                row.released_at = None
                session.flush()
                return ProjectLease.from_row(row)

            raise LeaseHeld(
                f"project {project_id} is held by {row.owner_instance_id} "
                f"epoch {row.epoch} until {row.expires_at.isoformat()}"
            )

    def renew(self, project_id: str, owner_instance_id: str, epoch: int) -> ProjectLease:
        """Extend the lease only for the owner/epoch that currently holds it."""
        with self.database.transaction() as session:
            row = session.get(ProjectLeaseRow, project_id, with_for_update=True)
            now = self._now(session)
            if (
                row is None
                or row.owner_instance_id != owner_instance_id
                or row.epoch != epoch
                or row.released_at is not None
                or row.expires_at <= now
            ):
                raise LeaseLost(
                    f"project {project_id} is no longer held by "
                    f"{owner_instance_id} epoch {epoch}"
                )
            row.expires_at = self._expiry(now)
            row.renewed_at = now
            session.flush()
            return ProjectLease.from_row(row)

    def release(self, project_id: str, owner_instance_id: str, epoch: int) -> bool:
        """Release the lease; a stale owner cannot release a newer epoch."""
        with self.database.transaction() as session:
            row = session.get(ProjectLeaseRow, project_id, with_for_update=True)
            now = self._now(session)
            if (
                row is None
                or row.owner_instance_id != owner_instance_id
                or row.epoch != epoch
                or row.released_at is not None
                or row.expires_at <= now
            ):
                return False
            row.released_at = now
            session.flush()
            return True

    def check(self, project_id: str, owner_instance_id: str, epoch: int) -> bool:
        """Whether this owner/epoch may still produce side effects."""
        with self.database.read_only() as session:
            value = session.execute(
                text(
                    "SELECT 1 FROM project_leases "
                    "WHERE project_id = :project_id "
                    "AND owner_instance_id = :owner "
                    "AND epoch = :epoch "
                    "AND released_at IS NULL "
                    "AND expires_at > now()"
                ),
                {
                    "project_id": project_id,
                    "owner": owner_instance_id,
                    "epoch": epoch,
                },
            ).scalar_one_or_none()
            return value is not None

    def current(self, project_id: str) -> ProjectLease | None:
        """The row a read-only operations projection can show."""
        with self.database.read_only() as session:
            row = session.get(ProjectLeaseRow, project_id)
            return ProjectLease.from_row(row) if row is not None else None


@dataclass
class LeaseGuard:
    """The loop's cheap per-round fence, with a periodic renewal.

    ``ensure`` is called before every turn. It renews the lease only when the
    renewal interval has elapsed, and otherwise asks the database whether the
    owner/epoch is still current. A lost or expired lease returns ``False`` so
    the loop halts before dispatching another turn.
    """

    service: LeaseService
    project_id: str
    owner_instance_id: str
    epoch: int
    renew_seconds: float | None = None
    _last_renew: float = field(default_factory=time.monotonic, init=False, repr=False)

    def ensure(self) -> bool:
        interval = (
            self.service.renew_seconds
            if self.renew_seconds is None
            else self.renew_seconds
        )
        now = time.monotonic()
        if now - self._last_renew >= interval:
            try:
                self.service.renew(self.project_id, self.owner_instance_id, self.epoch)
            except LeaseLost:
                return False
            self._last_renew = now
        return self.service.check(self.project_id, self.owner_instance_id, self.epoch)
