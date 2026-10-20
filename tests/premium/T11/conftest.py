"""T11 runs against the real stack: PostgreSQL, the object store, the Gateway.

The fixtures are the integration suite's own, re-exported rather than copied.
A T11 test that built its own project or its own handover row would be
asserting against a fixture instead of the composition: these routes are
supposed to write person-reported events for a package the production
preparer built, and only the shared fixtures guarantee that.

The fixtures this suite adds live in `support`, because a conftest that
defines a fixture named `database` while also re-exporting the integration one
is a name collision rather than a fixture.
"""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401
    Prepared,
    _database_test_lock,
    artifact_store,
    clean,
    database,
    integration_settings,
    other_project,
    prepare,
)
from tests.integration.gateway.conftest import (  # noqa: F401
    PASSWORD,
    ScriptedDeliveries,
    _isolated,
    account,
    app,
    bearer,
    client,
    deliveries,
    gateway_settings,
    sign_in,
    tokens,
)
from tests.support.lab import Handed, terms

from .support import (  # noqa: F401
    handed,
    lab_headers,
    lab_user,
    owner_headers,
    project,
    terms_with_sop,
)

__all__ = [
    "Handed",
    "Prepared",
    "ScriptedDeliveries",
    "account",
    "client",
    "deliveries",
    "handed",
    "lab_headers",
    "owner_headers",
    "sign_in",
    "terms",
    "terms_with_sop",
]
