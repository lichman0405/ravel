# DeepSeek Harness — Verified Context as of 2026-09-17

This file is **not** a frozen dependency lock. Implementation must re-check upstream and pin the real version at development start.

## Upstream

Repository:
https://github.com/deepseek-ai/deepseek-harness

Observed on 2026-09-17:
- project is explicitly marked Developer Preview
- compatibility-breaking changes are expected
- release list includes `v0.1.5-rc.2`
- MIT licensed

## Python SDK

Official docs:
https://github.com/deepseek-ai/deepseek-harness/blob/master/docs/user/guide/python-sdk.md

Observed:
- Python 3.10+
- `deepseek-harness-sdk`
- bundled matching runtime wheel
- DSH launched and driven through JSON-RPC/stdio
- explicit `dsh_home` / workspace
- profile owns composition/persistence/tools
- SDK minimal profile does not automatically include full Web/compaction/etc.

## Agent presets

References:
https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/preset/README.md
https://github.com/deepseek-ai/deepseek-harness/blob/master/apps/cli/config/agent-presets/cordis/skills/editing-cordis-compositions/SKILL.md

Observed:
- per-session agent composition
- preset can contribute tools, persona/prompt sections, compaction policy
- host plane retains cross-session registries, persistence, sandbox, model route
- suitable for RAVEL's five scientific roles

## Session persistence

Reference:
https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/session/session-persistence/README.md

Observed:
- persistence contract supports append-only durable session event logs
- backend-independent persistence API
- JSONL backend exists

Important reported issue (Aug 2026):
https://github.com/deepseek-ai/deepseek-harness/discussions/4954

A reported Python SDK restart path could collide when using persisted session ID rather than a true resume/open path. At RAVEL implementation start:
- reproduce current behavior
- inspect whether resolved
- do not claim session-resume support without integration test
- rely on RAVEL authoritative state/checkpoint for guaranteed recovery

## Web capability

References:
https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/README.md
https://github.com/deepseek-ai/deepseek-harness/blob/master/packages/web/tool-web/README.md

Observed:
- DSH has model-facing real `web_search` / `web_fetch`
- providers include selectable search backends
- fetch is not interactive browser automation

RAVEL requirement is stricter:
- Research Source Gateway must preserve provenance
- search result is lead only
- original source verification required
- browser-level research also required
- therefore do not expose a provenance-bypassing web tool path to Research Agent
