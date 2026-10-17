# T04 — Supervisor 项目租约与过期进程隔离

任务通道：RUNTIME；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03；必须全部DONE后READY。分支：`cc/T04-project-lease`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/execution/supervisor.py
- src/ravel/execution/leases.py（新）
- tests/premium/T04/**
- 租约表/migration、config：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 实现每 Project 的数据库租约、持有者 ID、单调 fencing epoch；默认 TTL=60 秒、续租=15 秒，数据库时间为准。
2. 驱动 Agent turn、DAG mutation 请求和启动 execution 的确定性入口验证租约 epoch；通过服务端 scope 传递，不由模型填写。
3. 续租失败或租约丢失立即停止新的 turn 与外部提交；已存在 Temporal/HPC 作业由新持有者查询恢复，不重复提交。
4. 把 lease 状态加入运维只读投影；正常退出条件释放，不允许旧进程释放新 epoch 的租约。

## 验收条件（全部满足）

- **T04-AC1**：两个 Supervisor 同时抢同一 Project，只有一个能调用 Agent 和提交执行。
- **T04-AC2**：模拟 TTL 过期后新 owner 获得更大 epoch；旧 owner 的写入与 start 被拒绝。
- **T04-AC3**：强停持有者后恢复项目，已提交任务未重复；数据库中断不会授予第二套本地权威。
- **T04-AC4**：不同 Project 能由不同持有者并行推进，pause/用户权限检查仍生效。

## 门禁与交付

- `make premium-task TASK=T04`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 租约实现/迁移
- 竞争与故障注入证据
- 运维状态字段

记录：`tasklogs/T04.md`、`reviews/T04/<attempt>.json`、`evidence/T04/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
