# T20 — 可插拔专用 SSH VM 沙箱

任务通道：SANDBOX；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T19；必须全部DONE后READY。分支：`cc/T20-vm-sandbox`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/sandbox/vm.py（新）
- src/ravel/sandbox/vm_pool.py（新）
- tests/premium/T20/**
- operator配置/schema：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 第二provider固定为operator预配的专用Linux SSH VM；RAVEL只租用池中资源，不自动购买VM，不把HPC登录节点当通用沙箱。
2. 每VM同一时间仅一个Project，持久租约与fencing；首次分配需operator登记干净基线、known_hosts、低权账号和网络策略。
3. 使用与Docker相同的execute/files/stop/artifact契约；能力不足显式UNSUPPORTED，不做host命令fallback。
4. Project释放后进入QUARANTINED；只有operator重建/验证干净基线并重新登记才回AVAILABLE，禁止仅删除目录后自动交给其他Project。

## 验收条件（全部满足）

- **T20-AC1**：相同provider contract在Docker和scripted SSH VM适配器均通过；业务调用方没有provider专用分支。
- **T20-AC2**：并发分配只租给一Project，过期epoch不能执行，known_hosts不符拒绝。
- **T20-AC3**：释放VM不可被下一Project自动重用；HPC target配置不能当VM pool条目导入。
- **T20-AC4**：真实VM隔离由T28认证；适配器测试不能标为已运行真实VM。

## 门禁与交付

- `make premium-task TASK=T20`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- SSH VM provider
- 专用VM池状态
- 重建/隔离手册

记录：`tasklogs/T20.md`、`reviews/T20/<attempt>.json`、`evidence/T20/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
