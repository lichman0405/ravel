# T25 — 真实 HPC 调度与 RASPA 计算认证

任务通道：EXTERNAL；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T23；必须全部DONE后READY。分支：`cc/T25-hpc-live`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- tests/premium/live/T25/**
- docs/premium/evidence/T25/**

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 先确认operator授权target/partition/account/软件许可/资源上限与known_hosts，缺项BLOCKED_EXTERNAL；使用T09/T10产物。
2. 真实小作业验证submit/query/cancel/collect、断线恢复和并发上限；随后执行一条T10已冻结标准的真实RASPA benchmark。
3. 提交前登记输入、来源、参数、环境与Acceptance；记录真实JobID、sacct及原始输出，由独立产品Review验收。

## 验收条件（全部满足）

- **T25-AC1**：排队、运行、取消与完成分别有真实调度器证据；重试未重复提交。
- **T25-AC2**：真实软件输出及数值/收敛按预先冻结标准验证；不能修改容差来追PASS。
- **T25-AC3**：实际资源与额度可追踪，不可得成本明确UNKNOWN；mock scheduler结果不能计入。

## 门禁与交付

- `make premium-live TASK=T25`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 真实JobID/manifest/输出hash
- 产品Review记录
- 资源消耗报告

记录：`tasklogs/T25.md`、`reviews/T25/<attempt>.json`、`evidence/T25/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
