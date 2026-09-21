"""Fixtures for driving each role's real tool server against the real database.

Nothing here is simulated. The server under test is the one the harness spawns,
started by the same `python -m ravel.mcp.server`, over the same stdio transport,
with the same environment a launched runtime gives it — pointed at the test
database. What a role can reach is read off the wire, and what it wrote is read
back out of PostgreSQL.

The database coordinates have to be passed explicitly because the server is a
separate process: it does not inherit the suite's `Settings` object, it reads
the environment, and the environment it reads must be the test database rather
than whatever `RAVEL_*` the developer happens to have exported. The runtime's
own composition passes the same mapping (`Settings.tool_server_env`), which is
why this fixture reads it from there rather than listing the keys again.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from ravel.config import Settings
from ravel.domain.project import Project
from ravel.domain.roles import AgentRole


@dataclass(frozen=True, slots=True)
class RoleEnvironment:
    """Builds the environment one role's tool server is launched with."""

    settings: Settings
    brief_dir: Path

    def for_role(self, role: AgentRole, project_id: str, *, title: str = "") -> dict[str, str]:
        """The environment a `(project, role)` server is launched with.

        The state coordinates are `Settings.tool_server_env()` rather than a
        list kept here, because the runtime now launches its own servers with
        exactly that mapping: a fixture that built its own could drift from the
        composition, and the drift would read as a passing test of a server
        launched differently from the one a deployment runs.
        """
        brief = self.brief_dir / f"{role.value}.brief.json"
        brief.parent.mkdir(parents=True, exist_ok=True)
        brief.write_text(
            json.dumps(
                {
                    "project_id": project_id,
                    "project_title": title or project_id,
                    "role": role.value,
                }
            ),
            encoding="utf-8",
        )
        return {
            **self.settings.tool_server_env(),
            "RAVEL_PROJECT_ID": project_id,
            "RAVEL_ROLE": role.value,
            "RAVEL_BRIEF_FILE": str(brief),
        }

    def for_project(self, project: Project, role: AgentRole) -> dict[str, str]:
        """The environment for a role serving a project that exists."""
        return self.for_role(role, project.project_id, title=project.title)


@pytest.fixture
def role_environment(integration_settings: Settings, tmp_path: Path) -> RoleEnvironment:
    return RoleEnvironment(settings=integration_settings, brief_dir=tmp_path / "briefs")
