"""Fixtures for the suite that is never mocked.

This suite is the reason the rest of the research stack can be trusted. Unit
tests prove the parsers and the state machines behave; only this suite proves
that RAVEL, pointed at the open Internet, opens real sources and writes down
what it actually read.

It therefore runs against the real stack — real PostgreSQL, real MinIO, real
Crossref, real arXiv, real PubChem — and it skips rather than substitutes when
one of those is unavailable. There is no fixture here that returns canned
bytes. If a source cannot be reached, the test that needed it says so, and the
suite reports fewer assertions rather than passing on invented data.
"""

# Fixtures are imported and then used as fixture parameters, which ruff reads as
# a redefinition. That is the pytest idiom, so the rule is off for this file
# rather than silenced five times.
# ruff: noqa: F811

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

# Imported so that pytest finds them in this directory too. This suite needs
# the same real database, object store and project that the integration tests
# use — it is the same stack, reached for the same reason — and duplicating
# those fixtures would let the two definitions drift.
from tests.integration.conftest import (  # noqa: F401
    artifact_store,
    clean,
    database,
    integration_settings,
    prepare,
    project,
    research_task,
)
from tests.integration.roles.conftest import RoleEnvironment

from ravel.config import Settings
from ravel.domain.project import Project
from ravel.research.gateway import ResearchSourceGateway
from ravel.state.database import Database
from ravel.state.store import ArtifactStore


@pytest.fixture(scope="session")
def live_settings(integration_settings: Settings) -> Settings:
    """Settings with a contact address, or a skip explaining why not.

    RAVEL refuses to fetch anonymously, and the refusal is deliberate: every
    service this suite uses asks to be able to contact a client that makes many
    requests, and the alternative is joining the pool that gets throttled. A
    placeholder address would defeat the point of asking, so the suite stops
    instead.
    """
    if not (integration_settings.research_contact_email or "").strip():
        pytest.skip(
            "RAVEL_RESEARCH_CONTACT_EMAIL is unset, and RAVEL will not fetch "
            "anonymously. Crossref, OpenAlex and NCBI route identified clients to "
            "a faster pool and ask for a contact address; set it in .env to run "
            "this suite."
        )
    return integration_settings


@pytest.fixture
def gateway(
    database: Database,
    live_settings: Settings,
    artifact_store: ArtifactStore,
    project: Project,
) -> Iterator[ResearchSourceGateway]:
    """A gateway for one project, with a real store and no browser.

    The browser is left unconstructed on purpose. Nothing in this suite asserts
    what a rendered page contains, and a navigator that is never asked to launch
    keeps the suite runnable on a host where Chromium cannot start.
    """
    with Session(bind=database.engine) as session:
        yield ResearchSourceGateway(
            session=session,
            project_id=project.project_id,
            settings=live_settings,
            store=artifact_store,
        )


@pytest.fixture
def actor_id() -> str:
    """Who is doing the research. Any identified actor will do here."""
    return "live-research-suite"


@pytest.fixture
def live_role_environment(live_settings: Settings, tmp_path: Path) -> RoleEnvironment:
    """The role environment the integration suites use, on live settings.

    The same builder — a tool server is launched identically here and there —
    pointed at the settings that carry a contact address. That is the whole
    difference between the two, and it is load-bearing: `RoleEnvironment` passes
    the address through to the child, and a Research session launched without
    one refuses to fetch. The refusal is correct behaviour and the wrong test.
    """
    return RoleEnvironment(settings=live_settings, brief_dir=tmp_path / "briefs")
