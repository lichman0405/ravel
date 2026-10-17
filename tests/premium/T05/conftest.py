"""T05 retry tests use the real PostgreSQL test database.

The fixtures are imported from the integration suite rather than re-created: the
property under test is that a spent retry budget survives a supervisor restart,
and only a real database can demonstrate that. A second definition of the test
database would also be a second place the `_test` safety interlock could drift.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401
    clean,
    database,
    integration_settings,
    other_project,
    prepare,
    project,
)
