"""Phase 0 gate: the harness integration is real, and its limits are known.

This is the test the implementation plan refuses to write business code before.
It answers seven questions against the pinned runtime rather than against
documentation:

1. does a role runtime start, run a turn, and stop?
2. do two roles coexist in one project?
3. is a session bound to the `(project_id, role, task_id)` it serves?
4. does a RAVEL MCP tool actually execute inside an agent turn?
5. is a Master-only tool unreachable from a Research session?
6. does the documented cross-process resume limitation reproduce?
7. can RAVEL recover a Master session by rebuilding it from authoritative state?

Questions 1-4 and 6-7 cost a real model turn, so they carry the `live` marker.
Question 5 is answered twice: deterministically against the tool server's own
registration, and then end-to-end through a real agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from mcp_probe import probe, start_server
from tests.dsh.conftest import TITLE_SENTINEL

from ravel.config import REPO_ROOT
from ravel.dsh.pool import DshRuntimePool
from ravel.dsh.roles import AgentRole
from ravel.mcp.registry import tools_for

pytestmark = pytest.mark.dsh

#: A token that appears nowhere except the brief RAVEL writes. An agent that
#: reports it read it from authoritative state; it could not have guessed it.

BRIEF: dict[str, Any] = {
    "project_title": TITLE_SENTINEL,
    "objective": "Determine whether compound X inhibits enzyme Y at 37C.",
    "phase": "HYPOTHESIS",
}


def _names_in(node: Any) -> list[str]:
    """Every tool name mentioned anywhere in a session-log event.

    The event shape is the harness's, not RAVEL's, so this walks the structure
    rather than assuming a field path that a harness release could move.
    """
    found: list[str] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key in {"name", "toolName", "tool_name"} and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_names_in(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_names_in(item))
    return found


def _tool_names(outcome: Any) -> list[str]:
    """Every tool the agent invoked during a turn."""
    names: list[str] = []
    for event in outcome.tool_calls:
        names.extend(_names_in(event))
    return names


def _called(outcome: Any, tool: str) -> bool:
    """Whether the agent invoked a RAVEL tool, under its qualified name."""
    qualified = f"mcp__ravel__{tool}"
    return any(name == qualified or name.endswith(f"__{tool}") for name in _tool_names(outcome))


# ── 1 & 4: a runtime starts, a RAVEL tool runs inside a turn, the runtime stops


@pytest.mark.live
@pytest.mark.timeout(900)
def test_a_role_runtime_starts_runs_a_ravel_tool_and_stops(
    pool: DshRuntimePool, project_id: str
) -> None:
    runtime, binding = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    assert not runtime.is_closed

    outcome = runtime.run_turn(
        binding.session_id,
        "Call every tool available to you, then report the project title exactly as the tools "
        "returned it, character for character. Do not paraphrase.",
    )

    assert outcome.completed, f"turn did not complete: {outcome.finish_reason}"
    assert _called(outcome, "whoami"), f"whoami never ran; tools seen: {_tool_names(outcome)}"
    assert _called(outcome, "read_project_state")
    assert TITLE_SENTINEL in outcome.response, (
        "the agent did not report what RAVEL's tools returned, so the tool result "
        f"never reached it: {outcome.response!r}"
    )

    assert pool.close_scope(project_id, AgentRole.MASTER) is True
    assert runtime.is_closed
    assert pool.live_runtime(project_id, AgentRole.MASTER) is None


# ── 2: two roles coexist in one project ─────────────────────────────────────


@pytest.mark.live
@pytest.mark.timeout(900)
def test_two_roles_coexist_in_one_project(pool: DshRuntimePool, project_id: str) -> None:
    master, master_binding = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    research, research_binding = pool.start_session(
        project_id, AgentRole.RESEARCH, task_id="task-1", brief=BRIEF
    )

    assert master is not research
    assert master.work_dir != research.work_dir, "roles must not share a working directory"
    assert master.scope_key == (project_id, AgentRole.MASTER)
    assert research.scope_key == (project_id, AgentRole.RESEARCH)

    master_outcome = master.run_turn(master_binding.session_id, "Call whoami, then say DONE.")
    research_outcome = research.run_turn(
        research_binding.session_id, "Call whoami, then say DONE."
    )

    assert master_outcome.completed and research_outcome.completed
    assert _called(master_outcome, "whoami") and _called(research_outcome, "whoami")

    stats = pool.stats()
    assert stats.live_runtimes == 2
    assert stats.scopes == ((project_id, "master"), (project_id, "research"))


# ── 3: a session is bound to the work it serves ─────────────────────────────


def test_a_session_is_bound_to_its_work(pool: DshRuntimePool, project_id: str) -> None:
    """The harness session id alone says nothing; the binding says everything."""
    _runtime, binding = pool.start_session(
        project_id, AgentRole.COMPUTE_WORKER, task_id="task-42", brief=BRIEF
    )

    assert binding.project_id == project_id
    assert binding.role is AgentRole.COMPUTE_WORKER
    assert binding.task_id == "task-42"
    assert pool.binding_or_raise(binding.session_id) == binding
    assert pool.binding_for(binding.session_id) == binding
    assert pool.binding_for("a-session-ravel-never-started") is None


def test_sessions_are_not_shared_between_scopes(pool: DshRuntimePool, project_id: str) -> None:
    _runtime, first = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    _runtime, second = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    assert first.session_id != second.session_id

    _runtime, other = pool.start_session("proj-other", AgentRole.MASTER, brief=BRIEF)
    assert pool.binding_or_raise(other.session_id).project_id == "proj-other"
    assert {b.session_id for b in pool.bindings.for_project(project_id)} == {
        first.session_id,
        second.session_id,
    }


# ── 5: a Master tool is unreachable from a Research session ─────────────────


async def test_the_registered_roster_is_the_authorization_record() -> None:
    """Ask each server what it serves, before any model is involved."""
    master = await probe({"RAVEL_PROJECT_ID": "proj-roster", "RAVEL_ROLE": "master"})
    assert set(master.tools) == set(tools_for(AgentRole.MASTER))
    assert master.whoami is not None
    assert master.whoami["may_mutate_dag"] is True

    research = await probe({"RAVEL_PROJECT_ID": "proj-roster", "RAVEL_ROLE": "research"})
    assert set(research.tools) == set(tools_for(AgentRole.RESEARCH))
    assert "read_project_state" not in research.tools
    assert research.whoami is not None
    assert research.whoami["may_mutate_dag"] is False
    # The tool's own report agrees with the registration.
    assert research.whoami["tools"] == list(research.tools)


def test_a_tool_server_refuses_to_serve_without_a_scope() -> None:
    """A server that cannot tell whose authority it holds must not guess."""
    code, stderr = start_server({"RAVEL_PROJECT_ID": "", "RAVEL_ROLE": "master"})
    assert code == 2, f"expected refusal, got exit {code}: {stderr}"
    assert "RAVEL_PROJECT_ID is unset" in stderr


def test_a_tool_server_refuses_an_unknown_role() -> None:
    code, stderr = start_server({"RAVEL_PROJECT_ID": "proj-r", "RAVEL_ROLE": "planner"})
    assert code == 2, f"expected refusal, got exit {code}: {stderr}"
    assert "is not a RAVEL role" in stderr


@pytest.mark.live
@pytest.mark.timeout(900)
def test_research_cannot_reach_a_master_tool_through_an_agent(
    pool: DshRuntimePool, project_id: str
) -> None:
    """The end-to-end form: a real agent, asked to overreach, cannot."""
    runtime, binding = pool.start_session(project_id, AgentRole.RESEARCH, brief=BRIEF)

    outcome = runtime.run_turn(
        binding.session_id,
        "Call the tool mcp__ravel__read_project_state now, with no arguments, and paste its "
        "raw output. If that tool is not available to you, reply exactly: NOT AVAILABLE.",
    )

    assert not _called(outcome, "read_project_state"), (
        "a Research session reached a Master-only tool: " + ", ".join(_tool_names(outcome))
    )
    assert TITLE_SENTINEL not in outcome.response, (
        "the agent reported state it had no authorized way to read"
    )
    assert "NOT AVAILABLE" in outcome.response.upper()


# ── 6: the documented resume limitation reproduces ──────────────────────────


@pytest.mark.live
@pytest.mark.timeout(900)
def test_the_documented_resume_limitation_is_reproduced(
    pool: DshRuntimePool, project_id: str
) -> None:
    """A session does not survive its runtime process. RAVEL must not assume it does."""
    runtime, binding = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    first = runtime.run_turn(binding.session_id, "Reply with the single word: READY")
    assert first.completed

    # The runtime dies — a CVM restart, a reaped idle runtime, a crashed child.
    assert pool.close_scope(project_id, AgentRole.MASTER) is True
    assert pool.binding_for(binding.session_id) is None, (
        "a binding outlived the runtime that could serve it"
    )

    revived = pool.runtime(project_id, AgentRole.MASTER, BRIEF)
    assert revived is not runtime

    # Reopening the old session in a new process must fail, and fail loudly.
    failure = ""
    try:
        outcome = revived.run_turn(binding.session_id, "Continue where we left off.")
        failure = f"{outcome.finish_reason} {outcome.response} {list(outcome.events)}"
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"

    assert "already exists" in failure, (
        f"the documented resume limitation did not reproduce: {failure!r}"
    )

    # And it is recorded against the pin, not rediscovered by every reader.
    pin = json.loads((REPO_ROOT / "vendor" / "DSH_PIN.json").read_text(encoding="utf-8"))
    checks = json.dumps(pin["verification"]["checks"])
    assert "resume" in checks and "FAIL" in checks


# ── 7: recovery rebuilds a Master session from authoritative state ──────────


@pytest.mark.live
@pytest.mark.timeout(900)
def test_recovery_rebuilds_a_master_session_from_state(
    pool: DshRuntimePool, project_id: str
) -> None:
    """RAVEL does not resume sessions; it rebuilds them from the record."""
    runtime, binding = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    runtime.run_turn(binding.session_id, "Reply with the single word: READY")
    pool.close_scope(project_id, AgentRole.MASTER)

    # Recovery: a fresh runtime, a fresh session, and the authoritative brief.
    recovered, recovered_binding = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    assert recovered is not runtime
    assert recovered_binding.session_id != binding.session_id

    outcome = recovered.run_turn(
        recovered_binding.session_id,
        "What is this project's title and current phase? Read them from your tools; do not "
        "guess and do not rely on any earlier conversation.",
    )

    assert outcome.completed
    assert _called(outcome, "read_project_state"), (
        "recovery did not read authoritative state: " + ", ".join(_tool_names(outcome))
    )
    assert TITLE_SENTINEL in outcome.response
    # The brief is on disk where an operator can read what the runtime launched with.
    brief = json.loads((recovered.work_dir / "brief.json").read_text(encoding="utf-8"))
    assert brief["project_id"] == project_id
    assert brief["role"] == "master"
    assert brief["project_title"] == TITLE_SENTINEL


def test_every_runtime_boots_from_a_reviewable_overlay(
    pool: DshRuntimePool, project_id: str
) -> None:
    """The authority a runtime launched with is a file, not a constructor argument."""
    runtime, _binding = pool.start_session(project_id, AgentRole.MASTER, brief=BRIEF)
    overlay = runtime.work_dir / "ravel-role.cordis.patch.yml"
    assert overlay.is_file()
    text = overlay.read_text(encoding="utf-8")
    assert f"RAVEL_PROJECT_ID: {project_id}" in text
    assert "RAVEL_ROLE: master" in text
    assert "!!js" not in text
    assert not (Path(runtime.work_dir) / ".env").exists(), (
        "the harness reads <cwd>/.env as a credential fallback, so a runtime's "
        "working directory must never hold one"
    )
