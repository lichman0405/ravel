"""What the operator's screen is allowed to claim, without a database in the way.

These need no PostgreSQL, no MinIO and no Temporal, which is the point: they are
assertions about what a diagnostic reports when the estate is *not* healthy, and
a test that needed the estate up to check them would be checking the wrong
thing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr

from ravel.config import REPO_ROOT, Settings
from ravel.domain.enums import JobState
from ravel.domain.state_machines import JOB_TRANSITIONS
from ravel.gateway.routes.admin import UNFINISHED, router
from ravel.gateway.runtime import PIN_PATH, harness_home, read_pin


def _settings(**overrides: object) -> Settings:
    """Settings without reading the environment, so a test is about its own subject."""
    values: dict[str, object] = {
        "database_url": "postgresql+psycopg://ravel:ravel@localhost:55432/ravel_test",
        "gateway_jwt_secret": SecretStr("a-secret-long-enough-to-sign-with"),
        "dsh_home": ".ravel-test-home-does-not-exist",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


# ── The pin ─────────────────────────────────────────────────────────────────


def test_the_pin_file_is_where_the_deployment_says_it_is() -> None:
    """Read rather than repeated, so the screen and the tests cannot disagree."""
    pin = read_pin()

    assert pin is not None, f"no pin file at {PIN_PATH}"
    assert pin["harness"] == "DeepSeek Harness"
    assert pin["pin"]["commit"]
    assert pin["patches"]["required"] is False


def test_a_pin_file_that_is_missing_or_unreadable_is_answered_with_nothing(
    tmp_path: Path,
) -> None:
    """A checkout without the pin file is a deployment with something wrong.

    The screen should show that as a missing field. Raising here would turn a
    diagnostic into a 500, and an operator would learn that something is broken
    without learning which thing.
    """
    assert read_pin(tmp_path / "absent.json") is None

    broken = tmp_path / "broken.json"
    broken.write_text("{not json at all", encoding="utf-8")
    assert read_pin(broken) is None

    directory = tmp_path / "a-directory"
    directory.mkdir()
    assert read_pin(directory) is None


# ── The home ────────────────────────────────────────────────────────────────


def test_resolving_the_harness_home_does_not_create_it(tmp_path: Path) -> None:
    """A health check that creates what it checks stops measuring.

    `Settings.dsh_home_path` makes the directory on the way to returning it, so
    a screen that used it would report `home_exists: true` from the second read
    onwards — which is exactly when the answer has stopped being worth having.
    """
    settings = _settings(dsh_home=str(tmp_path / "nowhere" / "dsh"))

    resolved = harness_home(settings)

    assert resolved == (REPO_ROOT / str(tmp_path / "nowhere" / "dsh")).resolve()
    assert not resolved.exists(), "resolving the path created it"


# ── The vocabulary ──────────────────────────────────────────────────────────


def test_unfinished_is_every_job_state_with_somewhere_left_to_go() -> None:
    """Derived from the transition table, because a hand-written list goes stale.

    A terminal state is a state with no way out of it, which the domain already
    knows. The two sets must together be exactly the vocabulary: a state in
    neither would be invisible to an operator counting what needs attention, and
    a state in both would be counted as unfinished forever.
    """
    terminal = {state for state, onward in JOB_TRANSITIONS.items() if not onward}

    assert terminal == {
        JobState.CANCELLED,
        JobState.COMPLETED,
        JobState.FAILED,
        JobState.TIMED_OUT,
    }
    assert set(JobState) == UNFINISHED | terminal
    assert not (UNFINISHED & terminal)


# ── What the admin surface is ───────────────────────────────────────────────


def test_every_administrator_route_is_a_read() -> None:
    """*Admin does not make scientific decisions*, asserted against the routes.

    Every route in the admin module answers GET. A POST appearing here would be
    the Gateway growing an operational write — a restart, a retry, a cancel —
    and those are things an operator does to the machine rather than decisions
    about a project's research. The day one is added deliberately, this test is
    where that gets recorded.
    """
    methods = {
        method
        for route in router.routes
        for method in (getattr(route, "methods", None) or set()) - {"HEAD", "OPTIONS"}
    }

    assert methods == {"GET"}
    assert len(router.routes) >= 4, "the admin surface shrank to almost nothing"


@pytest.mark.parametrize("field", ["dsh_provider", "dsh_model"])
def test_the_harness_screen_names_the_configured_provider(field: str) -> None:
    """Which model is answering is a fact about the deployment an operator needs.

    It is also the field most likely to be wrong after somebody edits an
    environment file and forgets to restart, so it is reported from the settings
    the process actually loaded rather than from the file on disk.
    """
    settings = _settings()

    assert getattr(settings, field)
