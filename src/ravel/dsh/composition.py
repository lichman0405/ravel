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

from dataclasses import dataclass
from pathlib import Path

import yaml

from ravel.dsh.roles import AgentRole, definition_for

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

    @classmethod
    def for_scope(
        cls,
        project_id: str,
        role: AgentRole,
        mcp_command: str,
        brief_path: Path,
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
        )

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
                            "env": {
                                "RAVEL_PROJECT_ID": self.project_id,
                                "RAVEL_ROLE": self.role.value,
                                "RAVEL_BRIEF_FILE": str(self.brief_path),
                            },
                        },
                    }
                ]
            },
        ]

    def write(self, path: Path) -> Path:
        """Write the overlay, replacing any previous one for this scope."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            yaml.dump(
                self.as_patch(),
                Dumper=_Dumper,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
                width=100,
            ),
            encoding="utf-8",
        )
        return path
