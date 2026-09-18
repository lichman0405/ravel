# 14 — Canonical Development Environment

## Canonical platform

RAVEL V0 is developed, tested, and deployed against:

- OS: **Ubuntu 24.04 LTS x86_64**
- Shell: bash
- Python: **3.12**
- Python package manager: prefer `uv`
- Container runtime: Docker Engine + Docker Compose v2
- Browser runtime: Playwright Chromium
- Harness: pinned real DeepSeek Harness release/tag/commit selected during Phase 0

This Ubuntu environment is the reference behavior for all V0 acceptance tests.

Windows and macOS are not canonical V0 development environments. They may later be used to validate the local TUI client only.

## Development topology

```text
Ubuntu 24.04 LTS
├── RAVEL source checkout
├── Python 3.12 virtual environment
├── pinned DSH runtime
├── Playwright Chromium
└── Docker Compose infrastructure
    ├── PostgreSQL
    ├── Temporal
    └── MinIO
```

RAVEL application code should normally run natively in the Python environment during development. Infrastructure services may run in Docker Compose.

## Production alignment

V0 production target is also Ubuntu 24.04 LTS x86_64 on a cloud VM.

Principle:

> Development Linux should be as close as practical to production Linux.

## Baseline tools

The implementation agent should verify and install only what is actually required. Typical baseline:

- git
- curl
- ca-certificates
- build-essential
- pkg-config
- Python 3.12
- Docker Engine
- Docker Compose plugin

## Node / TypeScript

RAVEL is Python-first.

Install Node.js/pnpm only if the pinned DSH release requires it for DSH-native bundle/preset/plugin work. Use versions required by that pinned DSH documentation and record them.

Do not move RAVEL scientific/domain/state logic into TypeScript simply because DSH itself uses TypeScript.

## Filesystem

Keep the canonical repository on a native Linux filesystem, e.g.:

`~/code/ravel`

## Secrets

- real secrets must never enter Git
- create `.env.example`
- use environment variables or a local secret mechanism
- if one real search provider requires credentials and none are available, use another legitimate real public source/provider
- never replace live Research with mock Web search

## Bootstrap deliverables

Implementation must create:

- `scripts/bootstrap_ubuntu.sh`
- `.env.example`
- `docker-compose.yml`
- `scripts/dev_up.sh`
- `scripts/dev_down.sh`
- `scripts/test_all.sh`
- `scripts/test_live_research.sh`

## Environment gate

Before core implementation proceeds, verify:

- Python 3.12 environment works
- Docker Compose starts PostgreSQL, Temporal and MinIO
- DB connectivity works
- object storage health check works
- Temporal smoke test works
- Playwright Chromium smoke test works
- pinned DSH smoke test works
- one custom RAVEL DSH preset/tool integration works
