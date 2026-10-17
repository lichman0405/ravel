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

V0's node-type vocabulary:

- `RESEARCH`
- `HYPOTHESIS`
- `COMPUTATION`
- `EXPERIMENT`
- `REVIEW`
- `DECISION`

Five of the six may be planned. `REVIEW` may not: a review is not a kind of
work the plan contains but a checkpoint *on* a node that runs, and RAVEL asks
for it through that node's own status — a `COMPUTATION` or `EXPERIMENT` node is
cleared to run by a `PRE_RUN` verdict, and every node that ran holds at
`REVIEWING` until a `FINAL` verdict moves it. Planning a node of that type asked
for a second copy of a question already being asked, and nothing was ever
dispatched to one; `KNOWN_LIMITATIONS.md` L-27 is the record. The value stays in
the vocabulary so that a node written before the change can still be read.

`HYPOTHESIS` and `DECISION` are Master's control nodes: the plan holds a claim
or a decision the rest of it depends on, the loop puts such a node to Master for
as long as it is unfinished rather than handing it to an execution seat, and
cancelling it or replacing it with work that runs is the ordinary ending.
`RESEARCH`, `COMPUTATION` and `EXPERIMENT` are the three a seat carries out.

Optional control metadata may express waiting/approval but must not invent arbitrary scientific node types in V0. `ravel.domain.state_machines.PLANNABLE_NODE_TYPES` is the set a node may be created as, and `DagNode.create` refuses anything outside it.

## 3. Rolling horizon

RAVEL keeps:
- high-level Roadmap: broad future phases
- executable Scientific DAG: near-term concrete nodes

Master should fully expand current stage and at most 1–2 next research stages when confidence supports it.

The two are stored as two things. The roadmap is `RoadmapPhase` rows — a name, a
position counting from 0, and what the stage is for — and Master writes them
with `commit_roadmap_phase`. A project is created with no stages at all, so on a
new project this is the first planning act, and a node cannot be committed to a
stage that does not exist yet. The DAG is `DagNode` rows, each naming the stage
it is work for.

How far the DAG may reach is derived, never stored:

- the current stage is the first one that is not settled — either it has no
  nodes yet, or some of its nodes have not reached a terminal status;
- the current stage and the two after it may be expanded into nodes, which is
  the "1–2 next research stages" above;
- a stage with nodes is settled once every one of them is terminal. PASSED,
  FAILED and CANCELLED are all endings, so a stage whose work was cancelled is
  behind the project rather than still under way;
- if every stage is settled, the current stage is the last one, so a project
  that has exhausted its roadmap can still be given follow-up work on the stage
  it ended on.

Nothing records which stages were expanded: that is a fact the DAG already
carries, and a stored copy would be a second answer that could disagree with the
first.

Committing a stage writes no `DecisionRecord`; committing the nodes that expand
it does. A decision records what changed among the nodes, and a stage on its own
commits none.

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

它也是 Project 自己的生命周期所依据的那份记录，两个状态由它和 DAG 决定，
不需要任何人记得去改：

- 第一版由 Master 经 `commit_success_contract` 写下一版，
  `SuccessContractRepository` 在同一个事务里把 Project 从 `CREATED`
  推到 `CONTRACT_DEFINED` —— 一个 Project 不可能一边持有 frozen 的成功定义，
  一边状态还说它没有；
- `CONTRACT_DEFINED → EXECUTING` 发生在第一个 node 进入 `RUNNING` 时，
  由 `DagRepository._apply_transition` 完成。Project 有没有开始，是 DAG
  知道的事：所有写 node 状态的路径都经过这一个函数，所以
  `begin_research` 和 Worker 的 `start_execution` 都不需要知道这件事。

A20 的四个 ending 里，COMPLETED / FAILED / INCONCLUSIVE 都是 `EXECUTING`
的出边，所以没有第二个状态，Project 就没有任何 ending 可以到达。记录
ending 的是 `conclude_project`（Master 独有），它把结果交给
`MasterService.conclude`；判据和拒绝理由都不变。
