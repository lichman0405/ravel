# T22 — 全量验收矩阵和不可伪造的完成状态

任务通道：QA；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T01, T03；必须全部DONE后READY。分支：`cc/T22-release-gates`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- scripts/premium_gate.py（新）
- docs/premium/acceptance-map.json
- tests/premium/T22/**
- Makefile/测试配置：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 实现make premium-gate PROFILE=internal或certified、make premium-gate-plan打印命令与覆盖、make premium-live TASK=T24等外部入口；保留make release-gate及其原CERTIFIED含义。
2. 收集全部legacy/premium测试，每条映射内部/外部并列AC与资源；新增未分类测试、空suite、missing AC、skip/xfail和非零退出都不能产生PASS。
3. internal执行全部内部rows、Python静态和迁移、TypeScript typecheck/构建、Electron安全与真实Docker测试；模型/Internet/COS/HPC/真人/真实邮箱/VM各为独立external row。
4. certified包含internal和全部external；输出commit/tree SHA、环境、版本、命令、退出码、JUnit、人工证据ID与hash，结果只用CONTRACTS定义状态。
5. 修复旧make test-integration marker少收测试问题：canonical runner按完整目录收集，比较nodeid列表，不能靠改报告数字解决。

## 验收条件（全部满足）

- **T22-AC1**：注入失败/空选择/缺映射/跳过/伪造人工文件时gate非零。
- **T22-AC2**：缺外部资源时certified非零并标PARTIALLY_CERTIFIED；internal也不得显示FULLY_CERTIFIED。
- **T22-AC3**：旧V0/Phase10/Phase11全部nodeid均入矩阵且无静默丢失。
- **T22-AC4**：所有数据库相关suite串行；日志退出码与最终状态一致。

## 门禁与交付

- `make premium-task TASK=T22`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- gate runner
- 覆盖矩阵
- 机器可读证据manifest
- runner自身失败测试

记录：`tasklogs/T22.md`、`reviews/T22/<attempt>.json`、`evidence/T22/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
