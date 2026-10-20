"""T10 reuses the integration fixtures unchanged.

The AC3 trace reads real PostgreSQL rows: a frozen execution contract, a frozen
acceptance contract bound to a node, artifact versions, evidence sources, and an
execution. Rebuilding those through anything other than the repositories the
product uses would test a different schema, so the integration fixtures are
re-exported rather than reimplemented.
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
