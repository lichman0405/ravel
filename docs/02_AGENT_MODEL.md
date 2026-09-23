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
- 写 Research Contract（`commit_research_contract`）：Project 被要求做什么。
  新增 Project 时它排在 roadmap 之前，且只写一次——用户想要的东西变了是
  新 Project，不是第二份 contract。它不是 DAG mutation（不动任何 node），
  但同样只有 Master 持有
- 写 Success Contract（`commit_success_contract`）：什么算回答了这个问题、
  什么算答反了、什么时候提前停、证据不够时怎么办。新增 Project 时它和
  Research Contract 一样排在最前，第一版就是让 Project 走出 `CREATED`
  的那一步——没有它，A20 的四个 ending 一个都不能记录，因为任何 ending
  都是关于结果的断言，而没有 frozen 的定义可以拿来量。它同样不是 DAG
  mutation（不动任何 node）
- 写 roadmap（`commit_roadmap_phase`）：新增 Project 时它是第一个规划动作
- 创建/取消/替换 node
- 修改 Execution Contract
- 正式改变 research route
- 处理 worker deviation
- 记录 ending（`conclude_project`）：Project 停下之后，四个 ending 里是哪
  一个，是 Master 的科学判断。循环问"该结束了吗"问的是"已经没有能跑的
  node 了"，不是"这个 Project 成功了"；SUCCESS/FAILED/INCONCLUSIVE 三个
  在还有未结束的工作、还有没人回答的 deviation、或还没有 Success Contract
  时都会被拒，TERMINATED 随时可以，并且是唯一会取消在跑 node 的 ending
- 在 Authority Envelope 内自主决策
- 产生 Decision Record

规划分两层，Master 的两层都写：roadmap 说明 Project 经过哪些 stage，
DAG 说明当前 stage 具体由什么组成。node 必须落在某个 stage 上，
而 stage 必须先存在——所以空 Project 上没有任何工具可以绕过
`commit_roadmap_phase` 直接把 node 写进 DAG。详见 `docs/03` §3。

### 读回研究结果
Master 看得到 Research 交付的东西，但只经由四个只读工具——全部是 Master
的，且都不在 `registry.WRITE_TOOLS` 里：`list_research_results`（哪些
research task 交出了结果、判成什么）、`read_research_result`（一条 task 的
record、它依据的 claims 与 sources、记下的冲突、按 ledger 实时算出的
sufficiency、以及 Review 的判词）、`read_evidence`、`read_source_metadata`
（source 的 ledger 行，不含正文）。`read_project_state` 里的
`completed_research` 是入口而不是内容：只列已经交出的 task，摘要里没有任
何 claim 原文。Master 没有写 ledger 的工具——需要一条 Research 没记下的
claim，答案是加一个 research task，不是补一行。详见 `docs/05` §13。

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

一个 research task 的开始与结束都经由它自己的工具，和 Worker 席位同构：

```text
begin_research            READY → RUNNING，任务进入这个 session 手里。
                          DAG 决定能不能跑（READY + Execution Contract 已绑定），
                          被拒时返回原因而不是另找一条路。

submit_research_record    写 Research Record，并在同一事务里把节点
                          RUNNING → REVIEWING —— 交给 FINAL checkpoint。
                          INCOMPLETE 也照样交出：证据够不够是 Review 的问题，
                          不是 agent 自己的判断。
```

Research 不启动任何 run：没有 Temporal workflow，没有 Execution Record，
没有 BackendJob。它的产出就是它读到的证据和写下的 record。

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

### Inputs
判决只能来自它读到的东西。Review 只有三个工具（队列、package、提交），
`read_review_package` 是它唯一的逐节点视图——所以判据里没有的东西，
等于不存在。

一个节点欠什么由它的类型决定：COMPUTATION / EXPERIMENT 欠 Execution Record
与 artifacts，RESEARCH 两者都不欠（它的工作是自己去读，不跑任何东西、
不产出 artifact），它的交付是 Research Record 和那条 record 所依据的
claims 与 sources，在 package 的 `research` 下。把空字段读成"没有交付"
是这里唯一的错法，而它发生过一次：一个已交付的 research 任务被判为
non-delivery，判词逐字复述了四个空字段。

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

## 8. 三个不能混为一谈的名字

Phase 10 把真实 Agent 放进两个 Worker 席位之后，三者更容易被读成一件事。它们是三件事：

```text
Worker Agent              （Compute Worker Agent / Experimental Worker Agent）
                          五类 Agent 中的两个。持有该角色工具的 DSH session，
                          按冻结的 Execution Contract 执行，不做科研决策，
                          不能改 DAG、不能改 Acceptance Criteria。

Temporal Execution Worker 托管 Temporal activity 的进程
                          （scripts/run_temporal_worker.py，`make worker`）。
                          基础设施，不是 Agent，不属于五类，没有科研权限。
                          Phase 10 之前它叫 run_worker.py，那个名字正是
                          这里要消除的歧义。

Backend                  真正干活的东西（WorkBackend 的 implementor）。
                          默认部署是 MockComputeBackend 与 MockLabBackend；
                          compute 侧另有真实的 SlurmComputeBackend
                          （见 KNOWN_LIMITATIONS.md L-22）。
```

三者的权限关系是单向的：`Backend` 报回状态，`Temporal Execution Worker` 把它落成 durable 记录，`Worker Agent` 在这些记录之上按 contract 行动。任何一个环节都不能替另一个做决定。

Phase 10 之后还要补一句：Research 也是执行席位，但它不属于上面任何一行。它既不是 Worker Agent（没有 Execution Contract 之下的执行行为，也不碰 backend），也不是基础设施——它是五类 Agent 之一，只是它"执行"的方式是自己去读，见 §2。所以描述席位时说的是"三个执行席位：两个 Worker 加 Research"，而不是"两个 Worker"。
