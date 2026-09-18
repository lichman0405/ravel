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
