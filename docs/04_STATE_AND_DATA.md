# 04 — State & Data Model

## 1. PostgreSQL is authoritative

V0 uses PostgreSQL, not Neo4j.

Graph-like relationships are represented using typed rows and references. If future graph analytics requires Neo4j, it may become a projection/index, never the authoritative source.

## 2. Core entities

- User
- Project
- ProjectMembership
- ResearchContract
- ProjectSuccessContract
- AuthorityEnvelope
- Roadmap
- DagNode
- DagEdge
- AcceptanceContract
- ExecutionContract
- ResearchRecord
- Evidence
- EvidenceSource
- EvidenceConflict
- Artifact
- ArtifactVersion
- DecisionRecord
- ReviewRecord
- ExecutionRecord
- PreparationRecord — the workspace a contract was materialized into, or the
  refusal to materialize it, with the manifest of what was read and written
- LabHandover — what a bench was given, and what it owes back
- RunReconciliation — why RAVEL ended a run it could no longer see
- AgentIdentity
- AgentSessionBinding
- MasterCheckpoint
- ApprovalRequest
- ProjectEvent

## 3. IDs

Use stable opaque IDs (UUID/ULID acceptable).
Never encode mutable semantics in primary IDs.

Human display IDs may be:
- `P-...`
- `RES-...`
- `COMP-...`
- `EXP-...`
- `REV-...`
- `DEC-...`
- `EVD-...`
- `ART-...`

## 4. Artifact rules

Artifact != Evidence.

Artifact:
- binary/file/data entity
- immutable content version
- object storage URI
- content hash
- size
- media/type
- provenance
- project/node association

Never overwrite in place.
Changed file => new ArtifactVersion.

## 5. Evidence rules

Evidence is a semantic research record, may reference:
- Artifact(s)
- external URL
- DOI
- database record
- patent
- standard

Evidence contains:
- statement
- evidence_class
- source refs
- conditions
- confidence/sufficiency
- conflicts
- retrieval metadata

## 6. Object storage

Use S3-compatible object store.
V0 recommended: MinIO.

Suggested key:
`projects/{project_id}/{artifact_id}/{version}/{filename}`

Do not expose raw storage credentials to TUI or Agent.

## 7. Event stream

Project changes produce monotonically sequenced ProjectEvent rows.

Use transactional outbox semantics so DB state and emitted event cannot diverge.

TUI WebSocket receives server projections. Client may reconnect from `last_event_seq`.

## 8. No hidden authoritative state

Never store business-critical state only in:
- DSH JSONL
- prompt text
- Temporal history
- TUI local files
- log files
