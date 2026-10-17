# Premium contract schemas

These schemas are the frozen v1 integration surface for the premium extension. They extend the existing RAVEL domain; they do not create a second Project/DAG/Evidence authority. Field names may reuse existing columns, but every new field below maps to a persistent column or a documented adapter projection.

| Entity | Schema | Persistent home / constraint | Mutating role |
|---|---|---|---|
| Project lease | `lease.schema.json` | `project_leases(project_id PK, owner_instance_id, epoch, expires_at, renewed_at)`; epoch monotonic; no row without project FK | Supervisor runtime (software), not a product Agent |
| Action | `action.schema.json` | `actions(action_id UUID PK, project_id FK, kind, payload_hash, status, expires_at, ...)`; status check; one CLAIMED attempt via atomic CAS | Master requests; Gateway/Owner approves; execution service claims |
| Approval | `approval.schema.json` | `action_approvals(approval_id PK, action_id FK, payload_hash, principal_user_id, expires_at)`; human membership required | Human Owner only |
| Budget reservation | `budget_reservation.schema.json` | `budget_reservations(reservation_id PK, project_id FK, action_id FK, status, cost_known, ...)`; CNY fixed-point text | Execution service; no model-written cost |
| Mailbox | `mailbox.schema.json` | `mailboxes(mailbox_id PK, owner, TLS flags, credential_ref, fixed_from, allowlist, enabled)`; TLS flags constrained true | Owner/Admin |
| Mail message | `mail_message.schema.json` | `mail_messages(message_id PK, mailbox_id FK, uidvalidity+uid UNIQUE, project_id nullable FK, content_hash, trust)`; trust constrained `EXTERNAL_UNTRUSTED` | Mail worker; project binding by authorized user |
| Sandbox handle | `sandbox_handle.schema.json` | `sandbox_handles(handle_id PK, provider, project_id FK, epoch, capabilities, state)`; provider/state checks; no secret column | Execution service; operator pre-provisions VM |
| Project event | `event.schema.json` | `project_events(event_id PK, project_id FK, seq, type, aggregate_id, aggregate_version, occurred_at, payload)`; `UNIQUE(project_id, seq)` | Event writer inside the authoritative transaction |
| HPC target | `hpc_target.schema.json` | `hpc_targets(target_id PK, kind, host/port, credential_ref, known_hosts_ref, limits, enabled)`; secrets only by reference | Operator/Admin |
| Calculation manifest | `calculation_manifest.schema.json` | `calculation_manifests(manifest_id PK, contract_version, software, parameters JSONB, resource_request, required_outputs, stop_conditions, materializer_version)` | Research freezes; Worker executes |
| Human-lab event | `lab_event.schema.json` | Reuses Phase11 lab handover/event tables; adds actor, protocol_version, sample_id, SOP_ref, lead confirmation, person_reported | Authorized human operator only |
| Scientific report | `report.schema.json` | `scientific_reports(report_id PK, project_id FK, data_mode, generated_at)` + claim/evidence link tables | Review/Master assemble; no evidence type promotion |

## Identity rule

No schema contains a trusted `project_id`, `role`, `binding_id`, `owner_instance_id`, or `lease_epoch` that a model may choose. Those values are attached by the server from the runtime binding or Gateway principal and validated against the current lease epoch before any side effect. A schema field named `project_id` is a projection of the authoritative binding, not an input to it.

## Migration plan

1. Add nullable columns/tables in one Alembic revision; keep existing rows readable.
2. Backfill only deterministic defaults; never invent external evidence.
3. Add constraints after backfill (`CHECK` status vocabularies, `UNIQUE(project_id, seq)`, UID uniqueness).
4. Keep one Alembic head; never edit an applied revision.
5. The migration test applies Phase 11 to an empty database and to a Phase 10 snapshot, then verifies both reach one head and existing rows remain readable.
