# T06 — 有版本审批、执行 Action 与资源预算

任务通道：AUTHORITY；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03；必须全部DONE后READY。分支：`cc/T06-actions-budget`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/actions/**（新）
- src/ravel/budgets/**（新）
- src/ravel/gateway/routes/control.py
- tests/premium/T06/**
- domain/state/mcp/config：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 复用现有 AuthorityEnvelope 和 Approval，通过 Action 绑定 exact payload_hash、contract_version、actor、recipient/target、budget、expires_at；默认审批有效24小时。
2. 原子执行前校验身份、当前 Envelope、租约、审批版本、有效期与额度；内容变更创建新 Action/审批。
3. 按 Project+cluster 做排队/资源预留，费用币种使用 CNY 定点数，预估与实耗分列；未知实际费用标 UNKNOWN，不能当0。
4. 默认项目并发计算2、部署全局8；测试上限1 CPU小时/作业、0 GPU、24小时等待、预算必须由 Owner 显式配置，未配置付费额度=0。参数属于配置默认，不能覆盖用户已有更严 Envelope。
5. Action 状态按 CONTRACTS：不确定的外部提交进入 UNKNOWN，资源预留保留直到核对；禁止重试造重复副作用。

## 验收条件（全部满足）

- **T06-AC1**：两个并发请求不能突破剩余额度；取消/已知拒绝释放、未知结果不释放。
- **T06-AC2**：批准后修改收件人/输入/contract/额度均导致旧审批无效；过期或撤销成员不能执行。
- **T06-AC3**：重复 Action ID 返回原状态，不能重复执行；HPC 和邮件分别使用其实际可保证的幂等策略。
- **T06-AC4**：User/Research/Review/Worker 仍不能直接改变 Scientific DAG 或冻结标准。

## 门禁与交付

- `make premium-task TASK=T06`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- Action/预算实现
- 审批 API 兼容映射
- 资源竞争与版本测试

记录：`tasklogs/T06.md`、`reviews/T06/<attempt>.json`、`evidence/T06/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
