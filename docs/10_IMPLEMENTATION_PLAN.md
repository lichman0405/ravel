# 10 — Recommended Implementation Plan

The implementation agent may adjust internal sequence only if acceptance gates remain intact.


## Phase -1 — Ubuntu Environment Bootstrap

Deliver:
- Ubuntu 24.04 canonical development setup
- Python 3.12 env
- Docker Compose PostgreSQL/Temporal/MinIO
- Playwright Chromium
- bootstrap/dev/test scripts

Gate:
all environment smoke tests pass before DSH Integration Spike.

## Phase 0 — DSH Integration Spike

Deliver:
- official DSH checkout/pin
- `vendor/DSH_PIN.json`
- one running DSH Host
- custom scientific preset proof
- multiple role sessions proof
- session metadata binding
- tool scoping proof
- persistence/restart experiment
- documented current resume behavior

Gate:
No core business implementation until spike passes.

## Phase 1 — Core domain + persistence

Deliver:
- Python project scaffolding
- Postgres migrations
- Pydantic domain models
- state machines
- project/event repositories
- S3/MinIO artifact layer
- immutable artifact versioning
- transaction/outbox

Gate:
domain unit tests.

## Phase 2 — Scientific DAG

Deliver:
- typed node/edge
- validated transitions
- rolling plan representation
- parallel/join
- Master-only mutation service
- Decision Record enforcement
- Acceptance freeze

Gate:
mutation and freeze tests.

## Phase 3 — DSH role presets/tools

Deliver:
- 5 presets
- role-specific tools
- Master project-state retrieval
- no unauthorized DAG tool for other roles
- session binding registry
- Master checkpoint

Gate:
role permission integration tests.

## Phase 4 — Real Research Source Gateway

Deliver:
- real structured source connectors
- real configurable web search
- real browser navigation
- source opening/verification
- evidence tiering
- snapshots/hash where permitted
- Fact/Inference/Hypothesis
- sufficiency
- ResearchRecord

Gate:
live Internet E2E research test with provenance.

## Phase 5 — Temporal durable execution

Deliver:
- activity/workflow layer
- long wait
- external signal
- retry/timeout
- runtime restart recovery
- no Scientific DAG ownership in Temporal

Gate:
restart/wait tests.

## Phase 6 — Mock execution backends

Deliver:
- Compute Backend interface + MockComputeBackend
- Experiment Backend interface + MockLabBackend
- long wait
- failure
- missing delivery
- deviation
- resume after Master decision

Gate:
all mock scenario tests.

## Phase 7 — Review + closed loop

Deliver:
- pre/runtime/final checkpoint
- PASS/FAIL/PARTIAL
- Master replanning after failure
- Decision Records
- project outcome states

Gate:
full headless loop.

## Phase 8 — Research Gateway + Textual TUI

Deliver:
- auth
- role permissions
- REST/WebSocket
- artifact upload
- Owner/Lab/Admin views
- Master conversation stream
- read-only DAG
- pause/resume/approval
- no direct DSH exposure

Gate:
TUI E2E.

## Phase 9 — Resilience + V0 acceptance

Run all 20 acceptance cases:
- process kill
- DSH session kill
- Temporal worker restart
- parallel branch
- deviation
- criterion freeze
- role access control
- final project status

Produce:
- deployment guide
- test report
- architecture status
- known limitations
