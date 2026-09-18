# DSH Patches

Pinned harness: `dsh-v0.1.5-rc.1` (`183f08e9c6dde7e36cd2318eaee70b0da08fb35e`).

## Status

**No patch to DeepSeek Harness source is required for RAVEL V0.**

RAVEL integrates with the pinned release strictly through shipped extension
mechanisms, in the priority order required by `docs/07_DSH_INTEGRATION.md`
section 9:

1. **Official plugin / bundle** — not needed.
2. **Local profile / preset** — used. One DSH profile per agent role, created
   from the shipped `sdk` template and layered with RAVEL patch files.
3. **Official Python SDK / JSON-RPC** — used. All agent turns are driven through
   `deepseek-harness-sdk` over newline-delimited JSON-RPC on stdio.
4. **Minimal external wrapper** — used. `ravel.dsh.client` is a thin typed
   boundary around the SDK so DSH API details never leak into domain logic.
5. **Upstream patch** — **not used**.

## Constraints discovered at this pin

These are properties of the pinned release that shaped the integration. They are
recorded here so a future re-pin can check whether they still hold.

### The SDK wire protocol is deliberately narrow

`packages/sdk/protocol/src/types.ts` declares exactly three client-to-server
requests: `initialize`, `session/prompt`, `shutdown`. There is no tool
registration surface in either direction, and `packages/sdk/protocol/README.md`
states that server-to-client requests are a dead capability at this release.
An external process therefore cannot host tools for an agent over the SDK
channel.

Consequence: RAVEL exposes its tools as MCP servers over stdio. The
`@deepseek-ai/dsh-mcp-client` row is configuration-only, so no TypeScript is
needed and all tool logic stays in Python.

### The SDK server does not join an agent preset

`packages/sdk/server/src/server.ts` `createSession()` composes each session from
the host-plane rows and carries the comment that a deployment configuring a
roster "has to join one here first". `AgentPresets.mount()` and
`AgentPresets.composeFrom()` run in an agent factory's setup window, which the
SDK server does not supply.

Consequence: per-session preset selection is unreachable through the SDK at this
pin. RAVEL enforces tool scope per role by composing one profile per role
instead, which makes the isolation process-level and strictly stronger. See
`docs/IMPLEMENTATION_DEVIATIONS.md`.

### The SDK server has no session resume

The server creates sessions implicitly through `ctx.agents.create()`. A second
runtime process given a persisted session id fails with
`session "<id>" already exists`. `AgentFactory.resume` exists in
`packages/core/agent` but is not reachable over the SDK wire protocol.

Consequence: RAVEL never depends on DSH session continuation for correctness.
Recovery reads PostgreSQL Project State plus the latest `MasterCheckpoint` and
builds a replacement session under the same Master identity, as
`docs/07_DSH_INTEGRATION.md` section 8 prescribes.

## If a patch ever becomes necessary

Any future patch must be minimal, isolated in `vendor/`, applied by an explicit
scripted step, and documented here with: the upstream file, the diff, the reason
no shipped mechanism suffices, and the removal condition once upstream provides
the capability.
