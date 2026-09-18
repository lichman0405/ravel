"""Fixtures for driving each role's real tool server against the real database.

Nothing here is simulated. The server under test is the one the harness spawns,
started by the same `python -m ravel.mcp.server`, over the same stdio transport,
with the same three environment variables — pointed at the test database. What
a role can reach is read off the wire, and what it wrote is read back out of
PostgreSQL.

The database coordinates have to be passed explicitly because the server is a
separate process: it does not inherit the suite's `Settings` object, it reads
the environment, and the environment it reads must be the test database rather
than whatever `RAVEL_*` the developer happens to have exported.
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

        `RAVEL_POSTGRES_DSN` is set to the empty string rather than omitted:
        an ambient override in the developer's shell would otherwise outrank
        the test database in the child's settings, and the suite would write to
        a real project.
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
            "RAVEL_ENV": "test",
            "RAVEL_POSTGRES_HOST": self.settings.postgres_host,
            "RAVEL_POSTGRES_PORT": str(self.settings.postgres_port),
            "RAVEL_POSTGRES_DB": self.settings.postgres_db,
            "RAVEL_POSTGRES_USER": self.settings.postgres_user,
            "RAVEL_POSTGRES_PASSWORD": self.settings.postgres_password.get_secret_value(),
            "RAVEL_POSTGRES_DSN": "",
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
