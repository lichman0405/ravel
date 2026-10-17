# T24 — 真实腾讯云 CVM 与 COS 验证

任务通道：EXTERNAL；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T23；必须全部DONE后READY。分支：`cc/T24-cloud-live`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- tests/premium/live/T24/**
- docs/premium/evidence/T24/**
- 资源配置只记录键名/资源ID不存秘密

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 仅使用operator已提供并授权的测试CVM/COS/网络目标，要求资源白名单和费用上限；缺任一项记录BLOCKED_EXTERNAL。
2. 部署固定内部release artifact，HTTPS登录桌面；对COS做put/read/hash/不可变冲突、备份和独立前缀恢复。
3. 记录VPC/region/对象endpoint、镜像digest和实际网络访问证据；同地域内网连通由实际DNS/连接检查确认。

## 验收条件（全部满足）

- **T24-AC1**：真实CVM与COS链路全通过，公网只预期HTTPS端口。
- **T24-AC2**：恢复后的测试Project能重新读取全部引用Evidence/Artifact。
- **T24-AC3**：费用、资源ID与实际操作在operator授权范围内；无资源时非零外部阻塞而非PASS。

## 门禁与交付

- `make premium-live TASK=T24`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 签名/哈希资源证据
- COS恢复报告
- 云端操作记录

记录：`tasklogs/T24.md`、`reviews/T24/<attempt>.json`、`evidence/T24/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
