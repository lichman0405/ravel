# RAVEL V0 Acceptance Matrix

V0 is not complete until all 20 items pass.

## A01 Project creation
Create Project, Research Contract draft, Authority Envelope, Project Success Contract draft.

## A02 Master start
Master identity/session starts and can read Project State.

## A03 Real research
Research Agent performs live real Web/API/database research. No mock search.

## A04 Evidence provenance
Evidence is registered only after original source verification; tier, retrieval timestamp, hash/ref, access status present.

## A05 Initial Scientific DAG
Master creates typed rolling-horizon DAG through authorized mutation path.

## A06 Compute success
Mock Compute Worker executes a successful task and submits complete ExecutionRecord/artifacts.

## A07 Compute failure
Mock backend produces failure; Worker follows allowed retry/escalation; no unauthorized scientific parameter change.

## A08 Review outcomes
Review returns PASS/FAIL/PARTIAL against frozen criteria and persists ReviewRecord.

## A09 Master replanning
A FAIL causes Master to create DecisionRecord and mutate future DAG; original failed node remains historically failed.

## A10 Long lab wait
Mock Experimental Worker enters WAITING_EXTERNAL and later resumes from Temporal signal/event.

## A11 Lab deviation
Mock Lab reports out-of-contract condition; Worker pauses and escalates; does not answer scientifically itself.

## A12 Contract response
Master resolves deviation by authorized decision: revised contract/new node/termination; history preserved.

## A13 Incomplete delivery
Worker detects missing required artifact, requests it or produces INCOMPLETE_DELIVERY; Review is not given a false complete result.

## A14 Acceptance freeze
Attempt to alter executed node's frozen Acceptance Criteria is rejected. New criteria requires new version + DecisionRecord + new execution/node.

## A15 Master session kill/recovery
Kill Master DSH session/process. Project survives. Recover Master from Project State + checkpoint; continue correctly.

## A16 Runtime/Temporal restart
Restart runtime/Temporal workers while a task waits. Waiting task resumes without lost authoritative state.

## A17 Parallel branch + join
Run at least two parallel branches and demonstrate ALL/ANY or threshold join behavior.

## A18 TUI interaction
Owner can view Master/current state/read-only DAG/Decision/Review/task status and execute allowed pause/resume/approval. Lab user can view assigned task/upload/report deviation. Admin can inspect runtime health.

## A19 DAG authorization
Direct DAG mutation by user/Research/Review/Worker is denied at service authorization level, not merely prompt instruction.

## A20 Project outcome
End-to-end project reaches one of:
- SUCCESS
- FAILED
- INCONCLUSIVE
- TERMINATED

with final audit trail.

---

# Required extra gates

These are part of the above acceptance and cannot be skipped:

- live Research test must reach real original sources
- fabricated DOI/source => automatic failure
- mock compute/lab data must be explicitly marked simulated
- DSH must be pinned
- all 5 roles use intended DSH preset/tool scope
- no direct public DSH endpoint
- Postgres remains authoritative after restarts
