"""A session binding is how RAVEL knows what a session's output belongs to."""

from __future__ import annotations

import pytest

from ravel.dsh.binding import BindingError, SessionBindingRegistry
from ravel.dsh.roles import AgentRole


@pytest.fixture
def registry() -> SessionBindingRegistry:
    return SessionBindingRegistry()


def test_bind_records_the_scope(registry: SessionBindingRegistry) -> None:
    binding = registry.bind("s1", "proj-a", AgentRole.RESEARCH, task_id="task-1")
    assert binding.session_id == "s1"
    assert binding.project_id == "proj-a"
    assert binding.role is AgentRole.RESEARCH
    assert binding.task_id == "task-1"
    assert registry.get("s1") == binding


def test_bind_is_idempotent_for_the_same_scope(registry: SessionBindingRegistry) -> None:
    registry.bind("s1", "proj-a", AgentRole.RESEARCH)
    registry.bind("s1", "proj-a", AgentRole.RESEARCH)
    assert len(registry) == 1


@pytest.mark.parametrize(
    "scope",
    [
        ("proj-b", AgentRole.RESEARCH, None),
        ("proj-a", AgentRole.MASTER, None),
        ("proj-a", AgentRole.RESEARCH, "task-9"),
    ],
)
def test_rebinding_a_session_to_another_scope_is_refused(
    registry: SessionBindingRegistry,
    scope: tuple[str, AgentRole, str | None],
) -> None:
    """A session id is unique in the harness; a second meaning is a fault."""
    registry.bind("s1", "proj-a", AgentRole.RESEARCH)
    project_id, role, task_id = scope
    with pytest.raises(BindingError, match="refusing to rebind"):
        registry.bind("s1", project_id, role, task_id)
    # The original binding survives the refused call.
    assert registry.require("s1").project_id == "proj-a"


def test_require_rejects_an_unbound_session(registry: SessionBindingRegistry) -> None:
    with pytest.raises(BindingError, match="not bound"):
        registry.require("ghost")


def test_get_returns_none_for_an_unbound_session(registry: SessionBindingRegistry) -> None:
    assert registry.get("ghost") is None


def test_for_project_returns_only_that_projects_sessions(registry: SessionBindingRegistry) -> None:
    registry.bind("s1", "proj-a", AgentRole.MASTER)
    registry.bind("s2", "proj-a", AgentRole.RESEARCH)
    registry.bind("s3", "proj-b", AgentRole.MASTER)
    assert {b.session_id for b in registry.for_project("proj-a")} == {"s1", "s2"}


def test_for_project_is_oldest_first(registry: SessionBindingRegistry) -> None:
    for session_id in ("s3", "s1", "s2"):
        registry.bind(session_id, "proj-a", AgentRole.RESEARCH)
    created = [b.created_at for b in registry.for_project("proj-a")]
    assert created == sorted(created)


def test_for_scope_narrows_to_a_role(registry: SessionBindingRegistry) -> None:
    registry.bind("s1", "proj-a", AgentRole.MASTER)
    registry.bind("s2", "proj-a", AgentRole.COMPUTE_WORKER, task_id="task-1")
    registry.bind("s3", "proj-a", AgentRole.COMPUTE_WORKER, task_id="task-2")
    scoped = registry.for_scope("proj-a", AgentRole.COMPUTE_WORKER)
    assert {b.session_id for b in scoped} == {"s2", "s3"}
    narrowed = registry.for_scope("proj-a", AgentRole.COMPUTE_WORKER, "task-1")
    assert {b.session_id for b in narrowed} == {"s2"}


def test_release_forgets_one_session(registry: SessionBindingRegistry) -> None:
    registry.bind("s1", "proj-a", AgentRole.MASTER)
    released = registry.release("s1")
    assert released is not None and released.session_id == "s1"
    assert registry.get("s1") is None
    assert registry.release("s1") is None


def test_release_scope_forgets_one_role_only(registry: SessionBindingRegistry) -> None:
    """Reaping a runtime must not forget another role's live sessions."""
    registry.bind("s1", "proj-a", AgentRole.MASTER)
    registry.bind("s2", "proj-a", AgentRole.RESEARCH)
    registry.bind("s3", "proj-a", AgentRole.RESEARCH)
    assert registry.release_scope("proj-a", AgentRole.RESEARCH) == 2
    assert [b.session_id for b in registry.for_project("proj-a")] == ["s1"]


def test_release_project_forgets_every_role(registry: SessionBindingRegistry) -> None:
    registry.bind("s1", "proj-a", AgentRole.MASTER)
    registry.bind("s2", "proj-a", AgentRole.RESEARCH)
    registry.bind("s3", "proj-b", AgentRole.MASTER)
    assert registry.release_project("proj-a") == 2
    assert {b.session_id for b in registry.for_project("proj-b")} == {"s3"}


def test_as_dict_is_json_safe(registry: SessionBindingRegistry) -> None:
    import json

    payload = registry.bind("s1", "proj-a", AgentRole.COMPUTE_WORKER, "task-1").as_dict()
    json.dumps(payload)  # must not raise
    assert payload["role"] == "compute-worker"
    assert payload["role_name"] == "Compute Worker"
