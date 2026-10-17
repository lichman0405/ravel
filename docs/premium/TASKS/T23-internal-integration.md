# T23 — 内部全链集成、复审与本地 main 合并

任务通道：SUPERVISOR；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T04, T05, T06, T07, T08, T09, T10, T11, T12, T13, T14, T15, T16, T17, T18, T19, T20, T21, T22；必须全部DONE后READY。分支：`cc/T23-internal-integration`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- 集成分支中的必要跨模块修复
- docs/premium/{PROGRESS,DEVLOG,ROADMAP,INTERNAL_ACCEPTANCE}.*
- 所有改动仍按文件锁与Review

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 固定integration候选SHA，执行完整internal gate；场景含真实PG/Temporal/MinIO、真实Docker、协议级IMAP/SMTP测试服务、Electron，计算/实验人员替身清楚标为simulated。
2. 同时测试Project暂停/恢复、Supervisor强停、活动流断线、审批过期、Slurm响应不确定、SMTP结果不确定和成员撤权；核对Ledger与桌面报告一致。
3. Reviewer对最终candidate完整diff、迁移链、权限矩阵、依赖许可与测试证据签发APPROVE；任何集成冲突修复或后续代码变更重新审查受影响范围。
4. 按ENGINEERING_PROTOCOL本地merge --no-ff集成分支到main，工作区干净且main未被其他工作推进才执行。main变化则创建更新候选、重做受影响gate和Review，禁止强推/重置。
5. 产出INTERNAL_READY_EXTERNAL_BLOCKED或INTERNAL_READY_EXTERNAL_PENDING；外部资源齐全则继续T24–T29，不能把内部main合并叫正式认证发布。

## 验收条件（全部满足）

- **T23-AC1**：全部实现任务T00–T22已DONE且internal所有行PASS，无skip/xfail/TODO占位实现。
- **T23-AC2**：最终Reviewed code tree与合并code tree一致，合并后git diff --check和工作区检查通过。
- **T23-AC3**：本地main有可追溯merge commit和所有task/review/测试证据；没有push或真实发布。
- **T23-AC4**：外部阻塞逐项写入BLOCKERS与PROGRESS，剩余未认证能力在界面和部署文档显式标记。

## 门禁与交付

- `make premium-gate PROFILE=internal`
- `Reviewer最终集成审查`
- `git diff --check`
- `合并后源码tree核对`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- INTERNAL_ACCEPTANCE.md
- main merge SHA
- 最终Review
- 内部构建与哈希

记录：`tasklogs/T23.md`、`reviews/T23/<attempt>.json`、`evidence/T23/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
