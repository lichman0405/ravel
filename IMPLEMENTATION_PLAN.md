# RAVEL V0 — Implementation Plan

**Goal:** Implement RAVEL V0 as a real autonomous scientific research runtime until every
executable item in `acceptance/V0_ACCEPTANCE.md` passes.

**Harness:** DeepSeek Harness, pinned at `dsh-v0.1.5-rc.1` (`vendor/DSH_PIN.json`).

**Stack:** Python 3.12 · Pydantic v2 · SQLAlchemy 2 + Alembic · PostgreSQL · Temporal Python SDK ·
FastAPI + WebSocket · Textual · Playwright · MinIO (S3-compatible) · pytest.

**Spec sources:** `docs/00`–`docs/15`, `schemas/*.yaml`, `prompts/*.md`, `acceptance/*`.

---

## Global Constraints

Copied verbatim from `START_PROMPT.md`; every task in every phase inherits these.

- Only DSH is supported. No Claude/Codex/Grok harness adapter.
- Exactly five agent roles: Master / Research / Review / Compute Worker / Experimental Worker.
  No Planner, Memory, Router, Safety, or Scheduler agent.
- Only Master may mutate the Scientific DAG, edges, Execution Contracts, or research route.
- Real research only. No mock web search, no fabricated DOI, no fabricated page, no model prior
  presented as a source. A search result is a lead until the original source is opened.
- Compute/Lab V0 use `MockComputeBackend` / `MockLabBackend`; mock data is labelled and never
  enters the Evidence Ledger as real scientific evidence.
- PostgreSQL is the authoritative Project State. Temporal is durable execution only.
  A DSH session is not a Project. A Temporal workflow is not the Scientific DAG.
- Artifact binaries live in S3-compatible object storage, never in PostgreSQL.
- No Kubernetes, Neo4j, Kafka, or Redis. No general Web dashboard. No POST dependency.
- Secrets never enter Git. `.env.example` documents every variable.

---

## Verified Environment Facts

These were established by direct experiment before this plan, not assumed.

| Fact | Consequence |
|---|---|
| Runtime and wheels exist only as PyPI `0.1.5rc1`; GitHub releases carry no assets | Pin is the PyPI pair plus upstream commit `183f08e9c6dde7e36cd2318eaee70b0da08fb35e` |
| SDK JSON-RPC exposes exactly `initialize` / `session/prompt` / `shutdown` | RAVEL cannot host tools for an agent over the SDK channel |
| `tools/call` params are `{ name, arguments }` only — no session identity | An MCP server cannot learn its caller from the protocol; scope must be structural |
| `dsh-mcp-client` merges configured `env` over a scrubbed ambient env (drops `DSH_*` and `*TOKEN*/*SECRET*`) | RAVEL's own variables survive into the MCP child; secrets are scrubbed by default |
| `sdk-app` / `sdk-minimal` bundles do not depend on `dsh-agent-presets` | Per-session preset selection is unreachable through the SDK; host-plane composition is the intended SDK shape |
| Cross-process session resume fails: `session "<id>" already exists` | No RAVEL correctness path may depend on continuing a DSH session |
| Host ports 5432 / 9000 / 9001 are occupied by an unrelated local project | RAVEL's compose file must publish different host ports |

---

## Architecture Decisions

**D1 — Tools are RAVEL MCP servers over stdio.**
All agent capability is exposed as MCP servers written in Python, mounted through
`@deepseek-ai/dsh-mcp-client`. This is configuration-only, keeps every domain decision in Python,
and requires no TypeScript. No DSH core is forked or patched.

**D2 — One DSH runtime process per `(project_id, role)`.**
Because `tools/call` carries no session identity, a shared MCP server would have to trust a
model-supplied project id — which the security model forbids. Scoping the runtime process instead
means `RAVEL_PROJECT_ID` and `RAVEL_ROLE` arrive through the process environment, so the MCP server
knows its authority by construction and the model cannot widen it. Runtimes start lazily on first
use and are idle-reaped. Deviation from the "one DSH Host" phrasing in `CLAUDE.md`, recorded in
`docs/IMPLEMENTATION_DEVIATIONS.md`.

**D3 — Role profiles carry persona and exactly one MCP server.**
Each role profile mounts `@deepseek-ai/dsh-persona` plus its own RAVEL MCP row. No `bash`,
no filesystem, no DSH `tool-web`. A Research Agent researches through RAVEL's gateway so that
provenance, retrieval metadata, and hashes are recorded at the moment of retrieval; a DSH built-in
web tool would produce real pages that RAVEL could never cite. Tool scope is therefore enforced by
composition, and verified two ways: statically against `--dump-config`, behaviourally by a test
that asserts a Master-scoped tool is absent from a Research session.

**D4 — Master runs persistently; recovery rebuilds from authoritative state.**
A long-lived DSH session is held for Master while the process lives. After a crash or restart,
RAVEL creates a *replacement* session under the same Master identity with a fresh session id, and
reconstructs working context from PostgreSQL Project State plus the latest `MasterCheckpoint`
(`docs/07` §8). No path replays chat history.

**D5 — Temporal owns durability, never science.**
Workflows run activities that call the same domain services the API calls. The DAG lives only in
PostgreSQL. A workflow restart must be indistinguishable from a slow activity to the scientific
record.

---

## Phase -1 — Ubuntu Environment Bootstrap

**Deliver**
- `scripts/bootstrap_ubuntu.sh` — verifies Ubuntu 24.04 x86_64, installs Python 3.12, Docker
  Engine + Compose v2, Playwright Chromium system deps; creates the project venv.
- `docker-compose.yml` — PostgreSQL, Temporal, Temporal UI, MinIO on non-conflicting host ports.
- `.env.example`, `scripts/dev_up.sh`, `scripts/dev_down.sh`, unified `pytest` entrypoint.
- `src/ravel/` package skeleton and `pyproject.toml`.

**Gate** `make env-check` passes: Postgres accepts a connection, Temporal reports ready, MinIO
round-trips an object, Chromium launches headless, `pytest` collects zero failures.

---

## Phase 0 — DSH Integration Spike

**Deliver**
- `vendor/DSH_PIN.json`, `vendor/DSH_PATCHES.md` — **written**.
- `src/ravel/dsh/client.py` — typed boundary over `deepseek-harness-sdk`; no SDK type escapes it.
- `src/ravel/dsh/runtime_pool.py` — lazy `(project_id, role)` runtime processes, idle reaping.
- `src/ravel/dsh/session_binding.py` — session ↔ `(project_id, role, task_id)` registry.
- `dsh/profiles/ravel-*.cordis.patch.yml` — five role compositions.
- A trivial echo MCP server proving the Python tool path end to end.

**Gate** `tests/dsh/test_spike.py` proves, against a real runtime: one role runtime starts and
stops; two roles coexist; a session is bound to `(project_id, role, task_id)`; an RAVEL MCP tool
executes inside an agent turn; a Master tool is unreachable from a Research session; the
persistence experiment reproduces the documented resume limitation; recovery rebuilds a Master
session from state. No business code before this passes.

---

## Phase 1 — Core Domain and Persistence

**Deliver** Pydantic domain models (`project`, `dag`, `contracts`, `evidence`, `artifacts`,
`decisions`, `reviews`); the state machines; SQLAlchemy models, Alembic migrations, repositories;
MinIO artifact store with immutable, hashed, versioned artifacts; a transactional outbox.

**Gate** `tests/unit/domain` and `tests/integration/state`: illegal state transitions are rejected;
artifact bytes are content-addressed and immutable; a rolled-back transaction emits no event.

---

## Phase 2 — Scientific DAG

**Deliver** Typed nodes and edges; validated transitions across the 11 states; rolling-horizon
planning; ALL/ANY/THRESHOLD joins; the Master-only mutation service; Decision Record enforcement;
Acceptance Criteria freeze with provenance.

**Gate** `tests/unit/dag` and `tests/integration/dag`: a non-Master caller cannot mutate; frozen
criteria reject later edits; a THRESHOLD join fires only when satisfied.

---

## Phase 3 — DSH Role Profiles and Tools

**Deliver** Five role profiles with role-specific MCP servers; Master project-state retrieval
tools; no DAG-mutation tool in any non-Master profile; session binding registry integration;
Master checkpoint write/restore.

**Gate** `tests/integration/roles`: role permission matrix passes for all five roles; the
composition dump asserts the tool roster per profile.

---

## Phase 4 — Real Research Source Gateway

**Deliver** Real structured connectors (Crossref, OpenAlex, arXiv, and standards/patent sources),
configurable real web search, Playwright browser navigation, source opening and verification,
evidence tiering, permitted snapshots with hashes, Fact/Inference/Hypothesis classification,
sufficiency evaluation, ResearchRecord production. Restricted access is recorded explicitly as
`PAYWALLED` / `AUTH_REQUIRED` / `ACCESS_LIMITED` and never guessed.

**Gate** `tests/live_research/` runs against the real Internet and asserts that every registered
Evidence row carries a retrievable URL, a real retrieved-at timestamp, and a content hash matching
the stored bytes. Unit tests may mock the HTTP parser; this suite may not.

---

## Phase 5 — Temporal Durable Execution

**Deliver** Activity and workflow layer; long waits with timers; external signals; retry and
timeout policies; worker-restart recovery. Zero DAG ownership in Temporal.

**Gate** `tests/integration/temporal`: kill the worker mid-activity, restart, and observe the
project reach the same state a clean run reaches.

---

## Phase 6 — Mock Execution Backends

**Deliver** Compute and Experiment backend interfaces plus their mock implementations, covering
every scenario in `acceptance/MOCK_SCENARIOS.yaml`: success, retryable infrastructure failure,
scientific failure, missing output, timeout, long lab wait, deviation pressure, missing raw data,
and undefined operator question.

**Gate** `tests/integration/backends`: each named scenario produces exactly its specified
transition; every mock-derived record is labelled as mock.

---

## Phase 7 — Review and Closed Loop

**Deliver** Pre-flight, runtime, and final checkpoints; PASS/FAIL/PARTIAL outcomes; Master
replanning after failure; Decision Records; terminal project states.

**Gate** `tests/e2e/test_headless_loop.py` drives a full project from creation to a terminal
outcome with no human input.

---

## Phase 8 — Research Gateway and Textual TUI

**Deliver** Authentication, role permissions, REST + WebSocket API, artifact upload, Owner/Lab/Admin
views, the Master conversation stream, a read-only DAG view, pause/resume/approval controls, and no
direct DSH endpoint exposure.

**Gate** `tests/e2e/test_tui.py` drives the TUI headlessly through the Owner, Lab, and Admin paths;
a test asserts that no route accepts a DAG mutation from a user.

---

## Phase 9 — Resilience and V0 Acceptance

**Deliver** All 20 acceptance cases executed; `DEPLOYMENT.md`, `TEST_REPORT.md`,
`IMPLEMENTATION_REPORT.md`, `KNOWN_LIMITATIONS.md`, `DSH_INTEGRATION_REPORT.md`,
`SECURITY_NOTES.md`.

**Gate** `make acceptance` runs A01–A20 plus the extra gates and prints a pass/fail matrix.

---

## Risk Register

| # | Risk | Mitigation |
|---|---|---|
| R1 | DSH is Developer Preview; a re-pin breaks the boundary | All DSH contact confined to `src/ravel/dsh/`; pin is exact; spike tests fail loudly on drift |
| R2 | Cross-process session resume is unavailable | D4 — recovery never depends on it; A15 tests the rebuild path |
| R3 | Search provider credentials absent | Fall back to keyless real sources (Crossref, OpenAlex, arXiv); never mock |
| R4 | Real sites change or block automation | Record `PAYWALLED`/`ACCESS_LIMITED` explicitly; live tests assert provenance, not fixed content |
| R5 | DSH base rows leak unintended tools into a profile | Static `--dump-config` assertion plus behavioural denial test per role |
| R6 | Temporal and Postgres disagree after a crash | Postgres is authoritative; activities are idempotent; A16 tests the divergence case |
| R7 | Host port collisions on the CVM | Non-default host ports in `docker-compose.yml` |
| R8 | Live research makes CI slow or flaky | Live suite is a separate marker with retries and a recorded-artifact assertion |

## Dependencies

Phase -1 → Phase 0 → Phase 1 → Phase 2 → Phase 3 → { Phase 4, Phase 5, Phase 6 } → Phase 7 → Phase 8 → Phase 9.
Phases 4, 5, and 6 are independent of one another and may proceed in parallel. Phase 2 precedes
Phase 3 because the Master tools wrap the mutation service. No phase may begin before its
predecessor's gate passes.

## Test Entrypoints

| Command | Scope |
|---|---|
| `make test` | unit tests, no external services |
| `make test-integration` | requires compose stack |
| `make test-dsh` | Phase 0 spike against a real pinned runtime |
| `make test-live` | Phase 4 real-Internet research |
| `make test-e2e` | Phases 7–8 headless loop and TUI |
| `make acceptance` | A01–A20 plus extra gates |

## Acceptance Mapping

| Phase | Acceptance items |
|---|---|
| 0 | A02, A15 (rebuild path) |
| 1 | A01 |
| 2 | A05, A14, A17, A19 |
| 3 | A02, A15 |
| 4 | A03, A04 |
| 5 | A16 |
| 6 | A06, A07, A10, A11, A12, A13 |
| 7 | A08, A09, A20 |
| 8 | A18 |

## Out of Scope for V0

Real Slurm/HPC and real LIMS/robotic-lab backends (interfaces only) · a general Web dashboard ·
POST integration · multi-node deployment · Kubernetes · Neo4j · Kafka · Redis.
