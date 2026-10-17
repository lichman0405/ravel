"""T04: one live driver per project, a monotonic epoch, and a hard fence."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from ravel.config import Settings
from ravel.domain.project import Project
from ravel.execution.leases import (
    LeaseGuard,
    LeaseHeld,
    LeaseLost,
    LeaseService,
)
from ravel.execution.supervisor import ProjectSupervisor
from ravel.state.database import Database


def _expire(database: Database, project_id: str) -> None:
    """Move the lease into the past using the database clock."""
    with database.transaction() as session:
        session.execute(
            text(
                "UPDATE project_leases "
                "SET expires_at = now() - interval '1 second' "
                "WHERE project_id = :project_id"
            ),
            {"project_id": project_id},
        )


def test_two_supervisors_only_one_acquires(database: Database, project: Project) -> None:
    first = LeaseService(database)
    second = LeaseService(database)

    lease = first.acquire(project.project_id, "supervisor-a")
    assert lease.epoch == 1
    assert first.check(project.project_id, "supervisor-a", lease.epoch) is True

    with pytest.raises(LeaseHeld):
        second.acquire(project.project_id, "supervisor-b")
    assert second.check(project.project_id, "supervisor-b", 1) is False


def test_expired_lease_is_taken_over_with_a_larger_epoch(
    database: Database, project: Project
) -> None:
    first = LeaseService(database)
    second = LeaseService(database)
    old = first.acquire(project.project_id, "supervisor-a")

    _expire(database, project.project_id)
    assert first.check(project.project_id, "supervisor-a", old.epoch) is False

    new = second.acquire(project.project_id, "supervisor-b")
    assert new.epoch == old.epoch + 1
    assert second.check(project.project_id, "supervisor-b", new.epoch) is True

    with pytest.raises(LeaseLost):
        first.renew(project.project_id, "supervisor-a", old.epoch)
    assert first.release(project.project_id, "supervisor-a", old.epoch) is False


def test_release_keeps_the_epoch_monotonic(database: Database, project: Project) -> None:
    first = LeaseService(database)
    second = LeaseService(database)
    old = first.acquire(project.project_id, "supervisor-a")
    assert first.release(project.project_id, "supervisor-a", old.epoch) is True
    assert first.check(project.project_id, "supervisor-a", old.epoch) is False

    new = second.acquire(project.project_id, "supervisor-b")
    assert new.epoch == old.epoch + 1
    assert first.release(project.project_id, "supervisor-a", old.epoch) is False


def test_different_projects_can_be_held_in_parallel(
    database: Database, project: Project, other_project: Project
) -> None:
    first = LeaseService(database)
    second = LeaseService(database)

    one = first.acquire(project.project_id, "supervisor-a")
    two = second.acquire(other_project.project_id, "supervisor-b")
    assert one.project_id != two.project_id
    assert first.check(project.project_id, "supervisor-a", one.epoch) is True
    assert second.check(other_project.project_id, "supervisor-b", two.epoch) is True


def test_guard_stops_after_the_lease_expires(database: Database, project: Project) -> None:
    service = LeaseService(database)
    lease = service.acquire(project.project_id, "supervisor-a")
    guard = LeaseGuard(
        service=service,
        project_id=project.project_id,
        owner_instance_id="supervisor-a",
        epoch=lease.epoch,
        renew_seconds=0.0,
    )
    assert guard.ensure() is True
    _expire(database, project.project_id)
    assert guard.ensure() is False


def test_supervisor_claim_is_a_gate_before_agents(
    database: Database, project: Project, integration_settings: Settings
) -> None:
    first = ProjectSupervisor(database=database, settings=integration_settings)
    second = ProjectSupervisor(database=database, settings=integration_settings)

    first_lease = first._claim_project(project.project_id)
    assert first_lease is not None
    assert first._leases[project.project_id] == first_lease.epoch
    assert second._claim_project(project.project_id) is None
