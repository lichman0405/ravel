# T28 — Docker 与真实专用 VM 可替换验证

任务通道：EXTERNAL；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T23；必须全部DONE后READY。分支：`cc/T28-vm-live`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- tests/premium/live/T28/**
- docs/premium/evidence/T28/**

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. operator提供获准专用VM及干净基线、网络策略、known_hosts和低权账号；不自动创建计费实例。
2. 使用相同任务包分别在真实Docker与VM执行文件/命令/停止/重连/产物回传契约；检查实际隔离与资源限制。
3. 尝试跨项目/metadata/宿主凭据访问均应拒绝；VM释放进入隔离区，operator重建后才重新登记。

## 验收条件（全部满足）

- **T28-AC1**：两个provider的同一contract套件都通过且业务代码无provider分支。
- **T28-AC2**：真实VM权限/网络/资源隔离确实生效，不能只测试SSH mock。
- **T28-AC3**：释放VM不能交给下一Project，直到有新基线确认。

## 门禁与交付

- `make premium-live TASK=T28`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- provider对照矩阵
- 真实隔离证据
- VM重建/重新登记记录

记录：`tasklogs/T28.md`、`reviews/T28/<attempt>.json`、`evidence/T28/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
