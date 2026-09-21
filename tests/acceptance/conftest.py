"""Fixtures for the suite that demonstrates V0's twenty acceptance items.

`acceptance/V0_ACCEPTANCE.md` opens with "V0 is not complete until all 20 items
pass", and this directory is where each of them is made to pass in one run.
The items are end-to-end by construction — a project is created, planned,
executed, reviewed, replanned and ended; a bench reports something the plan did
not allow and Master answers it — so the fixtures come from `tests/e2e`, which
already assembles the real stack: PostgreSQL, Temporal, a worker, MinIO, the
mock backends, and a real uvicorn for the Gateway.

Nothing is re-defined here. The one fixture this directory adds is the one the
other suites do not have a name for: a project that is empty and *known* empty,
so that "the audit trail of this run" means this run's trail.

**Every case starts from an empty database.** `_isolated` is autouse rather
than pulled in by whichever fixtures happen to need it, for the reason
`tests/integration/gateway/conftest.py` gives: usernames and project titles are
unique, so a case that registered `ada` would fail the next one on a constraint
rather than on the thing it was testing, and the failure would name the wrong
thing entirely.
"""

from __future__ import annotations

# Fixtures are imported and then used as fixture parameters, which ruff reads as
# a redefinition. That is the pytest idiom, and it is how every suite in this
# tree re-exports another suite's fixtures.
# ruff: noqa: F811
import pytest
from tests.acceptance.phase10_support import ScriptedSeats
from tests.e2e.conftest import (  # noqa: F401
    PASSWORD,
    SECRET,
    TTL_SECONDS,
    Headless,
    LiveGateway,
    ScriptedMaster,
    ScriptedMasterPort,
    ScriptedReview,
    Task,
    account,
    artifact_store,
    bearer,
    clean,
    database,
    execution_settings,
    gateway_settings,
    headless,
    # `tests/e2e/conftest.py` shortens this suite's execution poll interval and
    # keeps the integration fixture under this name so that the fixture it
    # derives from is still reachable — which is what `headless` above depends
    # on, and why it has to travel with it.
    integration_execution_settings,
    integration_settings,
    live_gateway,
    mock_clock,
    project,
    sign_in,
    temporal_unreachable,
    tokens,
)

# A18's subject is a project with a DAG in it and three people looking at it,
# and `tests/e2e/test_tui.py` already builds exactly that. Rebuilt here it would
# be a second definition of the owner/lab/admin arrangement, and the two would
# drift the first time a membership rule changed — which is the failure this
# fixture exists to catch. The console and the helpers that drive it are
# imported by the test that uses them; only the world is shared.
from tests.e2e.test_tui import world  # noqa: F401

# `tests/e2e/conftest.py` has no use for a node that is only ready to run, so
# this one comes from the gate below it rather than from the loop above: A14 is
# about criteria that were frozen before a node ran, and `prepare` is the
# fixture that builds that state the way production does.
from tests.integration.backends.conftest import (  # noqa: F401
    # `mock_clock` is already here by way of `tests/e2e/conftest.py`, which
    # re-exports this same fixture object; the two other names are the mocks
    # themselves. The gates sweep every scenario the acceptance catalogue
    # names, which is a question about the backends rather than about a
    # project, so they drive the backends directly.
    compute,
    lab,
    prepare,
)
from tests.integration.conftest import research_task  # noqa: F401
from tests.integration.review.conftest import (  # noqa: F401
    # A08 is about what a verdict is measured *against*, so its node has to be
    # in REVIEWING the way production gets there. `driving` walks the real
    # transitions rather than writing the status, and `reviews_of`/`status_of`
    # read the outcome back out of PostgreSQL. Only the four are taken: the rest
    # of that module's names are re-exports of fixtures this file already has,
    # and importing them would shadow the loop's own copies.
    driving,
    reviews_of,
    status_of,
    submit,
)

# A03 and A04 are live by definition — the item says "no mock search", and a
# substituted source would satisfy every assertion about *shape* while proving
# nothing. These two come from the suite that is never mocked, and they carry
# its refusal with them: `live_settings` skips the case when RAVEL has no
# contact address to fetch under, because fetching anonymously is the thing
# this project does not do. Only the two are taken; that module re-exports the
# same shared fixtures this file already has.
from tests.live_research.conftest import (  # noqa: F401
    live_role_environment,
    live_settings,
)

from ravel.config import Settings
from ravel.state.database import Database

pytestmark = pytest.mark.acceptance


@pytest.fixture(autouse=True)
def _isolated(clean: None) -> None:
    """Start every acceptance case from an empty database."""


@pytest.fixture
def seats(database: Database, execution_settings: Settings) -> ScriptedSeats:
    """The five seats of every project a phase-10 supervisor drives, scripted.

    The settings travel with them because a Worker's seat holds a real
    Execution Service, and which task queue a run lands on is the deployment's
    answer rather than the seat's.
    """
    return ScriptedSeats(database=database, settings=execution_settings)


__all__ = [
    "PASSWORD",
    "SECRET",
    "TTL_SECONDS",
    "Headless",
    "LiveGateway",
    "ScriptedMaster",
    "ScriptedMasterPort",
    "ScriptedReview",
    "Task",
]
