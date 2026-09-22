"""Fixtures for tests that drive a real pinned harness.

These tests are not mocked and not simulated: they spawn the pinned `dsh`
runtime, hand it a real generated overlay, and let a real model call RAVEL's
tool server. They are marked `dsh` and `live` because they cost money and
network, and because a fake version of them would prove nothing.

A missing credential skips rather than fails, so the suite stays runnable on a
machine without one. Set `RAVEL_REQUIRE_DSH=1` to make the absence a failure
instead — that is what the Phase 0 gate runs with, so the gate cannot pass by
being skipped.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import inspect

from ravel.config import Settings
from ravel.domain.events import ActorType
from ravel.dsh.pool import DshRuntimePool
from ravel.state.database import Database, create_db_engine
from ravel.state.repositories.projects import ProjectRegistry
from ravel.state.tables import Base


@pytest.fixture(scope="session")
def model_credential() -> str:
    """The model credential a live harness needs, or a skip."""
    key = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    if key:
        return key
    message = (
        "DEEPSEEK_API_KEY is unset, so no live harness turn can run. Export it "
        "(and set RAVEL_REQUIRE_DSH=1 to make this a failure rather than a skip)."
    )
    if os.environ.get("RAVEL_REQUIRE_DSH"):
        pytest.fail(message)
    pytest.skip(message)


@pytest.fixture(scope="session")
def live_settings(tmp_path_factory: pytest.TempPathFactory, model_credential: str) -> Settings:
    """Settings for a live harness whose scratch state is temporary.

    `dsh_home` stays at its configured location on purpose: it holds the
    harness's own state and is shared by every runtime RAVEL launches. Only
    RAVEL's runtime root moves, so a test never leaves a working directory
    behind in the checkout.
    """
    scratch = tmp_path_factory.mktemp("dsh-spike")
    settings = Settings(runtime_dir=scratch / "runtime")
    _refuse_an_unmigrated_database(settings)
    return settings


def _refuse_an_unmigrated_database(settings: Settings) -> None:
    """Fail before the first live turn if the database is behind the models.

    These cases run against the database `.env` names, not the test one. Every
    other suite here is pointed at `ravel_test` and builds its schema with
    `create_all`, so a model added with a table is there without anybody
    running a migration. This suite is not: it is the deployment path, and the
    database it reads is migrated by `make migrate` or not at all. A table the
    models declare and the database lacks therefore does not fail as a missing
    table — it fails inside a tool, and the agent reports `Error executing tool
    read_project_state` and answers honestly that it cannot read the state.
    The case then fails on a sentence about the model, which is wrong about
    both the model and the fault.

    Checked once per session, because the answer cannot change under a suite
    that never writes DDL. It compares table *names* only; whether a table that
    exists still carries every constraint the models declare is the question
    `tests/integration/conftest.py` asks of the database it creates itself.

    Raises:
        RuntimeError: A table the models declare is absent from the database.
    """
    engine = create_db_engine(settings)
    try:
        present = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    missing = sorted(set(Base.metadata.tables) - present)
    if missing:
        raise RuntimeError(
            "the database this suite is pointed at is older than the models, "
            "so these tables are missing: "
            + ", ".join(missing)
            + f". It is {settings.postgres_db!r}, which the live suites share "
            "with a real deployment and nothing here migrates; run "
            "`make migrate`."
        )


@pytest.fixture
def pool(live_settings: Settings) -> Iterator[DshRuntimePool]:
    """A pool whose runtimes are all shut down when the test ends.

    Function-scoped on purpose: a leaked Node process would make the next test's
    result depend on this one's, and a harness runtime is exactly the kind of
    resource that outlives a careless assertion.
    """
    with DshRuntimePool(settings=live_settings) as open_pool:
        yield open_pool


#: Sentinel written into the DSH spike brief. Tests that read authoritative
#: state assert the model reports this exact title.
TITLE_SENTINEL = "Spike Project ZQ7"


@pytest.fixture
def project_id(request: pytest.FixtureRequest) -> str:
    """A project id unique to the test, with a matching row in the database.

    The harness's tool server reads the same process settings as this fixture,
    so both sides target the same database. The project is created here so that
    tools such as `read_project_state` have authoritative state to return.
    """
    slug = "".join(ch if ch.isalnum() else "-" for ch in request.node.name).strip("-")
    # The project_id column is String(32). "proj-" is 5 characters, leaving 27
    # for the slug. This keeps the id deterministic per test while fitting the
    # schema.
    project_id = f"proj-{slug[:27]}"

    db = Database.from_settings()
    try:
        with db.transaction() as session:
            existing = ProjectRegistry(session).find(project_id)
            if existing is not None:
                # A previous failed or partial run left the row behind. Reusing
                # the same id keeps the test deterministic and avoids collisions
                # with other test projects.
                return project_id
            ProjectRegistry(session).create(
                project_id=project_id,
                title=TITLE_SENTINEL,
                objective="DSH integration spike objective.",
                created_by="dsh-spike-test",
                actor_type=ActorType.USER,
            )
        return project_id
    finally:
        db.dispose()


@pytest.fixture
def scratch_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("dsh-scratch")
