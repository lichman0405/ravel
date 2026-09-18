# 01 — System Architecture

## 1. Topology

```mermaid
flowchart TB
    TUI[Local RAVEL TUI] -->|HTTPS / WebSocket / Artifact transfer| GW[Research Gateway]

    GW --> RT[RAVEL Runtime]
    RT --> PG[(PostgreSQL)]
    RT --> OBJ[(S3-compatible Object Storage)]
    RT --> TEMP[Temporal]
    RT --> DSH[Single DeepSeek Harness Host]

    DSH --> MASTER[Master Sessions]
    DSH --> RESEARCH[Research Sessions]
    DSH --> REVIEW[Review Sessions]
    DSH --> CW[Compute Worker Sessions]
    DSH --> EW[Experimental Worker Sessions]

    RT --> RSG[Research Source Gateway]
    RSG --> WEB[Real Web Search/Browser]
    RSG --> DB[Scientific DB/API/Patent/Standards]

    CW --> CB[Compute Backend]
    CB --> MOCKC[MockComputeBackend]
    CB -.future.-> SLURM[Slurm/HPC]

    EW --> EB[Experiment Backend]
    EB --> MOCKL[MockLabBackend]
    EB -.future.-> LAB[Human Lab / LIMS / Robotics]
```

## 2. Strict ownership

### PostgreSQL owns authoritative state
- Project
- contracts
- Scientific DAG
- node status
- decisions
- reviews
- evidence metadata
- artifact metadata
- user/project permissions
- checkpoints
- execution records

### Object storage owns binary artifacts
- PDFs if legally storable
- webpage snapshots if permitted
- experiment raw data
- structures
- input/output files
- logs
- plots
- reports

### DSH owns current agent working sessions
DSH session = working context, not Project.

### Temporal owns durable execution mechanics
Temporal does not know scientific meaning.

## 3. Scientific DAG vs Temporal

Scientific DAG answers:
> What scientifically should happen next?

Temporal answers:
> How can this action survive wait/retry/crash/time?

Never compile the Scientific DAG into a static Temporal DAG.

RAVEL Runtime reads Scientific DAG state and starts/cancels/awaits Temporal workflows/activities as needed.

## 4. One DSH host

V0 has **one DSH Host per deployment**, not one host per user/project.

Isolation rules:
- each agent session has immutable server-bound metadata:
  - `project_id`
  - `agent_role`
  - optional `task_id`
- project_id must be injected server-side, never trusted from model tool arguments
- tools enforce scope from session identity
- project workspaces separated by filesystem path / sandbox policy
- agent preset fixed per session role
- all DSH external network access used for formal Research must be routed/wrapped through RAVEL Research Source Gateway

## 5. Components that are NOT agents

- Scientific DAG Engine
- Research Gateway
- Research Source Gateway
- Temporal workflows
- Artifact Store
- Contract validator
- Authority checker
- Compute Backend
- Experiment Backend
- authentication
- event dispatcher
- database repository layer
- TUI

They are deterministic software.

## 6. Event model

PostgreSQL stores a durable project event stream/outbox. Typical event types:
- PROJECT_CREATED
- MASTER_STARTED
- NODE_READY
- NODE_STARTED
- NODE_WAITING
- NODE_COMPLETED
- NODE_FAILED
- REVIEW_SUBMITTED
- DECISION_CREATED
- DAG_MUTATED
- DEVIATION_REPORTED
- ARTIFACT_REGISTERED
- APPROVAL_REQUESTED
- APPROVAL_RESOLVED
- PROJECT_STATUS_CHANGED

WebSocket streams projections of these events to the TUI.

## 7. Crash model

Expected failures:
- RAVEL process restart
- DSH process/session loss
- Temporal worker restart
- CVM reboot
- network loss
- search provider outage
- Mock/real backend task failure

Recovery must derive from PostgreSQL + Temporal durable state + DSH checkpoint/session persistence, never from memory alone.
