"""T12 reuses the integration fixtures unchanged.

The fixtures create a real project in a real PostgreSQL database, run the
schema guards, and truncate every table between tests. A report test that
built its own rows would be testing a different schema from the one RAVEL
runs against; re-exporting the integration fixtures keeps that impossible.
"""

from tests.integration.conftest import (  # noqa: F401
    _database_test_lock,
    clean,
    database,
    integration_settings,
    other_project,
    prepare,
    project,
)
