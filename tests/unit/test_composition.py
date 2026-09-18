"""The generated overlay is the runtime's whole authority, so it is asserted.

Every value is written literally because the pinned harness does not evaluate
`!!js` expressions in `--patch` overlays. That is why these tests parse the
written YAML rather than inspecting a structure: the file on disk is what the
harness reads, and it is also the audit record of what a runtime was launched
with.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from ravel.dsh.composition import (
    BASE_PROFILE,
    MCP_SERVER_NAME,
    RoleComposition,
    composition_dump,
)
from ravel.dsh.roles import AgentRole, definition_for
from ravel.mcp.registry import DAG_MUTATION_TOOLS, tools_for


@pytest.fixture
def composition(tmp_path: Path) -> RoleComposition:
    return RoleComposition.for_scope(
        project_id="proj-a",
        role=AgentRole.RESEARCH,
        mcp_command="/usr/bin/python3",
        brief_path=tmp_path / "brief.json",
    )


def _inserted_rows(composition: RoleComposition) -> list[dict[str, Any]]:
    """The rows the overlay inserts.

    `as_patch()` is typed as a list of rows holding `object` values, which is
    honest — the rows are heterogeneous. Reading into them is what needs the
    wider type, and widening it in one place keeps the assertions readable.
    """
    patch: list[dict[str, Any]] = composition.as_patch()
    return patch[-1]["insert"]


def _row(patch: list[dict[str, Any]], row_id: str) -> dict[str, Any]:
    for row in patch:
        if row.get("id") == row_id:
            return row
    raise AssertionError(f"overlay has no row {row_id!r}")


def test_overlay_replaces_the_persona_with_the_role_contract(composition: RoleComposition) -> None:
    row = _row(composition.as_patch(), "system-prompt")
    assert row["config"]["personaPrefix"] == definition_for(AgentRole.RESEARCH).persona
    # The agent must not be told it is inside a harness, nor handed a snapshot
    # of runtime context; orientation comes from state it reads deliberately.
    assert row["config"]["includeHarnessIdentity"] is False
    assert row["config"]["includeRuntimeContext"] is False


@pytest.mark.parametrize("row_id", ["persistent-bash", "persistent-pwsh"])
def test_overlay_removes_the_shell(composition: RoleComposition, row_id: str) -> None:
    """An agent that can run a shell can act outside its authorized tools."""
    assert _row(composition.as_patch(), row_id)["disabled"] is True


def test_overlay_mounts_exactly_one_tool_server(composition: RoleComposition) -> None:
    inserts = [row for row in composition.as_patch() if "insert" in row]
    assert len(inserts) == 1
    rows = inserts[0]["insert"]
    assert isinstance(rows, list) and len(rows) == 1
    assert rows[0]["name"] == "@deepseek-ai/dsh-mcp-client"


def test_tool_server_row_carries_the_scope_in_its_environment(composition: RoleComposition) -> None:
    """This is the whole reason a tool handler can trust its project id."""
    row = _inserted_rows(composition)[0]
    config = row["config"]
    assert config["serverName"] == MCP_SERVER_NAME
    assert config["transport"] == "stdio"
    assert config["command"] == "/usr/bin/python3"
    assert config["args"] == ["-m", "ravel.mcp.server"]
    assert config["failOnStartupError"] is True
    assert config["env"] == {
        "RAVEL_PROJECT_ID": "proj-a",
        "RAVEL_ROLE": "research",
        "RAVEL_BRIEF_FILE": str(composition.brief_path),
    }


@pytest.mark.parametrize("role", list(AgentRole))
def test_every_role_composes(tmp_path: Path, role: AgentRole) -> None:
    composed = RoleComposition.for_scope(
        project_id="proj-a",
        role=role,
        mcp_command="/usr/bin/python3",
        brief_path=tmp_path / "b.json",
    )
    row = _inserted_rows(composed)[0]
    assert row["config"]["env"]["RAVEL_ROLE"] == role.value


def test_written_overlay_is_parseable_yaml(composition: RoleComposition, tmp_path: Path) -> None:
    path = composition.write(tmp_path / "nested" / "role.cordis.patch.yml")
    assert path.is_file()
    parsed = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert parsed == composition.as_patch()


def test_written_overlay_contains_no_js_tags(composition: RoleComposition, tmp_path: Path) -> None:
    """`!!js` is not evaluated in `--patch` overlays; the harness would refuse."""
    text = composition.write(tmp_path / "role.cordis.patch.yml").read_text(encoding="utf-8")
    assert "!!js" not in text
    assert "tag:yaml.org,2002:js" not in text


def test_persona_is_written_as_a_reviewable_block(
    composition: RoleComposition, tmp_path: Path
) -> None:
    """A one-line escaped prompt is unreviewable; the block form is the point."""
    text = composition.write(tmp_path / "role.cordis.patch.yml").read_text(encoding="utf-8")
    assert "personaPrefix: |" in text
    # The persona's own newlines survive verbatim, unescaped.
    assert "\n" in definition_for(AgentRole.RESEARCH).persona
    assert "\\n" not in text.split("personaPrefix: |")[1].split("- id:")[0]


def test_rewriting_the_overlay_replaces_it(composition: RoleComposition, tmp_path: Path) -> None:
    path = tmp_path / "role.cordis.patch.yml"
    path.write_text("stale: true\n", encoding="utf-8")
    composition.write(path)
    assert yaml.safe_load(path.read_text(encoding="utf-8")) == composition.as_patch()


def test_base_profile_is_the_shipped_minimal_tree() -> None:
    assert BASE_PROFILE == "sdk-minimal"


# ── The dump ────────────────────────────────────────────────────────────────


def _dump(tmp_path: Path) -> dict[str, dict[str, Any]]:
    entries = composition_dump(
        project_id="proj-a",
        mcp_command="/usr/bin/python3",
        brief_dir=tmp_path / "briefs",
    )
    return {str(entry["role"]): entry for entry in entries}


def test_the_dump_covers_every_role_exactly_once(tmp_path: Path) -> None:
    assert set(_dump(tmp_path)) == {role.value for role in AgentRole}


@pytest.mark.parametrize("role", list(AgentRole))
def test_the_dump_reports_the_roster_from_the_registry(tmp_path: Path, role: AgentRole) -> None:
    """The dump is a view of the roster table, not a second copy of it."""
    assert _dump(tmp_path)[role.value]["tools"] == list(tools_for(role))


def test_the_dump_names_the_dag_mutation_tools_and_only_master_has_them(tmp_path: Path) -> None:
    for role_value, entry in _dump(tmp_path).items():
        if role_value == AgentRole.MASTER.value:
            assert set(entry["dag_mutation_tools"]) == set(DAG_MUTATION_TOOLS)
        else:
            assert entry["dag_mutation_tools"] == []


def test_the_dump_carries_the_scope_the_runtime_would_be_launched_with(tmp_path: Path) -> None:
    entry = _dump(tmp_path)[AgentRole.MASTER.value]
    assert entry["project_id"] == "proj-a"
    assert entry["mcp_command"] == "/usr/bin/python3"
    assert entry["mcp_module"] == "ravel.mcp.server"
    # The persona is measured rather than reproduced: the dump says a contract
    # was loaded and how large it is, and the overlay is where it is read.
    assert entry["persona_chars"] == len(definition_for(AgentRole.MASTER).persona)
    assert entry["persona_chars"] > 0
