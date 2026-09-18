# 03 — Scientific DAG

## 1. Purpose

Scientific DAG is RAVEL's explicit research execution plan.

It is:
- typed
- dynamic
- rolling horizon
- versioned/auditable
- parallel
- Master-mutated only

It is not:
- LLM conversation history
- Temporal workflow graph
- user-editable workflow canvas

## 2. Node types

V0 allowed node types:

- `RESEARCH`
- `HYPOTHESIS`
- `COMPUTATION`
- `EXPERIMENT`
- `REVIEW`
- `DECISION`

Optional control metadata may express waiting/approval but must not invent arbitrary scientific node types in V0.

## 3. Rolling horizon

RAVEL keeps:
- high-level Roadmap: broad future phases
- executable Scientific DAG: near-term concrete nodes

Master should fully expand current stage and at most 1–2 next research stages when confidence supports it.

## 4. Parallelism

Native support:
- fan-out
- fan-in
- ALL join
- ANY join
- threshold join
- failure-tolerant branches

Each join must specify:
- dependencies
- completion rule
- failure policy

## 5. Required node fields

See `schemas/scientific_dag.schema.yaml`.

Core:
- node_id
- project_id
- node_type
- objective
- status
- dependencies
- executor_role
- acceptance_contract_ref
- execution_contract_ref
- artifact_refs
- created_by
- decision_ref
- timestamps

## 6. Node status

Allowed V0:
- PLANNED
- READY
- RUNNING
- WAITING_EXTERNAL
- WAITING_DECISION
- REVIEWING
- PASSED
- FAILED
- PARTIAL
- BLOCKED
- CANCELLED

State transitions must be validated in code.

## 7. Mutation

Only Master may submit a DAG mutation command.

Every material mutation requires immutable `DecisionRecord`:
- trigger
- rationale
- evidence refs
- alternatives considered
- affected nodes
- authority check
- confidence

Mutation is append/transition oriented. History must remain reconstructable.

## 8. Acceptance Criteria freeze

For COMPUTATION/EXPERIMENT:
- criteria must be defined before entering RUNNING
- freeze version on execution start
- Review uses frozen version
- result cannot cause retroactive threshold editing

If benchmark was wrong:
1. original node keeps FAIL/PARTIAL
2. Master creates Decision Record
3. new criterion version
4. new node or new execution version

## 9. Provenance

Every acceptance criterion requires provenance:
- user_requirement
- literature_derived
- standard
- authoritative_database
- prior_project_result
- research_inference
- provisional

No provenance => cannot present as objective benchmark.

## 10. Project Success Contract

Project-level:
- success criteria
- failure criteria
- termination criteria
- budget/time/iteration limits
- unresolved critical uncertainty policy

Frozen before formal execution; later changes are versioned and require Decision Record.
