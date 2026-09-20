# Phase 10A — Latest DeepSeek Harness Capability Spike

**Date:** 2026-09-20  
**Branch:** `phase10/true-autonomy`  
**Spike goal:** Re-evaluate whether the current public DeepSeek Harness (DSH) release line supports the single-host, multi-project, per-session role-isolation design described in `docs/07_DSH_INTEGRATION.md`.

## 1. What was inspected

| Artifact | Version / identity | Source |
|---|---|---|
| Installed PyPI pin | `deepseek-harness-sdk==0.1.5rc1` + matched runtime-bin | `pyproject.toml`, `vendor/DSH_PIN.json` |
| Latest public Git tag | `dsh-v0.1.6-alpha.2` | `git clone https://github.com/deepseek-ai/deepseek-harness.git` |
| Latest public PyPI release | still `0.1.5rc1` (no `0.1.6` or `0.1.5-rc.2` wheels) | `pip index versions deepseek-harness-sdk` / PyPI JSON API |
| Source tree checked out | `dsh-v0.1.6-alpha.2` (commit unspecified; tag is the only public newer ref) | `/tmp/dsh-spike` |

The inspection focused on three properties RAVEL needs for single-host multi-session role isolation:

1. **Per-session Agent Preset selection** — can the Python SDK create a session that joins a preset such as `ravel-compute-worker`?
2. **Trusted MCP caller identity** — does the wire from the harness to an MCP server carry the session/project id so a shared MCP server can authorize the call?
3. **Python SDK surface** — are new methods exposed that would let RAVEL drive the above?

## 2. Findings

### 2.1 Python SDK surface is unchanged

`python/sdk/src/deepseek_harness/client.py` in `dsh-v0.1.6-alpha.2` exposes the same three JSON-RPC methods as the pinned `0.1.5-rc.1` release:

- `initialize`
- `session/prompt`
- `shutdown`

There is no `session/open`, no `session/resume`, no preset argument, and no session-scoped context on the wire.

### 2.2 SDK server still composes sessions without joining a preset

`packages/sdk/server/src/server.ts:274-292`:

```ts
private async createSession(sessionId: string): Promise<SessionRecord> {
  // No preset composition: this server's compositions keep the model-facing
  // rows in the host plane, so this agent reads them from the global layer. A
  // deployment that configures a roster has to join one here first
  // (@deepseek-ai/dsh-agent-presets README, "Composing a child agent").
  const handle = await this.ctx.agents.create({
    sessionId: brandString<SessionId>(sessionId),
    meta: { cwd: this.cwd },
    agentOptions: {
      provider: this.provider,
      model: this.model,
      ...
    },
  })
  ...
}
```

The server itself documents the gap: a deployment that wants per-session presets must call `AgentPresets.mount()` inside `createSession`, and the SDK server does not.

### 2.3 Preset package exists, but is not reachable from the SDK path

`packages/preset/agent-presets/README.md` states:

> "One process can run sessions with different presets while keeping their state separate."

The package defines `agentPresetProjectionDefinition` (`packages/preset/agent-presets/src/session.ts`) and a session header field `agentPreset`. However:

- The preset is selected at session creation or switched while the session is blank.
- The Python SDK has no parameter to pass a preset name to `session/prompt`.
- The SDK server's `createSession` does not read a preset from the session header or from any SDK parameter.

So the capability exists inside the host-layer Cordis runtime, but it is not exposed to an out-of-process Python SDK caller such as RAVEL.

### 2.4 MCP tool calls still carry no session identity

`packages/mcp/mcp-client/src/tools.ts:138-141`:

```ts
call: (args, execution) => client.callTool(
  { name: tool.name, arguments: args },
  { signal: execution.signal, timeout: opts.toolCallTimeoutMs, toolDefinition: tool },
)
```

The JSON-RPC `tools/call` request contains only `name` and `arguments`. No `sessionId`, no `projectId`, no role header. A single shared MCP server would therefore have to trust whatever project/role the model supplied in its arguments, which violates `docs/09_SECURITY_AND_IDENTITY.md` and `CLAUDE.md` ("project-scoped authorization > model-supplied project_id").

### 2.5 No release assets for the newer tag

The `dsh-v0.1.6-alpha.2` GitHub tag has no wheels and no source distribution attached. The source tree's `python/sdk/pyproject.toml` reports `version = "0.0.0.dev0"`, indicating it is not a releasable package at that tag. The only installable DSH artifacts remain the `0.1.5rc1` wheels from PyPI.

## 3. Conclusion

The newer DSH tag does not remove any of the blockers documented in `docs/IMPLEMENTATION_DEVIATIONS.md` D-001:

- Per-session Agent Preset selection is still unreachable through the Python SDK.
- MCP tool calls still carry no trusted caller identity.
- The Python SDK surface is unchanged.

Therefore **RAVEL cannot consolidate onto one DSH Host per CVM at this public release line**. The existing per-`(project_id, role)` runtime design remains the only implementation that enforces role separation structurally rather than trusting a model-supplied scope.

## 4. Follow-up

- Record Decision B in `SINGLE_DSH_BLOCKER_REPORT.md`.
- Keep the pin at `dsh-v0.1.5-rc.1` / `0.1.5rc1`.
- Extend D-001's follow-up note with the `dsh-v0.1.6-alpha.2` finding.
- Proceed to Phase 10C: wire real Compute Worker / Experimental Worker Agents using the existing per-role multi-runtime design, with Mock backends.
