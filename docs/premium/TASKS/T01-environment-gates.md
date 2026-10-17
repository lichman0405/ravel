# T01 — Linux 环境、测试隔离及证据执行入口

任务通道：PLATFORM；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T00；必须全部DONE后READY。分支：`cc/T01-environment-gates`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- scripts/premium_task.py（新）
- scripts/premium_preflight.py（新）
- tests/premium/T01/**（新）
- Makefile、pyproject.toml、测试配置：共享文件锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 后端标准环境固定 Ubuntu 24.04 x86_64 / Python 3.12 / Docker Engine+Compose v2。优先使用已有 WSL2 Ubuntu 或已有获准 Linux 主机；不自动安装操作系统、重启或重装 Docker。Windows 只承担桌面构建与 Git。
2. 记录后端路径与 Windows 仓库之间的映射；全部实现必须回到同一本地 Git 提交图。若后端在 Linux 镜像工作区中，必须记录所检出 SHA 和回传分支，禁止 rsync 未提交产品代码冒充已验证提交。
3. 实现 make premium-preflight、make premium-source-digest 和 make premium-task TASK=Txx。后者读取 acceptance-map.json 的底层execution_commands和AC映射，禁止递归调用自身；串行执行、保存退出码、日志、JUnit、被测SHA、dirty状态及环境版本；失败不能被后续命令覆盖。
4. 所有可能访问数据库的测试执行器使用同一个系统级排他锁；锁路径由数据库/端口组合哈希生成，放在共享运行目录，跨 worktree 一致。仅修改 queue 名不能替代数据库隔离；拒绝非测试数据库名。
5. 收集当前全部 pytest nodeid，建立 legacy-inventory.json：每项必须归入 internal 或 external，并列出依据、原 marker、外部资源；无法分类时失败。保留现有 suite 和 gate，不能删测试、统一 xfail 或用 -k 隐藏失败。
6. 运行已有静态检查及可用的确定性基线，真实 DSH/Internet/HPC/Lab 缺资源逐项标 BLOCKED_EXTERNAL。未运行的测试不能标 PASS。生成 baseline-results.json。

## 验收条件（全部满足）

- **T01-AC1**：缺少 Linux/Python/Docker 时 preflight 非零并精确列出依赖，不能声称后端验收通过。
- **T01-AC2**：第二个测试执行器竞争同一数据库时等待或明确拒绝，不能开始 TRUNCATE。
- **T01-AC3**：故意失败的子命令使 premium-task 非零；空测试选择、未映射 AC、无理由 skip 均失败。
- **T01-AC4**：日志没有凭据，产物包含运行时间、命令、退出码、SHA 与环境；预先收集失败亦不能得到空白 PASS。

## 门禁与交付

- `make premium-preflight`
- `.venv/bin/python -m pytest tests/premium/T01 -q`
- `make lint`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 运行入口
- legacy-inventory.json
- acceptance-map.json 初版
- baseline-results.json
- 环境映射

记录：`tasklogs/T01.md`、`reviews/T01/<attempt>.json`、`evidence/T01/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
