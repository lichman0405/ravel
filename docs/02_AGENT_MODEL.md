# 02 — Agent Model

RAVEL V0 固定 5 类独立 Agent。不要新增角色。

## 1. Master / Supervisor

### Lifecycle
- 1 个 Project 对应 1 个 Master identity
- 默认长期在线 persistent DSH session
- session 可 crash/rebuild
- identity/state/decisions 跨 session 存续

### Visibility
Master 有完整 Project visibility，但**每轮 context 只检索必要片段**。

Full visibility != full context window.

### Exclusive authority
只有 Master 可以：
- mutate Scientific DAG
- 创建/取消/替换 node
- 修改 Execution Contract
- 正式改变 research route
- 处理 worker deviation
- 在 Authority Envelope 内自主决策
- 产生 Decision Record

### Must not
- 绕过 Review 把执行结果判定为通过
- 覆盖已有历史记录
- 事后修改 frozen Acceptance Criteria
- 用模型记忆代替 Evidence

## 2. Research Agent

### Identity
Project 级独立角色，不是 Master subagent。

### Work model
按 `RESEARCH` task 激活一个 scoped session。
它可以在当前 task 内自主规划检索步骤，但不能扩张 Project DAG。

### Responsibilities
- real literature/web/database/patent/standard research
- preliminary low-cost analysis
- Evidence Ledger construction
- Fact / Inference / Hypothesis separation
- benchmark/acceptance suggestion
- conflict recording
- Evidence Sufficiency Assessment
- Research Completion Contract

### Output
1. Structured Research Record — authoritative
2. Human-readable Research Report — projection

### Must not
- run formal HPC computation
- send real lab task
- mutate DAG
- register unverifiable claims as Evidence

## 3. Review Agent

### Lifecycle
Project 级独立角色，按 checkpoint 激活。

### Checkpoint types
- PRE_RUN
- RUNTIME
- FINAL

### Output
- PASS / FAIL / PARTIAL
- diagnosis
- evidence/references
- acceptance comparison
- recommendations
- Review Record

### Must not
- mutate DAG
- modify Execution Contract
- change Acceptance Criteria
- decide research route

Review recommendation is advisory only.

## 4. Compute Worker

### Lifecycle
Task-scoped.
创建于 computation task 启动，结束于正式交付给 Review 后。

### Responsibilities
- read frozen Execution Contract
- prepare inputs
- invoke Compute Backend
- wait
- collect outputs/logs
- Delivery Completeness Check
- submit Execution Record to Review

### Authority
Only actions explicitly permitted by Execution Contract.

May retry only if policy explicitly allows deterministic retry.

Scientific changes (method, model, parameters outside bounds) require escalation.

## 5. Experimental Worker

### Lifecycle
Task-scoped but may live for hours/days while waiting for lab response.

### Responsibilities
- issue approved experiment instruction through Experiment Backend
- communicate with lab within Execution Contract
- collect artifacts
- completeness check
- report deviations
- wait for Master decision
- resume with revised/new contract when authorized
- submit Execution Record to Review

### Communication rule
It does not classify "execution question vs science question" using judgment.

It only asks:
> Is this action explicitly allowed by Execution Contract?

If no:
`PAUSE → DEVIATION → MASTER`.

## 6. Separation of powers

```text
Research  -> evidence/advice
Worker    -> execution
Review    -> acceptance/diagnosis/advice
Master    -> decision and DAG mutation
User      -> constraints/approval/intervention, never DAG editing
```

## 7. Master memory model

Three layers:

### Working Memory
- current focus
- transient ideas
- unresolved observations
- recent conversation

### Authoritative Project State
Anything that changes future execution must crystallize here:
- hypothesis
- evidence
- decision
- DAG mutation
- contract
- review

### Checkpoint
Periodic structured checkpoint:
- current_focus
- active_hypotheses
- pending_questions
- waiting_on
- recent_decision_refs
- important_context_refs
- last_event_seq

Session recovery loads Project State + checkpoint + relevant recent records.
