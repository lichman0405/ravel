# 15 — Autonomous Development Contract

This file defines how Claude Code / Codex should execute the repository from zero to V0 completion.

## Expected behavior

The coding harness should proceed autonomously through:
- repository initialization
- Ubuntu bootstrap
- real current DSH pinning
- DSH integration spike
- domain/state implementation
- Scientific DAG
- real Research Source Gateway
- Temporal
- Mock Compute/Lab
- Review/Master closed loop
- Research Gateway
- Textual TUI
- resilience tests
- all V0 acceptance tests
- final handoff documentation

It should continuously implement, test, fix, document and continue.

## Do not re-ask frozen decisions

Do not ask the user again for:
- OS
- language
- Harness
- DB
- Temporal
- object-store pattern
- Agent roles
- whether Web research may be mocked
- whether users can edit DAG
- whether POST is required
- TUI framework

Those decisions are already frozen.

## Legitimate blockers

Only request user input when truly blocked by information that cannot be derived or mocked under the V0 spec, such as:
- a private credential with no legitimate credential-free real alternative
- access to a private external HPC/Lab system when explicitly requested beyond V0
- external domain/certificate ownership
- an unresolved legal/licensing decision

If a V0 backend is explicitly mockable, use the Mock Backend instead of blocking.

## No false completion

Do not declare completion if:
- Web research was mocked
- DSH was not actually pinned and tested
- DSH role presets/tool scoping were not verified
- Master recovery was not tested
- Temporal restart/wait recovery was not tested
- Acceptance freeze was not tested
- unauthorized DAG mutation was not tested
- TUI is only static
- any of the 20 V0 acceptance items were skipped

## Stop condition

Stop only when:
1. all V0 acceptance items pass; or
2. a real external blocker is documented precisely and every non-blocked requirement is complete.

## Final handoff documents

Produce:
- `IMPLEMENTATION_REPORT.md`
- `TEST_REPORT.md`
- `KNOWN_LIMITATIONS.md`
- `DSH_INTEGRATION_REPORT.md`
- `SECURITY_NOTES.md`
- `DEPLOYMENT.md`

Include exact Ubuntu commands to bootstrap, start, stop and test RAVEL V0.
