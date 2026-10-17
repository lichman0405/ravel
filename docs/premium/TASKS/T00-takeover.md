# T00 — 接管本地仓库与建立开发记录

任务通道：SUPERVISOR；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：无；必须全部DONE后READY。分支：`cc/T00-takeover`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- docs/premium/**；根 CLAUDE.md 仅追加本包入口；不改产品代码

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 目标只能是 C:/Users/lishi/code/ravel。读取 git status --porcelain=v1、branch、HEAD、remote -v、worktree list 和适用的 AGENTS.md/CLAUDE.md；不读取或输出 .env 值。
2. 核对本地 main 的已观察基线 79db86e07d8c2e541fd366d6667b54ee76f671fb；执行时若 HEAD 改变，保存实际 SHA 与差异清单，不回退到该 SHA。
3. 若工作区出现用户修改，逐文件记录，禁止 stash、覆盖或代为 commit；从当前已提交 HEAD 创建独立 worktree，保留原工作区。若修改涉及将要编辑的同一功能，记录 BLOCKED_USER_INPUT 并继续其他任务。
4. 创建本地集成分支 cc/ravel-premium-v1，保存 MAIN_BASE_SHA、ORIGINAL_BRANCH 和 INTEGRATION_WORKTREE。若同名已存在，读取既有进度并恢复，禁止覆盖重建。
5. 把本交接包复制到 docs/premium，初始化本包列出的状态文件、日志目录、任务与审查记录。已存在同名文件时保留并进行内容合并，禁止把已有进度清零。
6. 在根 CLAUDE.md 追加本包是高端版开发入口及 SPEC.md 的明确范围更新；保留原五角色、证据和 DAG 权限规则。提交 docs(T00): adopt premium development contract。

## 验收条件（全部满足）

- **T00-AC1**：真实本地路径、分支、SHA、dirty 状态及所有现有 worktree 已记录；没有触碰用户未提交文件。
- **T00-AC2**：docs/premium 下 30 个任务 ID 唯一且依赖无环，所有状态初值为 TODO；T00 自身只有经 Reviewer 审查后才进入 DONE。
- **T00-AC3**：Reviewer 能从一次启动提示定位规则、任务、roadmap、进度、日志和外部阻塞。

## 门禁与交付

- `git status --short`
- `git diff --check`
- `Reviewer 对接管清单与文档的一致性审查`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- BASELINE.md
- PROGRESS.json / PROGRESS.md
- DEVLOG.md
- FILE_LOCKS.json
- T00 commit SHA

记录：`tasklogs/T00.md`、`reviews/T00/<attempt>.json`、`evidence/T00/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
