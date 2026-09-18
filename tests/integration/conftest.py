"""Fixtures for tests that need the real stack.

These tests run against a real PostgreSQL, a real MinIO, and — where the gate
calls for it — a real Temporal, because the properties being asserted are
properties of those systems. A test that proved "an UPDATE is rejected" against
a fake database would prove nothing about the constraint that does the
rejecting.

**The database is a dedicated one, and that is enforced.** Teardown truncates
every table, so pointing these tests at a developer's working database would
destroy real projects. `_test_settings` refuses to run if the configured
database name does not end in `_test`.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError

from ravel.config import Settings
from ravel.domain.enums import UserRole
from ravel.domain.project import Project
from ravel.state import guards
from ravel.state.database import Database, create_db_engine, install_utc_guard
from ravel.state.repositories.identity import MembershipRepository, UserRepository
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.store import S3ArtifactStore
from ravel.state.tables import Base

#: Every integration test truncates on the way in, so the database name is a
#: safety interlock rather than a convention.
TEST_DATABASE_SUFFIX = "_test"


@pytest.fixture(scope="session")
def integration_settings() -> Settings:
    """Settings pointed at the dedicated test database.

    Raises:
        RuntimeError: The configured database is not a test database.
    """
    # The DSN override is addressed by its alias, which is the name pydantic
    # actually accepts: an ambient RAVEL_POSTGRES_DSN must not redirect a
    # suite that truncates every table.
    settings = Settings(env="test", postgres_db="ravel_test", RAVEL_POSTGRES_DSN=None)
    if not settings.postgres_db.endswith(TEST_DATABASE_SUFFIX):
        raise RuntimeError(
            f"integration tests truncate every table and refuse to run against "
            f"{settings.postgres_db!r}; the name must end in {TEST_DATABASE_SUFFIX!r}"
        )
    return settings


@pytest.fixture(scope="session")
def database(integration_settings: Settings) -> Iterator[Database]:
    """The test database, with the schema and the guards installed."""
    engine = create_db_engine(integration_settings)
    install_utc_guard(engine)
    db = Database(engine)
    try:
        db.health()
    except (OperationalError, ProgrammingError) as error:  # pragma: no cover
        pytest.skip(f"PostgreSQL is not reachable; run scripts/dev_up.sh ({error})")
    Base.metadata.create_all(engine)
    # `create_all` fires `after_create` only when it actually created tables, so
    # the guards are installed explicitly as well. `install` is idempotent, and
    # an existing schema must still be guarded.
    with engine.begin() as connection:
        guards.install(connection)
    yield db
    db.dispose()


@pytest.fixture
def clean(database: Database) -> None:
    """Empty every table before a test.

    TRUNCATE rather than DELETE: the append-only triggers reject DELETE on
    every table, which is the property they exist to enforce. Row triggers do
    not fire on TRUNCATE, so this is the one way state leaves the database, and
    it is available only to tests.
    """
    names = ", ".join(sorted(Base.metadata.tables))
    with database.engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {names} RESTART IDENTITY CASCADE"))


@pytest.fixture
def artifact_store(integration_settings: Settings) -> S3ArtifactStore:
    """The real object store, with its bucket present."""
    store = S3ArtifactStore(integration_settings)
    try:
        store.ensure_bucket()
    except Exception as error:  # pragma: no cover - depends on the stack
        pytest.skip(f"MinIO is not reachable; run scripts/dev_up.sh ({error})")
    return store


@pytest.fixture
def project(database: Database, clean: None) -> Project:
    """A project in CREATED status, with an owner who may direct it."""
    with database.transaction() as session:
        owner = UserRepository(session).create(username="owner", password_hash="x")
        project = ProjectRegistry(session).create(
            title="Catalyst screen", objective="Find a better dopant.", created_by=owner.user_id
        )
        MembershipRepository(session, project.project_id).grant(
            user_id=owner.user_id, role=UserRole.PROJECT_OWNER
        )
        return project


@pytest.fixture
def other_project(database: Database, project: Project) -> Project:
    """A second project, for testing that scopes do not leak into each other."""
    with database.transaction() as session:
        return ProjectRegistry(session).create(
            title="Unrelated", objective="Something else entirely.", created_by="someone"
        )
