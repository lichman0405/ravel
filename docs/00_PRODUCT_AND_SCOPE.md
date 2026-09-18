# 00 — Product & Scope

## 1. Product

**RAVEL — Research Autonomous Validation & Execution Loop**

RAVEL 接收非专家也可以表达的自然语言目标，例如产业需求，并自主完成：

`Intent → Scientific Translation → Research → Hypothesis → Scientific DAG → Computation/Experiment → Review → Replan → Project Outcome`

用户不需要先知道专业 benchmark。系统必须通过真实研究来源将业务意图形式化为可研究、可执行、可验收的科学问题。

## 2. V0 的产品目标

证明一个云端 autonomous research project 可以在**真实 Research + Mock Execution**条件下可靠闭环运行，且：

- 状态可审计
- 长任务可恢复
- Agent 权限边界可验证
- Research provenance 真实
- Master 能根据失败/偏差自主重规划
- 人类可以远程观察、对话、批准、暂停，但不能直接编辑 DAG

V0 **不要求产生真实有效的新材料成果**。

## 3. User capability model

用户只需要描述：
- 想解决的业务/科研问题
- 现实约束
- 最在意的目标
- 不允许发生的事
- 时间/预算/实验资源边界

系统负责：
- Scientific problem formulation
- 文献/数据库/专利/标准研究
- 生成候选 metrics
- 设置并解释 Acceptance Criteria
- 定义 provenance
- 形成 Research Contract 和 Success Contract

原则：

> Ask users about their world, not scientific parameters they may not know.

## 4. Project autonomy

采用 C 型闭环自主科研。

Human defines **Research Authority Envelope**；Master 在 Envelope 内自主：
- 追加真实 Research
- 修改计划
- 修改/替换后续 DAG
- 处理 deviation
- 重新设计计算/实验路线

越界才请求用户。

## 5. V0 deployment concept

单台云端 CVM：
- RAVEL Runtime
- 一个 DSH Host
- Temporal
- PostgreSQL
- S3-compatible object storage
- Research Gateway

用户本地：
- RAVEL Textual TUI

多个 Project 共享一个 DSH Host，但 session/workspace/tool authorization 必须按 Project 隔离。

## 6. V0 In Scope

- Project lifecycle
- Research Contract
- Project Success Contract
- Research Authority Envelope
- Dynamic typed Scientific DAG
- rolling horizon planning
- parallel branch + join
- 5 agent roles
- Decision Record
- Review Record
- Execution Contract
- Acceptance Criteria freeze + provenance
- Evidence Ledger
- true Web/API/database research
- Research Source Gateway
- Artifact registry/object storage
- Mock compute
- Mock lab
- DSH persistent Master + recovery
- Temporal durable wait/retry/resume
- local remote TUI
- Owner / Lab / Admin roles
- failure and deviation E2E

## 7. Explicit Non-goals

V0 不做：
- POST integration
- Multi-harness
- Claude/Codex/Grok runtime adapter
- Neo4j
- Kubernetes
- full Web Dashboard
- production multi-region HA
- real robotic lab
- real LIMS
- broad real HPC integration
- dozens of scientific software packages
- automated publication/patent generation
- billing
- complex enterprise RBAC
- user-editable DAG
- reinforcement learning
- scientific model training platform

## 8. Relationship to POST

RAVEL 与 POST 必须独立。

RAVEL owns:
- research execution state
- live agent runtime
- scientific execution DAG
- running compute/experiment tasks

POST future direction may own:
- research state/network/assets/version/reuse/collaboration

V0 不实现 POST schema/API。只坚持通用数据原则：stable IDs, version, provenance, relations, artifact references。
