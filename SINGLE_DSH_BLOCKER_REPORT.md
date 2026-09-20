# Decision B — Single DSH Host Blocker Report

**Date:** 2026-09-20  
**Branch:** `phase10/true-autonomy`  
**Decision:** Retain the per-`(project_id, role)` DSH runtime design. Do **not** migrate to a single shared DSH Host in Phase 10.

## 1. Background

`docs/07_DSH_INTEGRATION.md` and `CLAUDE.md` describe RAVEL V0's target topology as:

> One DSH Host on a single CVM, carrying multiple Projects/sessions, with per-session Agent Presets selecting the role (`ravel-master`, `ravel-research`, `ravel-review`, `ravel-compute-worker`, `ravel-experimental-worker`).

The implementation instead runs one DSH runtime process per `(project_id, role)` pair, documented as `docs/IMPLEMENTATION_DEVIATIONS.md` D-001. The deviation was always framed as a follow-up item to re-evaluate at the next DSH re-pin. Phase 10A performed that re-evaluation.

## 2. Re-evaluation method

- Inspected the installed PyPI pin (`0.1.5rc1`) and the newer public Git tag `dsh-v0.1.6-alpha.2`.
- Checked PyPI for any newer wheel release.
- Read the SDK server, the agent-presets package, the MCP client bridge, and the Python SDK client in the newer tag.

Full details are in `PHASE10_DSH_SPIKE_REPORT.md`.

## 3. Blockers

### Blocker B1 — No per-session preset selection in the Python SDK

`packages/sdk/server/src/server.ts:274-292` creates each session without joining an Agent Preset. The Python SDK exposes only `initialize`, `session/prompt`, and `shutdown`; there is no parameter to name a preset.

Without this, a single DSH Host cannot switch a session between `ravel-master`, `ravel-compute-worker`, etc. The host plane supports presets internally, but the out-of-process SDK path that RAVEL uses does not expose them.

### Blocker B2 — No trusted MCP caller identity

`packages/mcp/mcp-client/src/tools.ts:138-141` forwards tool calls as `{ name, arguments }`. No `sessionId`, `projectId`, or role header reaches the MCP server.

A shared MCP server would have to rely on a model-supplied `project_id` argument, which `CLAUDE.md` and `docs/09_SECURITY_AND_IDENTITY.md` forbid:

> project-scoped authorization > model-supplied project_id

### Blocker B3 — No newer installable artifact

`dsh-v0.1.6-alpha.2` has no release assets or PyPI wheels. The Python SDK source at that tag reports `version = "0.0.0.dev0"`. The only installable, versioned artifacts remain `0.1.5rc1`.

## 4. Options considered

| Option | Feasibility | Risk | Verdict |
|---|---|---|---|
| A. Migrate to single DSH Host now | Blocked by B1–B3 | Would require trusting model-supplied scope or patching DSH core | **Rejected** |
| B. Keep per-`(project, role)` runtimes | Works today | More processes than single-host; mitigated by lazy start + idle reaping | **Accepted** |
| C. Patch DSH core to expose presets / MCP identity | Possible in source, but violates "no core modification" priority and creates a fork | Maintenance burden, breaks at next re-pin | **Rejected** |
| D. Build DSH from `alpha.2` source | Source is dev version, no runtime binary assets; build not reproducible from released artifacts | Unsupported deployment path | **Rejected** |

## 5. Decision

**Adopt Option B.**

RAVEL V0 Phase 10 will:

- Keep the DSH pin at `dsh-v0.1.5-rc.1` / `0.1.5rc1`.
- Keep one runtime process per `(project_id, role)`.
- Implement true five-agent architecture on top of that design, including live `ComputeWorkerAgent` and `ExperimentalWorkerAgent` sessions.
- Keep `MockComputeBackend` and `MockLabBackend` as the V0 execution backends.

## 6. Consequences

- `docs/IMPLEMENTATION_DEVIATIONS.md` D-001 remains in force and is updated with the `alpha.2` finding.
- The single-host target is deferred to a future DSH re-pin that exposes preset selection and trusted MCP caller identity through the Python SDK.
- Phase 10C–E will use the existing `DshRuntimePool` and `RoleRuntime` machinery; no new agent type or harness adapter is introduced.

## 7. Follow-up

- Re-evaluate single-host feasibility whenever a new DSH release provides:
  - a Python SDK method to create/join a session under a named Agent Preset, **or**
  - MCP tool calls that carry a trusted session/project identity, **or**
  - an officially supported Python plugin seam for same-process tools.
- Until then, do not fork DSH core or build from unreleased source.
