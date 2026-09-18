# 07 — DeepSeek Harness Integration

## 1. Only Harness

RAVEL V0 and intended product support **DeepSeek Harness only**.

Do not create runtime adapters for Claude/Codex/Grok.

An internal `DshRuntimeClient` boundary is still useful to prevent DSH API details leaking into domain logic.

## 2. Pin current real DSH at implementation start

DSH is Developer Preview and may break compatibility.

Implementation must:
- fetch official upstream
- inspect current docs/API
- pin release/tag/commit
- record in `vendor/DSH_PIN.json`
- never silently track master

## 3. Current verified facts as of 2026-09-17

See `references/DSH_CURRENT_STATE_2026-09-17.md`.

Do not treat these as eternal API truth.

Key observed capabilities:
- official Python SDK drives DSH over JSON-RPC/stdio
- explicit DSH_HOME
- persistent session logs available with persistence provider/profile
- per-session agent presets exist
- presets can contribute tools/persona/prompt/compaction
- host plane owns shared registries/persistence/model route/sandbox
- built-in real web search/fetch capability exists with selectable providers
- DSH remains pre-stable

Known risk observed in late Aug 2026:
- Python SDK restart/resume path had a reported persisted-session ID collision issue
- implementation must test current behavior before relying on cross-process session continuation

## 4. RAVEL DSH topology

One Host, many sessions.

Agent presets:
- `ravel-master`
- `ravel-research`
- `ravel-review`
- `ravel-compute-worker`
- `ravel-experimental-worker`

Do not use the shipped Standard Coding Agent persona as the scientific role.

## 5. Session binding

Server-side immutable binding:

```text
session_id
project_id
agent_role
task_id?
preset
workspace
created_at
status
```

The model must not be trusted to choose its project scope.

## 6. Tool exposure

Each role sees only tools it needs.

### Master
- project state query
- DAG mutation commands
- decision creation
- authority checks
- dispatch commands
- request research/review/execution
- approvals/context retrieval

### Research
- Research Source Gateway tools
- evidence registration
- artifact/reference query
- research record submit

No DAG mutation tool.

### Review
- read task/criteria/artifacts/execution
- submit review

No DAG mutation tool.

### Compute Worker
- compute backend tools
- artifact tools
- execution record tools
- deviation/escalation

### Experimental Worker
- experiment backend tools
- artifact upload/receive metadata
- deviation/escalation

## 7. Filesystem/sandbox

Every Project has separate workspace root.
Every session receives only allowed workspace and tool scope.

Do not rely on system prompt for filesystem security.

## 8. Master persistence

Normal:
- persistent Master session remains live

Checkpoint:
- structured checkpoint written periodically and on important state transitions

Recovery:
1. read Project authoritative state
2. read latest checkpoint
3. inspect current DSH persistence capability
4. resume session if current pinned DSH safely supports it
5. otherwise create replacement Master session with same Master identity and reconstruct working context

Never lose Project because a DSH session dies.

## 9. DSH core modifications

Priority:
1. official plugin
2. local profile/preset
3. official Python SDK / JSON-RPC
4. minimal external wrapper
5. only then tiny upstream patch

Any patch must be isolated and documented.
