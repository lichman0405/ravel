# T03 — 冻结高端版接口、范围与 AC 映射

任务通道：CONTRACT；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T02；必须全部DONE后READY。分支：`cc/T03-contracts`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- docs/premium/CONTRACTS.md
- docs/premium/SPEC.md
- schemas/premium/**（新）
- docs/premium/acceptance-map.json

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 逐项落实 CONTRACTS.md 中定义的 Lease、Action、Budget、Mailbox、Sandbox、API/Event、报告契约，不另建平行 Project/DAG/Evidence 系统。
2. 为每个新持久化实体列主键、项目外键、唯一约束、状态转换、只读/可写角色及迁移计划；复用现有 Envelope/Approval/Outbox 时列出适配映射。
3. 冻结 v1 API 的路径、请求/响应示例、鉴权、错误码、分页和重连语义；生成计划不是产品实现，但须足以让前后端独立开发。
4. 把每项 T04–T29 的 AC 映射到测试文件/场景或人工证据格式；外部项不能混入 internal PASS。

## 验收条件（全部满足）

- **T03-AC1**：Reviewer 确认接口字段、错误及状态转换完整；不存在让模型指定可信身份的参数。
- **T03-AC2**：五类产品角色没有扩充，Master 的 DAG/contract 决策权没有转移给开发 Supervisor、桌面用户或执行器。
- **T03-AC3**：T04–T29 的每项 AC 有唯一 ID、类型与预期证据，任务依赖与接口版本一致。

## 门禁与交付

- `git diff --check`
- `契约 schema 解析/引用检查`
- `Reviewer 逐 AC 审核`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- CONTRACTS.md v1
- schemas/premium
- acceptance-map.json 完整版
- ADR-001

记录：`tasklogs/T03.md`、`reviews/T03/<attempt>.json`、`evidence/T03/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
