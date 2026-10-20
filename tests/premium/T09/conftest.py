"""T09 imports the worker script directly to test its target-mode wiring.

The Action-quota tests use the real PostgreSQL test database: whether an
`ACCEPTED` Action holds a compute slot is a property of the rows and the
transaction-scoped advisory lock, and a fake would prove neither. The fixtures
come from the integration suite, so the `_test` database interlock stays in one
place.
"""

from __future__ import annotations

import sys
from pathlib import Path

from tests.integration.conftest import (  # noqa: F401
    _database_test_lock,
    clean,
    database,
    integration_settings,
    other_project,
    prepare,
    project,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
