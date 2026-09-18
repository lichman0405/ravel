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
from pathlib import Path

import pytest

from ravel.config import Settings
from ravel.dsh.pool import DshRuntimePool


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
    return Settings(runtime_dir=scratch / "runtime")


@pytest.fixture
def pool(live_settings: Settings) -> DshRuntimePool:
    """A pool whose runtimes are all shut down when the test ends.

    Function-scoped on purpose: a leaked Node process would make the next test's
    result depend on this one's, and a harness runtime is exactly the kind of
    resource that outlives a careless assertion.
    """
    with DshRuntimePool(settings=live_settings) as open_pool:
        yield open_pool


@pytest.fixture
def project_id(request: pytest.FixtureRequest) -> str:
    """A project id unique to the test, so scopes cannot collide across tests."""
    slug = "".join(ch if ch.isalnum() else "-" for ch in request.node.name).strip("-")
    return f"proj-{slug[:48]}"


@pytest.fixture
def scratch_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("dsh-scratch")
