# RAVEL Development Agent Rules

你正在开发 RAVEL，不是在设计另一个 coding agent。

## 不可改变的产品事实

- RAVEL 是 autonomous scientific research runtime。
- 唯一 Harness：DeepSeek Harness (DSH)。
- 云端部署：单台 CVM 的 V0 可以运行一个 DSH Host，并承载多个 Project/session。
- 5 类 Agent 固定：Master / Research / Review / Compute Worker / Experimental Worker。
- Scientific DAG 由 RAVEL 管理，强 schema，只有 Master 能 mutation。
- PostgreSQL 是 authoritative Project State。
- Temporal 只负责 durable execution。
- Web/论文/数据库 Research 必须真实，不能 mock。
- Compute/Lab V0 可以 mock。
- 本地 TUI 是用户交互入口；不做完整 Web Dashboard。
- POST 是未来独立项目，不作为依赖。

## 权限哲学

Execution / Review / Decision 三权分离。

- Worker：只执行明确 contract。
- Review：验收、诊断、建议；不能改 DAG/Contract。
- Master：唯一正式科研决策和 DAG mutation 权。
- User：可干预 Master、pause/resume、approval、authority envelope；不能直接改 DAG。

## 代码原则

- 显式状态机 > 隐式 prompt 行为
- 数据库约束 > “模型应该记得”
- schema validation > 自由文本
- immutable record > overwrite
- idempotent activity > fragile side effects
- project-scoped authorization > model-supplied project_id
- authoritative state > session context
- small interfaces > speculative abstractions

## 禁止

- 不新增 agent type 来解决确定性软件问题。
- 不让 Research/Review/Worker 直接更新 DAG。
- 不让 TUI 成为 source of truth。
- 不把 Temporal workflow 当 Scientific DAG。
- 不把 DSH session 当 Project。
- 不把 Artifact binary 塞进 PostgreSQL。
- 不使用 mock web evidence 通过 E2E。
- 不因为 DSH 是 Developer Preview 就 fork 大量 core。
- 不提前实现 POST adapter。


Claude Code：请同时严格遵守 `START_PROMPT.md` 的阶段门槛，不要把本项目重构成普通 coding-agent 项目。

## 高端版开发入口（2026-10-17）

- 当前开发由 `docs/premium/CLAUDE_CODE_START_PROMPT.md` 和 `docs/premium/DEVELOPMENT_TASKBOOK.md` 组织，任务编号 T00–T29；权威进度是 `docs/premium/PROGRESS.json`，不是聊天记忆。
- `docs/premium/SPEC.md` 对旧 V0 限制的明确更新：新增 Electron+React+TypeScript 桌面薄客户端；内部测试允许模拟执行，但最终认证要求真实 HPC 与真人实验；阶段范围改为 T00–T29；保留现有 DSH pin，只有实际阻塞才另立兼容升级任务。
- 旧规则中的五类产品 Agent、DSH-only、真实 Research 证据、Master 唯一 DAG/Contract 决策权、PostgreSQL 权威状态、Temporal 只负责 durable execution 继续生效。
- 开发 Supervisor / Reviewer / Subagent 是开发组织，不是产品内 Agent；不得把开发角色加入产品 agent model。
- 开发流程和门禁严格按 `docs/premium/ENGINEERING_PROTOCOL.md`、`docs/premium/GATES.md`；未实现或未运行的命令不得写成 PASS。
- 本地 Git 流程已授权：任务分支/worktree → 独立 Review → 集成候选 → ff-only 集成分支 → T23 才 no-ff 合并本地 main。禁止 push、force、reset --hard、git clean 或覆盖用户未提交修改。
