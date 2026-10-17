# T08 — COS 兼容存储与恢复演练

任务通道：STORAGE；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03；必须全部DONE后READY。分支：`cc/T08-cos-backup`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/state/store.py
- scripts/premium_backup.py（新）
- scripts/premium_restore.py（新）
- tests/premium/T08/**
- config/deploy 文档：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 在既有 S3ArtifactStore 上适配可配置 endpoint/region/bucket/addressing，不建立第二套 artifact 元数据。
2. 对象 immutable put/hash/重试/内容冲突语义与 MinIO 一致；浏览器只取短期受控下载，不得到 COS 账号密钥。
3. 备份包含 PostgreSQL 一致性转储、全部引用对象清单/哈希、DSH pin、软件版本及配置键名；秘密另由操作员备份，不打印。
4. 恢复仅允许明确命名的新测试库/新bucket前缀；禁止覆盖运行库，记录恢复时刻和通过的对象数。

## 验收条件（全部满足）

- **T08-AC1**：MinIO兼容集成测试通过，已有 Artifact 的哈希、大小与内容保持一致。
- **T08-AC2**：从含Project/DAG/Evidence/实验上传的数据快照恢复到独立目标，所有外键及对象引用有效。
- **T08-AC3**：缺对象、哈希错误、重复不同内容 put 均明确失败。
- **T08-AC4**：提供 COS live 检查入口；未有账号时标外部阻塞，不能以MinIO冒充COS验证。

## 门禁与交付

- `make premium-task TASK=T08`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 存储适配
- 备份/恢复脚本
- 恢复报告
- COS live preflight

记录：`tasklogs/T08.md`、`reviews/T08/<attempt>.json`、`evidence/T08/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
