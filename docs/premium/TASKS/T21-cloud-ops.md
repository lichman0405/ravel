# T21 — 腾讯云部署包、监控、备份与回滚

任务通道：OPS；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T04, T05, T07, T08, T09, T17, T19；必须全部DONE后READY。分支：`cc/T21-cloud-ops`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- infra/premium/**（新）
- scripts/premium_deploy_check.py（新）
- docs/premium/OPERATIONS.md
- tests/premium/T21/**
- 服务配置：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 提供一台Ubuntu24.04 x86_64 CVM的Compose部署包：Gateway/Supervisor/Temporal worker/DSH运行平面、PG、Temporal、COS端点和独立sandbox-supervisor；公网只HTTPS反代。
2. 隔离sandbox宿主权限域、内网DB/Temporal和邮箱/HPC凭据；镜像固定digest，秘密通过文件或既有secret机制注入，不进入镜像或Git。
3. 接入健康、Project租约、停滞、预算、Temporal丢失run、队列、邮件UNKNOWN和对象hash失败监控；活着与依赖可达分别显示。
4. 部署前备份，升级用新镜像与migration；提供已知可回退版本及不可逆migration处理。脚本默认plan/check，不自动购买资源、改DNS或部署用户机器。

## 验收条件（全部满足）

- **T21-AC1**：本地隔离Linux部署可启动并从桌面访问，端口扫描无公开PG/Temporal/sandbox API。
- **T21-AC2**：故意停Supervisor/worker并恢复，不重复外部提交；健康面板明确显示真实探测与心跳的差别。
- **T21-AC3**：恢复至独立库/bucket前缀演练通过；回滚说明列明migration兼容性。
- **T21-AC4**：腾讯云真实部署由T24，需要已提供且授权的目标；本任务不声称CVM实测。

## 门禁与交付

- `make premium-task TASK=T21`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 部署包与镜像清单
- 监控/回滚/恢复手册
- Linux隔离部署证据

记录：`tasklogs/T21.md`、`reviews/T21/<attempt>.json`、`evidence/T21/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
