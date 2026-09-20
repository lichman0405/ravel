# Phase 10 Baseline

Recorded on branch `phase10/true-autonomy` before any Phase 10 changes.

## Environment

- OS: Ubuntu 24.04 LTS x86_64
- Python: 3.12
- DSH pin: `dsh-v0.1.5-rc.1` / `deepseek-harness-sdk==0.1.5rc1` / `deepseek-harness-runtime-bin==0.1.5rc1`
- Credentials: `DEEPSEEK_API_KEY` and `RAVEL_RESEARCH_CONTACT_EMAIL` present in `.env`

## Test Results

| Suite | Result |
|---|---|
| `make lint` | ruff passed, pyright 0 errors |
| `make test-unit` | 752 passed, 0 failed |
| `make test-integration` | 447 passed, 90 deselected, 0 failed |
| `make test-e2e` | 20 passed, 0 failed |
| `make test-live` | 13 passed, 0 failed |
| `make test-dsh` | 11 passed, 0 failed |
| `make acceptance` | 27/27 demonstrated, 0 failed, 0 skipped, 0 missing |

## Current Architecture Facts

- Master and Review are already driven by real DSH agents (`HarnessAgent`).
- Compute / Experimental Worker roles exist in the role roster and MCP registry, but in the execution loop the actual computation/experiment execution is handled by deterministic Temporal activities calling `MockComputeBackend` / `MockLabBackend`.
- There is no `ProjectSupervisor`: projects are driven manually by `scripts/run_project.py`.
- DSH runtime is currently one process per `(project_id, role)` pair, because the pinned SDK cannot select per-session agent presets or identify MCP caller sessions.

## Phase 10 Starting Gaps

1. **Latest DSH not re-evaluated** — still pinned at v0.1.5-rc.1; need to spike current release for single-host multi-session capabilities.
2. **Compute Worker Agent not in execution loop** — no live DSH agent turn for compute tasks.
3. **Experimental Worker Agent not in execution loop** — no live DSH agent turn for lab tasks.
4. **No Project Supervisor** — manual `run_project.py` required.
5. **No long-autonomy certification** — a full live five-agent project has not been run end to end.

## Next Step

Phase 10A — Latest DSH Capability Spike.
