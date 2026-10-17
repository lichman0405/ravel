# Phase 11 Adoption Report (T02)

## Source and merge

- Base integration SHA: `b79fd5287c42ced28d80137e5f3f16e5c9487998` (T00+T01 integrated)
- Imported official upstream candidate: `c9815f13282c6df85e76e616679473511e94f812`
- Ancestry: `79db86e` (local Phase10) is an ancestor of `c9815f1`; verified locally with `git merge-base --is-ancestor`.
- T02 branch: `cc/T02-phase11-adoption`
- Merge commit: `7cca83e4bddffb2fa4017267c63e136c0ca15baa` (`merge --no-ff`, upstream history preserved)
- Changed files: 174 upstream files, 40,398 insertions, 677 deletions. No upstream file was rewritten to fake the merge.
- One conflict: `Makefile`, where T01 added premium targets and Phase11 added `phase11-acceptance`/`release-gate` targets. Resolution keeps both sets; no target was dropped.

## What the candidate contains (inspection)

The candidate implements the Phase 11 work already present in the official repository:

- execution reconciliation, including lost/unknown run handling;
- research deep-read and source verification;
- calculation preparation/workspace/registry;
- Slurm SSH backend and job state handling;
- HumanLab handover and operator-reported events;
- preparation is not a decision (state-machine separation);
- service management/identity and role-facing surfaces;
- Phase 11 acceptance documents and tests.

The T02 task is adoption and verification, not a rewrite of these features.

## Verification performed

### Static

- `make lint`: ruff `All checks passed!`; pyright `0 errors, 0 warnings, 0 informations`.

### Unit

- Canonical container: `.venv/bin/python -m pytest tests/unit -q --junitxml=/tmp/unit.xml`
- Result: 1106 tests, 0 failures, 0 errors, 2 skipped.
- The two skips are external/live-dependent and are not counted as PASS; they remain in the legacy inventory as external rows.

### Migrations

- Empty database `ravel_phase11_empty`: `alembic upgrade head` completed; version `d1f7a40c8e21`.
- Phase 10 snapshot database `ravel_phase11_upgrade`:
  - Phase 10 migrations applied to a new database (`b1c4a7e2f903` head);
  - a marker user `usr-phase10-marker` inserted;
  - Phase 11 `alembic upgrade head` applied the seven new revisions;
  - version `d1f7a40c8e21`;
  - marker row still present and unmodified;
  - `alembic heads` reports exactly one head.
- Evidence commands and outputs are recorded in the T02 task log and evidence directory.

### Integration / E2E

- `tests/integration`: 709 tests, exit 0, 100% complete against real PostgreSQL and Temporal with LocalStack 3.8 as the S3-compatible endpoint.
- `tests/e2e`: 27 tests, exit 0.
- LocalStack results are `SIMULATED_S3`: they prove client-side behavior but do not satisfy the GATES requirement for real MinIO. Real MinIO remains an internal environment gap.
- `make phase11-acceptance`: 11/11 items demonstrated, 0 failed, 0 missing; pytest exit 0. Individual skips remain visible in the rows:
  - P11-02 deep read: 1 live/external skip;
  - P11-05 Slurm: 1 real-cluster/external skip;
  - P11-10 services: 1 skip because the canonical container has no user-level systemd (the host WSL has systemd; this is an internal environment gap, not a product failure);
  - P11-11 whole chain: 1 live/external skip.
- Two Phase11 acceptance tests were fixed after the first run:
  - `test_phase11_reconcile.py` now waits for the worker's backend-job row before reading it or terminating the workflow; the previous run raced the submit activity.
  - `test_phase11_services.py`'s `transient_unit` cleanup now checks for a user-level systemd before calling `systemctl`; a container skip no longer becomes an ERROR.
- `make release-gate` is running; its exit code and every row will be recorded as-is. External rows are not converted to PASS.

## Simulated / real / human distinction (T02-AC3)

The merged source keeps execution-type and claim-type vocabularies in code:

- `src/ravel/domain/enums.py` contains `RESEARCH_SOURCE`, `COMPUTATION`, `HUMAN_LAB`, `SIMULATED`.
- The same domain surface carries `FACT`, `INFERENCE`, `HYPOTHESIS`.
- `src/ravel/execution/reconcile.py`, `src/ravel/domain/reconciliation.py` and the gateway admin route keep `UNKNOWN` as an explicit state rather than a zero/empty PASS.
- Phase 11 tests (`tests/unit/domain/test_reconciliation.py`, `tests/integration/reconcile/test_execution_reconcile.py`, `tests/unit/preparation/test_lab.py`, `tests/unit/preparation/test_raspa.py`) exercise those distinctions.

These are code/test facts, not a claim that real HPC or a human experiment ran. The 39 live/dsh nodeids in `legacy-inventory.json` are explicitly classified `external` and remain **BLOCKED_EXTERNAL**; they are never counted as PASS.

## Conflict and line-ending notes

- The Windows checkout writes shell scripts with CRLF. The canonical container needs LF for `scripts/install_services.sh` and the other shell scripts. The T02 worktree uses `extensions.worktreeConfig` + `core.autocrlf=false`; the shell scripts were normalized to LF in the working tree. No tracked content changed (the Git blobs are LF).
- This is an execution-environment fix, not a product change; it does not alter the merge content.

## Known gaps

- Real MinIO is unavailable in the allowed registries; LocalStack is a development substitute marked `SIMULATED_S3`.
- Real HPC, real human-lab execution and live DSH/Research remain external rows; T02 does not claim them.
- The full `make release-gate` result is recorded separately; external rows remain BLOCKED_EXTERNAL and internal skips are not PASS.
