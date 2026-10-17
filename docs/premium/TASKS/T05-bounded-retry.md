# T05 — 持久化重问限制、冷却与停滞通知

任务通道：RUNTIME；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T04；必须全部DONE后READY。分支：`cc/T05-bounded-retry`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/execution/loop.py
- src/ravel/execution/supervisor.py
- src/ravel/execution/retry_policy.py（新）
- tests/premium/T05/**
- 重试状态表/config：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 对 project/问题指纹/相关状态版本持久化计数；每轮最多3次、最多3轮，轮间冷却60秒和300秒，达到9次后锁为 AWAITING_OPERATOR，Supervisor 重启不清零。
2. 默认模型 turn 超时180秒，最多2个并发模型请求/Project、部署全局最多6个；到达已有项目预算则更早停止。操作员显式重试或相关科学状态版本变化才解锁，单纯 timer 不解锁。
3. 仅产生一条 durable escalation 事件与待处理项，不每 tick 发消息；保留 Project 的科学状态，不把运行故障写成科学失败或直接 SUCCESS。
4. 有正常运行中的长时计算或等待真人实验，不累计为模型停滞。

## 验收条件（全部满足）

- **T05-AC1**：无响应问题跨10次 Supervisor 重启总模型调用仍不超过9次；无新状态时不自动恢复调用。
- **T05-AC2**：冷却期间没有 Agent 请求；人为重试记录操作者和新的 retry episode。
- **T05-AC3**：长 HPC/真人等待不被误判；通知每 episode 一条。
- **T05-AC4**：Project 终止、pause、失去租约均能停止新 turn，保留既有证据和决定。

## 门禁与交付

- `make premium-task TASK=T05`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 持久化重试策略
- 停滞投影
- 确定性时间/重启测试

记录：`tasklogs/T05.md`、`reviews/T05/<attempt>.json`、`evidence/T05/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
