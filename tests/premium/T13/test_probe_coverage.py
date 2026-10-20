"""T13-AC3: the shared probe table can address every versioned route.

`probe_path` refuses a path whose placeholder it was not taught, so a missing
stand-in becomes an assertion at the probe instead of a request to a literal
`{source_id}` that nothing serves. The all-route probes enumerate whatever the
application publishes; this test checks the other half of that contract — that
the table they share has kept up with the `/v1` surface.

The two values a route cannot invent are the project and the node, which every
caller already holds as a fixture. Everything else must come from
`ABSENT_PLACEHOLDERS`, or this test names the placeholder that is missing.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient
from tests.premium.T13.support import EXPECTED_V1
from tests.support.routes import ABSENT_PLACEHOLDERS, effective_routes, probe_path

pytestmark = pytest.mark.integration

#: The placeholders a probe must supply from its own fixture. A versioned route
#: that needs a third real value is a route the shared probes cannot enumerate,
#: and adding it here is a deliberate act rather than a silent default.
CALLER_SUPPLIED = {
    "project_id": "prj_probe_coverage",
    "task_id": "tsk_probe_coverage",
}


def test_every_effective_v1_route_can_be_addressed_by_the_shared_probe_table(
    client: TestClient,
) -> None:
    """No `/v1` route may carry a placeholder the probes have not been taught.

    The route set is compared with the contract's path set first, so the walk is
    known to have seen the versioned surface rather than a fragment of it. Then
    each path is filled exactly the way `probe_path` fills it. A route added
    later with, say, `{report_id}` fails here instead of in four probes whose
    failures each look like a product bug.
    """
    routes = effective_routes(client.app)  # type: ignore[arg-type]
    versioned = [route for route in routes if route.path.startswith("/v1/")]
    assert {route.path for route in versioned} == set(EXPECTED_V1)
    operations = {
        (route.path, method.lower())
        for route in versioned
        for method in route.methods
    }
    assert operations == {
        (path, method)
        for path, methods in EXPECTED_V1.items()
        for method in methods
    }

    taught = set(ABSENT_PLACEHOLDERS) | set(CALLER_SUPPLIED)
    for route in versioned:
        placeholders = set(re.findall(r"\{([^}]+)\}", route.path))
        missing = placeholders - taught
        assert missing == set(), (
            f"{route.path} needs {sorted(missing)}; teach `ABSENT_PLACEHOLDERS` "
            f"in tests/support/routes.py or supply it from the probe fixture"
        )
        addressed = probe_path(route.path, **CALLER_SUPPLIED)
        assert "{" not in addressed
        assert addressed.startswith("/v1/")
