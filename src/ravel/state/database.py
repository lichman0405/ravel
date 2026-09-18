"""The database engine, sessions, and the transaction boundary.

One rule governs this module: **a unit of work is one transaction**. Domain
changes and the events describing them are written together, so a reader can
never see a change whose event is missing, or an event for a change that was
rolled back. `transaction()` is the only way to get a session, because a
session handed out without one invites a caller to commit half a change.
"""

from __future__ import annotations

import logging
from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.orm import Session, sessionmaker

from ravel.config import Settings, get_settings

logger = logging.getLogger(__name__)


def create_db_engine(settings: Settings | None = None, **overrides: Any) -> Engine:
    """Build an engine for the authoritative Project State database.

    `pool_pre_ping` is on because a CVM restart, a Docker restart, or an idle
    timeout all silently invalidate pooled connections; without it the first
    query after any of those fails with a stale-connection error that looks
    like a bug in the query.
    """
    resolved = settings if settings is not None else get_settings()
    options: dict[str, Any] = {
        "pool_pre_ping": True,
        "pool_size": 5,
        "max_overflow": 5,
        "future": True,
    }
    options.update(overrides)
    return create_engine(resolved.postgres_dsn, **options)


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to an engine.

    `expire_on_commit=False` so that a caller may read a record's fields after
    the transaction closes — the objects RAVEL hands back are frozen domain
    values, not live ORM rows that need a connection to stay readable.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


class Database:
    """The engine and session factory a process uses.

    Held as one object so tests can substitute an engine without patching a
    module global.
    """

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.sessions = create_session_factory(engine)

    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> Database:
        """Build a database from process settings."""
        return cls(create_db_engine(settings))

    @contextmanager
    def transaction(self) -> Generator[Session]:
        """Run a unit of work, committing on success and rolling back on error.

        Every write in RAVEL goes through here. The `except` re-raises rather
        than swallowing: a caller that catches the exception must be able to
        tell that nothing was written.
        """
        session = self.sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def read_only(self) -> Generator[Session]:
        """Run a read without opening a write transaction."""
        session = self.sessions()
        try:
            yield session
        finally:
            session.close()

    def health(self) -> str:
        """The server version, as a liveness check."""
        with self.engine.connect() as connection:
            return str(connection.execute(text("SHOW server_version")).scalar_one())

    def dispose(self) -> None:
        """Close every pooled connection."""
        self.engine.dispose()


def install_utc_guard(engine: Engine) -> None:
    """Force every session on this engine to interpret timestamps as UTC.

    PostgreSQL's `timestamptz` stores an instant, but the session's `TimeZone`
    setting decides how it is rendered. Pinning it removes a class of bug where
    a record written by one process reads back shifted by hours in another.
    """

    @event.listens_for(engine, "connect")
    def _set_utc(dbapi_connection: Any, _record: Any) -> None:
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET TIME ZONE 'UTC'")
