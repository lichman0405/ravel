"""T06 Action and budget tests use the real PostgreSQL test database.

The fixtures are imported from the integration suite rather than re-created.
Budget serialisation is a property of PostgreSQL advisory and row locks, and
Action immutability is a property of the triggers those tables carry; a fake
database would prove nothing about either. Importing the fixtures also keeps
the `_test` database safety interlock in one place.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401
    _database_test_lock,
    clean,
    database,
    integration_settings,
    other_project,
    prepare,
    project,
)
