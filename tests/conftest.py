"""Shared test fixtures.

Tests never touch the developer's real runtime directory or the developer's
`.env` secrets: a fixture hands out a `Settings` rooted in a temporary tree.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ravel.config import REPO_ROOT, Settings


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Settings whose runtime state lives in a temporary directory."""
    return Settings(
        runtime_dir=tmp_path / "runtime",
        dsh_home=tmp_path / "runtime" / "dsh_home",
        # Addressed by alias: these tests must not pick up an ambient key.
        DEEPSEEK_API_KEY=None,
    )


@pytest.fixture
def repo_root() -> Path:
    """The checkout under test."""
    return REPO_ROOT


@pytest.fixture
def live_env() -> dict[str, str]:
    """Environment for a RAVEL tool server process."""
    return {
        "RAVEL_PROJECT_ID": "proj-test",
        "RAVEL_ROLE": "master",
    }
