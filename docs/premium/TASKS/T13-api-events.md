# T13 — 桌面 API v1、事件重连与生成客户端

任务通道：API；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03, T06, T07；必须全部DONE后READY。分支：`cc/T13-api-events`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/gateway/routes/premium/**（新）
- src/ravel/gateway/stream.py
- packages/api-client/**（新）
- tests/premium/T13/**
- gateway/app/schema：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 发布CONTRACTS定义的/v1路由；旧TUI/API保留兼容，接口适配复用现有service层，不能直接写DAG。
2. 从FastAPI OpenAPI生成TypeScript类型和客户端；生成产物可复现，手写代码不改生成文件。
3. WebSocket使用项目内单调seq，snapshot锚点与replay一致；断线从最后已处理seq恢复，过旧cursor回复RESYNC_REQUIRED及新snapshot。
4. 所有分页有稳定cursor和limit<=100；error使用稳定code/request_id/details，无秘密；关联任务可在原项目重连读取。

## 验收条件（全部满足）

- **T13-AC1**：同一项目断线/重连没有漏失事件，重复事件按seq去重；撤权5秒内断流。
- **T13-AC2**：错误项目和过期token不能读取snapshot或重放；过旧cursor要求resync而不是空成功。
- **T13-AC3**：生成客户端与OpenAPI一致，旧TUI回归通过；API schema中无用户DAG mutation端点。
- **T13-AC4**：真实FastAPI+PostgreSQL集成测试通过，不能用静态JSON充当后端验收。

## 门禁与交付

- `make premium-task TASK=T13`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- v1 OpenAPI
- api-client包
- 事件恢复与兼容报告

记录：`tasklogs/T13.md`、`reviews/T13/<attempt>.json`、`evidence/T13/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
