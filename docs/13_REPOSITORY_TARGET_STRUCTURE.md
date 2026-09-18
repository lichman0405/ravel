# 13 — Target Repository Structure

This is a target shape, not a requirement to create every file on day one.

```text
ravel/
├── README.md
├── pyproject.toml
├── docker-compose.yml
├── .env.example
├── vendor/
│   ├── DSH_PIN.json
│   └── DSH_PATCHES.md
│
├── dsh/
│   ├── bundle/                       # installable RAVEL DSH bundle
│   │   ├── package.json
│   │   ├── cordis.patch.yml
│   │   └── src/ or index.js
│   └── agent-presets/
│       ├── ravel-master/
│       ├── ravel-research/
│       ├── ravel-review/
│       ├── ravel-compute-worker/
│       └── ravel-experimental-worker/
│
├── src/ravel/
│   ├── domain/
│   │   ├── project.py
│   │   ├── dag.py
│   │   ├── contracts.py
│   │   ├── evidence.py
│   │   ├── artifacts.py
│   │   ├── decisions.py
│   │   └── reviews.py
│   ├── state/
│   │   ├── repositories/
│   │   ├── models/
│   │   ├── migrations/
│   │   └── outbox/
│   ├── dsh/
│   │   ├── client.py
│   │   ├── session_binding.py
│   │   ├── checkpoint.py
│   │   └── presets.py
│   ├── research/
│   │   ├── gateway.py
│   │   ├── search/
│   │   ├── browser/
│   │   ├── connectors/
│   │   ├── evidence_registry.py
│   │   └── snapshots.py
│   ├── execution/
│   │   ├── temporal/
│   │   ├── compute/
│   │   ├── experiment/
│   │   └── completeness.py
│   ├── review/
│   ├── master/
│   ├── gateway/
│   │   ├── api/
│   │   ├── websocket/
│   │   ├── auth/
│   │   └── artifacts/
│   └── tui/
│       ├── app.py
│       ├── screens/
│       ├── widgets/
│       └── theme/
│
├── tests/
│   ├── unit/
│   ├── integration/
│   ├── live_research/
│   ├── dsh/
│   └── e2e/
│
└── docs/
```

## Language boundary

### Python owns
- all RAVEL domain logic
- Project State
- Scientific DAG
- Research Source Gateway
- Temporal orchestration
- Gateway
- TUI
- Backends
- tests

### DSH-native bundle/preset layer may use JavaScript/TypeScript/Cordis config
DeepSeek Harness plugin distribution uses bundle/profile/Cordis concepts. If the pinned DSH release requires JS/TS to register native tools or host services, use the **minimum DSH-native code necessary**.

This does **not** change the Python-first decision.

Do not move RAVEL domain logic into TypeScript merely because DSH itself is implemented in TypeScript.

Preferred seam:

```text
DSH preset/tool layer
    -> thin RPC/tool call
RAVEL Python Runtime
    -> authorization/domain/state/action
```

Where a tool can be implemented safely through official Python SDK/API without a native DSH plugin, prefer the simpler supported mechanism after validating the pinned DSH version.

## DSH bundle rule

At implementation start, read the pinned DSH plugin authoring docs.

Observed in Sep 2026:
- installable DSH bundle is an npm package declaring `dsh.bundle`
- bundle applies `cordis.patch.yml`
- profile is a separate runnable composition
- plugin modules may be JavaScript/TypeScript

Never copy internal DSH source packages into RAVEL.
