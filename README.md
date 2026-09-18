# RAVEL V0 — Autonomous Scientific Research Runtime

**RAVEL = Research Autonomous Validation & Execution Loop**

本包是 RAVEL V0 的**产品规格 + 系统架构 + Agent 协议 + 开发验收包**。目标不是解释概念，而是让 Claude Code / Codex 在读取本包后，可以从空仓库开始自主实施并持续开发到 V0 验收通过。

## 一句话定位

RAVEL 是运行在云端 CVM 上、以 **DeepSeek Harness (DSH)** 为唯一底层 Harness 的自主科研执行 Runtime。它接收自然语言研究/产业需求，将其形式化为科学问题，进行真实网络/数据库研究，构建动态 Scientific DAG，调度计算与实验执行，独立 Review，并由 Master 在证据约束下持续重规划，直到 Project 进入 SUCCESS / FAILED / INCONCLUSIVE / TERMINATED。

RAVEL **不是代码开发 Agent**。代码、shell、HPC script 仅是科研执行手段。


## Canonical 开发环境

RAVEL V0 的唯一标准开发、验收和生产基准环境是 **Ubuntu 24.04 LTS x86_64 + Python 3.12**。

开发 Agent 必须先阅读：
- `docs/14_DEVELOPMENT_ENVIRONMENT.md`
- `docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md`

## V0 核心边界

- Harness：**DeepSeek Harness only**
- Agent types：Master / Research / Review / Compute Worker / Experimental Worker
- Master：Project 级长期角色，默认 persistent session，可 checkpoint/recover
- Scientific DAG：RAVEL 自研、强 schema、滚动展开、支持 parallel/join
- Durable execution：Temporal
- Authoritative state：PostgreSQL
- Artifact：S3-compatible object storage（V0 推荐 MinIO）
- Research：**必须真实 Web / API / Database；严禁 mock 研究证据**
- Compute：V0 使用 MockComputeBackend
- Experiment：V0 使用 MockLabBackend
- 用户交互：本地 Textual TUI → Research Gateway → Cloud RAVEL/DSH
- Web Dashboard：V0 不做
- POST：完全独立项目；V0 不依赖，不实现 POST integration
- 用户不能直接修改 DAG
- 只有 Master 能修改 DAG / Execution Contract / research route

## 开始开发

**不要从 README 自行发挥。**

1. 阅读 `START_PROMPT.md`
2. 然后按其要求读取所有 `docs/`、`schemas/`、`prompts/`、`acceptance/`
3. 首先完成 DSH Integration Spike，拉取**开发当天真实当前版本**并 pin tag/commit
4. 严格按 V0 acceptance matrix 开发到全部通过

## 目录

- `START_PROMPT.md`：唯一启动提示词
- `AGENTS.md`：Codex/通用 coding agent 强制规则
- `CLAUDE.md`：Claude Code 强制规则
- `docs/`：完整产品和技术规格
- `schemas/`：机器可读领域 schema / event / backend contract
- `prompts/`：5 类 Agent 的行为规范草案
- `acceptance/`：V0 端到端验收矩阵与 mock 场景
- `references/`：截至 2026-09-17 的 DSH 现状与官方资料指针

## Source of Truth 优先级

发生冲突时按以下优先级：

1. `docs/00_PRODUCT_AND_SCOPE.md`
2. `docs/01_ARCHITECTURE.md`
3. `docs/02_AGENT_MODEL.md`
4. `docs/03_SCIENTIFIC_DAG.md`
5. `docs/04_STATE_AND_DATA.md`
6. `docs/05_RESEARCH_AND_EVIDENCE.md`
7. `docs/06_EXECUTION_AND_REVIEW.md`
8. `docs/07_DSH_INTEGRATION.md`
9. `docs/08_GATEWAY_AND_TUI.md`
10. `docs/09_SECURITY_AND_IDENTITY.md`
11. `docs/10_IMPLEMENTATION_PLAN.md`
12. `acceptance/V0_ACCEPTANCE.md`
13. 其他文件

如果仍不明确：选择**最小实现、最强审计、最少隐式判断、最少耦合**的方案，并在 `docs/IMPLEMENTATION_DEVIATIONS.md` 记录。
