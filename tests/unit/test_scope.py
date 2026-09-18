"""The tool server's authority comes from its environment, and nowhere else.

These tests are the ones that matter most in the whole unit suite: if a scope
can be defaulted, guessed, or supplied by the caller, then "project-scoped
authorization > model-supplied project_id" is a slogan rather than a property.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ravel.dsh.roles import AgentRole
from ravel.mcp.scope import RuntimeBrief, ScopeError, ToolScope


def test_scope_requires_a_project(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RAVEL_PROJECT_ID", raising=False)
    monkeypatch.setenv("RAVEL_ROLE", "master")
    with pytest.raises(ScopeError, match="RAVEL_PROJECT_ID is unset"):
        ToolScope.from_environment()


def test_scope_rejects_a_blank_project(monkeypatch: pytest.MonkeyPatch) -> None:
    """A blank value is the documented way to say "unconfigured"."""
    monkeypatch.setenv("RAVEL_PROJECT_ID", "   ")
    monkeypatch.setenv("RAVEL_ROLE", "master")
    with pytest.raises(ScopeError):
        ToolScope.from_environment()


def test_scope_rejects_an_unknown_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAVEL_PROJECT_ID", "proj-a")
    monkeypatch.setenv("RAVEL_ROLE", "planner")
    with pytest.raises(ScopeError, match="is not a RAVEL role"):
        ToolScope.from_environment()


def test_scope_rejects_a_missing_role(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAVEL_PROJECT_ID", "proj-a")
    monkeypatch.delenv("RAVEL_ROLE", raising=False)
    with pytest.raises(ScopeError):
        ToolScope.from_environment()


def test_scope_reads_an_explicit_environment() -> None:
    scope = ToolScope.from_environment(
        {"RAVEL_PROJECT_ID": "proj-a", "RAVEL_ROLE": "compute-worker"}
    )
    assert scope.project_id == "proj-a"
    assert scope.role is AgentRole.COMPUTE_WORKER
    assert scope.brief_path is None


def test_only_master_scope_may_mutate_the_dag() -> None:
    for role in AgentRole:
        scope = ToolScope.from_environment({"RAVEL_PROJECT_ID": "proj-a", "RAVEL_ROLE": role.value})
        assert scope.is_master is (role is AgentRole.MASTER)


def test_brief_defaults_when_no_file_was_written() -> None:
    scope = ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=None)
    brief = scope.brief()
    assert brief.project_id == "proj-a"
    assert brief.project_title == "proj-a"
    assert brief.bindings == ()


def test_brief_defaults_when_the_file_is_absent(tmp_path: Path) -> None:
    scope = ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=tmp_path / "gone.json")
    assert scope.brief().project_id == "proj-a"


def test_brief_is_read_when_present(tmp_path: Path) -> None:
    path = tmp_path / "brief.json"
    path.write_text(
        json.dumps(
            {
                "project_id": "proj-a",
                "project_title": "Protein Folding Study",
                "role": "master",
                "objective": "Determine the folding pathway.",
                "phase": "RESEARCH",
                "bindings": [{"session_id": "s1", "role": "research"}],
            }
        ),
        encoding="utf-8",
    )
    brief = ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=path).brief()
    assert brief.project_title == "Protein Folding Study"
    assert brief.objective == "Determine the folding pathway."
    assert brief.phase == "RESEARCH"
    assert brief.bindings == ({"session_id": "s1", "role": "research"},)


def test_brief_naming_another_project_is_refused(tmp_path: Path) -> None:
    """A brief that disagrees with the process scope is a wiring fault."""
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"project_id": "proj-b", "role": "master"}), encoding="utf-8")
    with pytest.raises(ScopeError, match="names project 'proj-b'"):
        ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=path).brief()


def test_brief_naming_another_role_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"project_id": "proj-a", "role": "master"}), encoding="utf-8")
    with pytest.raises(ScopeError, match="names role 'master'"):
        ToolScope(project_id="proj-a", role=AgentRole.RESEARCH, brief_path=path).brief()


def test_brief_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "brief.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ScopeError, match="not valid JSON"):
        ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=path).brief()


def test_brief_that_is_not_an_object_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "brief.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ScopeError, match="must be a JSON object"):
        ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=path).brief()


def test_brief_with_a_non_list_bindings_field_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"project_id": "proj-a", "bindings": "s1"}), encoding="utf-8")
    with pytest.raises(ScopeError, match="non-list 'bindings'"):
        ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=path).brief()


def test_brief_with_absent_bindings_is_empty_not_an_error(tmp_path: Path) -> None:
    """Absent and empty mean the same thing: nothing is bound yet."""
    path = tmp_path / "brief.json"
    path.write_text(json.dumps({"project_id": "proj-a"}), encoding="utf-8")
    brief = ToolScope(project_id="proj-a", role=AgentRole.MASTER, brief_path=path).brief()
    assert brief.bindings == ()


def test_runtime_path_refuses_to_escape_the_runtime_root(tmp_path: Path) -> None:
    """Project ids arrive from the database and the API; they are not constants."""
    from ravel.config import Settings

    settings = Settings(runtime_dir=tmp_path / "runtime")
    for hostile in ("../..", "a/b", "..", "", "\\", "a\x00b"):
        with pytest.raises(ValueError, match="not usable as a path component"):
            settings.runtime_path("dsh_cwd", hostile)


def test_runtime_path_stays_under_the_root(tmp_path: Path) -> None:
    from ravel.config import Settings

    root = (tmp_path / "runtime").resolve()
    settings = Settings(runtime_dir=tmp_path / "runtime")
    path = settings.runtime_path("dsh_cwd", "proj-a", "master")
    assert path.is_dir()
    assert path.resolve().is_relative_to(root)


def test_runtime_brief_is_immutable() -> None:
    brief = RuntimeBrief(project_id="proj-a", project_title="A", role=AgentRole.MASTER)
    with pytest.raises(AttributeError):
        brief.project_id = "proj-b"  # type: ignore[misc]
