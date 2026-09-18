# RAVEL V0 — One-shot Development Prompt

你正在接手一个从零开始实现的项目：**RAVEL (Research Autonomous Validation & Execution Loop)**。

你的任务不是做 Demo，也不是生成架构建议，而是**自主完成 RAVEL V0 的真实工程实现，直到 `acceptance/V0_ACCEPTANCE.md` 中所有可执行验收项通过**。

## 第 0 条：先阅读，禁止直接开写

在写任何产品代码前，完整阅读：

- `README.md`
- `AGENTS.md`
- `CLAUDE.md`（即使你不是 Claude Code，也要读）
- `docs/00_PRODUCT_AND_SCOPE.md`
- `docs/01_ARCHITECTURE.md`
- `docs/02_AGENT_MODEL.md`
- `docs/03_SCIENTIFIC_DAG.md`
- `docs/04_STATE_AND_DATA.md`
- `docs/05_RESEARCH_AND_EVIDENCE.md`
- `docs/06_EXECUTION_AND_REVIEW.md`
- `docs/07_DSH_INTEGRATION.md`
- `docs/08_GATEWAY_AND_TUI.md`
- `docs/09_SECURITY_AND_IDENTITY.md`
- `docs/10_IMPLEMENTATION_PLAN.md`
- `docs/11_TECH_DECISIONS.md`
- `docs/12_POST_BOUNDARY.md`
- `docs/13_REPOSITORY_TARGET_STRUCTURE.md`
- `docs/14_DEVELOPMENT_ENVIRONMENT.md`
- `docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md`
- `schemas/*`
- `prompts/*`
- `acceptance/*`
- `references/DSH_CURRENT_STATE_2026-09-17.md`

然后输出一份简短的 `IMPLEMENTATION_PLAN.md`，明确 Phase 0–9、风险、依赖、测试门槛。不要向用户重新询问本包已经回答的问题。


## 第 0.5 条：Canonical Linux 环境

在 DSH Integration Spike 之前，先完成 Ubuntu 环境门槛：

- Ubuntu 24.04 LTS x86_64
- Python 3.12
- Docker Engine + Compose v2
- PostgreSQL / Temporal / MinIO
- Playwright Chromium
- 项目本地虚拟环境
- `scripts/bootstrap_ubuntu.sh`
- `.env.example`
- `scripts/dev_up.sh` / `scripts/dev_down.sh`
- 统一测试入口

V0 acceptance 只以 Ubuntu canonical environment 为准。

## 第 1 条：DSH 必须以开发当天真实当前版本为准

DeepSeek Harness 是唯一 Harness。

开发开始时必须：

1. 从官方仓库拉取开发当天真实当前版本。
2. 阅读官方 `README`、architecture、Python SDK、agent preset、session persistence、web capability 相关文档。
3. 选择一个实际验证可运行的 release/tag/commit。
4. 写入 `vendor/DSH_PIN.json`：
   - upstream repository
   - tag
   - commit SHA
   - SDK version
   - verified_at
   - notes
5. **Pin 之后开发，不跟随 main/master 漂移。**
6. 优先通过 DSH plugin / profile / agent preset / official SDK/API 实现；不要侵入修改 DSH core。
7. 如确实需要 patch，必须最小化，并写入 `vendor/DSH_PATCHES.md`。

**不要假设本包编写时的 DSH API 仍完全一致。必须以拉取到的当前官方版本验证。**

## 第 2 条：先做 DSH Integration Spike

在进入业务实现前，必须先证明：

- 单个 DSH Host 可以由 RAVEL Runtime 管理；
- 同一 Host 内可以创建角色不同的 session/preset；
- session 能被唯一映射到 `project_id / role / task_id`；
- 工具可以按项目/角色被严格 scope；
- session 持久化/恢复机制的真实行为已验证；
- 如果 Python SDK 的跨重启 resume 仍存在限制，不允许假装可用：
  - 保持 Master 正常情况下 persistent；
  - Project State + checkpoint 为恢复依据；
  - 使用当前 DSH 官方可用的 resume/open/replay 能力；
  - 如官方能力仍不足，重建新 Master session 并从 authoritative state 恢复，而不是依赖旧聊天上下文。

Spike 必须有自动化 integration test。

## 第 3 条：核心产品规则不可违反

### Harness
- 只支持 DSH。
- 不实现 Claude/Codex/Grok Harness adapter。
- 可以有内部 DSH boundary，但不要为不存在的多 Harness 场景过度抽象。

### Agent roles
只有五类独立 Agent：
1. Master
2. Research Agent
3. Review Agent
4. Compute Worker
5. Experimental Worker

不要新增 Planner/Memory/Router/Safety/Scheduler 等 Agent。确定性职责必须由软件组件承担。

### DAG mutation
只有 Master 能：
- 创建/取消/替换 Scientific DAG node
- 修改 DAG edge
- 修改 Execution Contract
- 做正式科研路线决策

Research / Review / Worker / 用户均不能直接修改 DAG。

### Real research
Research Agent 的 Web、论文、数据库、标准、专利检索**必须是真实的**。
- 禁止 mock web search
- 禁止伪造 DOI
- 禁止伪造网页
- 禁止模型先验冒充来源
- Search result/snippet 只能是 lead
- 必须打开原始 source 后才可注册 Evidence
- 重要 evidence 要记录 metadata/retrieved_at/hash/引用内容，许可允许时保存快照
- Browser-level research 和 structured API/database research 都要支持
- 访问受限必须明确记录 `PAYWALLED / AUTH_REQUIRED / ACCESS_LIMITED`，不得猜测补全

Unit test 可以 mock HTTP parser；**端到端 Research acceptance 不得 mock。**

### Compute / Lab
V0 可以且应该使用：
- MockComputeBackend
- MockLabBackend

但接口必须允许未来真实 Slurm/HPC、真实实验团队/LIMS/robotic lab 扩展。

### State
- PostgreSQL 是 Project/Scientific DAG authoritative source of truth
- Temporal 不是 source of truth
- DSH session 不是 source of truth
- Artifact binary 不进 PostgreSQL，进入 S3-compatible object storage
- Artifact immutable + hash + version
- Evidence 是独立一等对象

## 第 4 条：实施原则

- Python-first。
- TUI 用 Textual。
- Research Gateway 使用 Python 服务端（推荐 FastAPI + WebSocket）。
- Durable execution 使用 Temporal Python SDK。
- ORM/migration 推荐 SQLAlchemy 2 + Alembic。
- Schema 推荐 Pydantic v2。
- Browser research 推荐 Playwright。
- Object storage 采用 S3-compatible API，V0 本地/CVM 推荐 MinIO。
- 不引入 Kubernetes。
- 不引入 Neo4j。
- 不引入 Kafka。
- 不引入 Redis，除非实现过程中出现经文档证明的必要性。
- 不实现普通 Web Dashboard。
- 不依赖 POST。

## 第 5 条：工作方式

从 Phase 0 开始连续推进。每一阶段：

1. 实现
2. 单元测试
3. 集成测试
4. 运行测试
5. 修复
6. 更新 docs
7. 提交阶段性 commit（如果仓库可提交）
8. 进入下一阶段

不要在还有可执行工作的情况下停下来请求用户确认。

只有以下情况才允许请求用户：
- 需要真实私密 credential/secret，且环境中不存在；
- 需要真实外部基础设施地址/账号；
- 技术上被外部系统永久阻塞且无法通过规范允许的 Mock Backend 完成 V0。

对 Research live test，如果缺少商业搜索 API key，应优先使用无需密钥或现有可配置的真实公开 API/网页来源完成真实检索；绝不能退回 mock search 并声称验收通过。

## 第 6 条：完成定义

只有在以下条件全部满足后，才能宣布 V0 完成：

- `acceptance/V0_ACCEPTANCE.md` 的 20 项核心验收全部通过；
- live real-research acceptance 通过；
- Master kill/recovery test 通过；
- Temporal/runtime restart recovery 通过；
- parallel branch/join 通过；
- Acceptance Criteria freeze test 通过；
- 用户无法直接 mutation DAG；
- Project 可最终进入 SUCCESS / FAILED / INCONCLUSIVE；
- TUI 可完成 Owner/Lab/Admin 对应 V0 交互；
- `pytest` / integration / E2E 测试有可重复入口；
- 有一条命令或极少数命令可在单台 Linux CVM 上启动 V0；
- README 包含实际部署、配置、运行、测试说明；
- 没有把假 Research 数据伪装成真实 Evidence。

开始执行。
