"""The harness composition a role runtime boots with.

RAVEL does not patch the harness and does not keep a profile tree on disk. Each
runtime is launched with a generated overlay applied over the shipped
`sdk-minimal` profile, containing exactly three decisions:

1. the role's behavioral contract becomes the system prompt,
2. the shell is removed from the model's view, and
3. one MCP server is mounted, serving only that role's tools.

The overlay is generated rather than templated because the harness does not
evaluate `!!js` expressions in `--patch` overlays — only in bundle layers. Every
value is therefore written literally, which has the useful side effect that the
authority a runtime was launched with is a file on disk that can be read and
diffed after the fact.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from ravel.dsh.roles import ROLE_DEFINITIONS, AgentRole, definition_for
from ravel.mcp.registry import mutating_tools_for, tools_for

#: The shipped profile the overlay is applied to. It is a standalone tree: it
#: owns every row it mounts and carries no filesystem, web, subagent, or skill
#: tools, so a role's granted roster is exactly what this overlay adds.
BASE_PROFILE = "sdk-minimal"

#: The MCP server name. It becomes the model-facing tool prefix
#: `mcp__ravel__<tool>`, so it must be stable: session history and permission
#: rules are keyed on the qualified name.
MCP_SERVER_NAME = "ravel"


def _literal_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    """Emit multi-line strings as literal blocks.

    Prompts and paths are far easier to review as blocks than as one long
    quoted line with escaped newlines.
    """
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(str, _literal_str)


@dataclass(frozen=True, slots=True)
class RoleComposition:
    """The overlay one `(project, role)` runtime is launched with."""

    project_id: str
    role: AgentRole
    persona: str
    mcp_command: str
    mcp_module: str
    brief_path: Path
    #: What the tool server must be told about the state it serves, from the
    #: settings the launching process holds — see `Settings.tool_server_env`.
    #: Required rather than defaulted: a composition that forgot it would launch
    #: a server reading whatever `.env` says, and a session acting on a database
    #: its supervisor does not share is the one failure this field exists to
    #: make impossible.
    tool_server_env: Mapping[str, str]

    @classmethod
    def for_scope(
        cls,
        project_id: str,
        role: AgentRole,
        mcp_command: str,
        brief_path: Path,
        *,
        tool_server_env: Mapping[str, str],
    ) -> RoleComposition:
        """Build the composition for one scope from the role's contract."""
        definition = definition_for(role)
        return cls(
            project_id=project_id,
            role=role,
            persona=definition.persona,
            mcp_command=mcp_command,
            mcp_module=definition.mcp_module,
            brief_path=brief_path,
            tool_server_env=tool_server_env,
        )

    def tool_server_environment(self) -> dict[str, str]:
        """The complete environment the tool server is launched with.

        The scope and the state coordinates together, because they are one
        grant: which project this process may act on is meaningless without
        which database holds it. The scope keys come last, so a settings
        mapping can never overwrite the authority the composition states.
        """
        return {
            **self.tool_server_env,
            "RAVEL_PROJECT_ID": self.project_id,
            "RAVEL_ROLE": self.role.value,
            "RAVEL_BRIEF_FILE": str(self.brief_path),
        }

    @property
    def tools(self) -> tuple[str, ...]:
        """The tools this role's runtime will actually be able to reach.

        Read from the registry rather than stored on the composition, so that a
        dump cannot describe a roster the launched server would not register.
        There is one roster table, and this is a view of it.
        """
        return tools_for(self.role)

    def as_dump(self) -> dict[str, Any]:
        """A reviewable summary of one role's runtime.

        The overlay is the authority; this is the part of it a human or a test
        wants to check without reading YAML — chiefly, which tools the role
        holds and whether any of them can change the DAG.
        """
        return {
            "project_id": self.project_id,
            "role": self.role.value,
            "role_name": self.role.display_name,
            "mcp_command": self.mcp_command,
            "mcp_module": self.mcp_module,
            "tools": list(self.tools),
            "dag_mutation_tools": list(mutating_tools_for(self.role)),
            "persona_chars": len(self.persona),
            "brief_path": str(self.brief_path),
        }

    def as_patch(self) -> list[dict[str, object]]:
        """The overlay as the harness's patch list."""
        return [
            # The role's contract replaces the deployment persona. The harness
            # identity line and runtime-context snapshot stay off: the agent
            # should not be told it is running inside a harness, and its
            # orientation comes from the project state it reads deliberately.
            {
                "id": "system-prompt",
                "config": {
                    "includeHarnessIdentity": False,
                    "includeRuntimeContext": False,
                    "personaPrefix": self.persona,
                },
            },
            # Remove the shell from the model's view. RAVEL agents act only
            # through the tools below, so every action is authorized against
            # this process's project scope and recorded. The terminal rows stay
            # mounted but unreachable; disabling the tools that expose them is
            # the smaller and reversible change.
            {"id": "persistent-bash", "disabled": True},
            {"id": "persistent-pwsh", "disabled": True},
            # The role's tool server. `failOnStartupError` is on deliberately:
            # a server that failed to start would otherwise leave the agent
            # with no tools and no error.
            {
                "insert": [
                    {
                        "id": "ravel-tools",
                        "name": "@deepseek-ai/dsh-mcp-client",
                        "config": {
                            "serverName": MCP_SERVER_NAME,
                            "transport": "stdio",
                            "command": self.mcp_command,
                            "args": ["-m", self.mcp_module],
                            "failOnStartupError": True,
                            "env": self.tool_server_environment(),
                        },
                    }
                ]
            },
        ]

    def write(self, path: Path) -> Path:
        """Write the overlay, replacing any previous one for this scope.

        The file is written owner-readable only, because the environment it
        carries is a credential: the harness spawns the tool server with it, and
        the server connects to PostgreSQL. It is the one place on disk a
        deployment's database password appears, and it sits in a directory the
        harness reads rather than in a secrets store, so its mode is set here
        rather than left to the umask.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        rendered = yaml.dump(
            self.as_patch(),
            Dumper=_Dumper,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
            width=100,
        )
        # Opened with the mode rather than written and then tightened: a
        # create-then-chmod leaves the password readable by everyone between
        # the two calls, and leaves it that way for good if the process dies in
        # between. `O_CREAT` with an existing file keeps that file's mode, so
        # the chmod below is still needed — it is what tightens an overlay that
        # a previous version of this code left readable.
        handle = os.fdopen(
            os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
            "w",
            encoding="utf-8",
        )
        with handle:
            handle.write(rendered)
        path.chmod(0o600)
        return path


def composition_dump(
    project_id: str,
    mcp_command: str,
    brief_dir: Path,
    *,
    roles: Iterable[AgentRole] = tuple(ROLE_DEFINITIONS),
) -> list[dict[str, Any]]:
    """Which authority each role would be launched with, as one document.

    This is the profile table a reviewer reads before agreeing that a role has
    the authority it claims — in particular that exactly one role holds tools
    that change the DAG. It describes what *would* be launched; the integration
    suite launches each role's server instead and asserts this dump against
    what the server actually registered, so the dump cannot quietly drift from
    the runtime.

    The question it answers is about the *roster*, so the state coordinates are
    not an argument to it and not part of the answer: they are the same for
    every role, they name the database the sessions read, and the mapping that
    carries them holds its password. A composition built here is a means of
    reading a role's contract, and `as_dump` never reports the environment —
    the written overlay is where the authority a runtime was launched with is
    read.
    """
    return [
        RoleComposition.for_scope(
            project_id=project_id,
            role=role,
            mcp_command=mcp_command,
            brief_path=brief_dir / f"{role.value}.brief.json",
            tool_server_env={},
        ).as_dump()
        for role in roles
    ]
