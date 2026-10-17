"""T04 lease tests use the real PostgreSQL test database.

The fixtures are imported from the integration suite rather than re-created:
the point of these tests is the database's atomic conflict behaviour, and a
second definition of the test database would be a second place the safety
interlock could drift.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401
    clean,
    database,
    integration_settings,
    other_project,
    project,
)
