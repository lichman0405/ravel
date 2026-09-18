# 11 — Frozen Technology Decisions

## Core

- Canonical OS: Ubuntu 24.04 LTS x86_64
- Canonical Python: 3.12

- Language: Python
- UI: Textual TUI
- Harness: DeepSeek Harness only
- Durable execution: Temporal
- Authoritative DB: PostgreSQL
- Artifact storage: S3-compatible; V0 recommend MinIO
- Scientific DAG: RAVEL custom domain model
- Gateway: Python, recommended FastAPI + WebSocket
- Validation/modeling: Pydantic v2
- ORM/migrations: SQLAlchemy 2 + Alembic
- Browser research: Playwright
- HTTP: httpx or equivalent
- Tests: pytest + integration/E2E suites

## Deployment

V0:
- Linux CVM
- Docker Compose acceptable/preferred for infrastructure services
- one DSH Host
- no Kubernetes

## Explicitly not selected

- Neo4j
- Kafka
- Redis by default
- Go as core language
- Rust as core language
- C++
- full web frontend
- multi-harness adapter layer

## Future language exception

A future Lab Edge Connector may use Rust if a single-binary, cross-platform, low-resource local daemon becomes necessary. This is not a V0 requirement.

## DSH-native extension exception

RAVEL remains Python-first. However, the pinned DSH release may require a small
JavaScript/TypeScript/Cordis bundle/preset layer for native plugin/tool registration.
This layer must remain thin; scientific/domain/state logic stays in Python.

