# T02 — 审查并引入已存在的 Phase 11

任务通道：BASELINE；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T00, T01；必须全部DONE后READY。分支：`cc/T02-phase11-adoption`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- 上游 Phase11 的 174 个差异文件，仅在本任务分支整批接管；此任务集成前禁止其他任务写这些路径

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 候选源固定 https://github.com/lichman0405/ravel 的 c9815f13282c6df85e76e616679473511e94f812，不跟随最新 main。fetch 前检查 remote；不更改 remote URL。通过可信官方 URL 获取该对象，保存取得方式。
2. 用 git merge-base --is-ancestor 检查本地基线与候选源关系；已知观察基线 79db86e 是该源祖先。若执行时不是祖先，不盲合并，先保存分歧清单并形成单独适配决定。
3. 从集成分支创建 cc/T02-phase11-adoption，merge --no-ff 固定候选 SHA，保留上游历史；docs/premium 与 CLAUDE 范围入口冲突逐行保留双方有效要求。禁止 reset 本地 main 到远端。
4. Reviewer 先按权限、migration、Temporal 恢复、Slurm 提交幂等、实验人员回传、证据深读六类检查差异；已有代码发现缺陷就在本分支修复并追加 commit。
5. 更新测试清单覆盖 Phase11 所有新增项；运行 legacy deterministic suites 与 Phase11 内部项；真实集群和真人实验缺口保持 external，不把仓库旧报告当本轮测试。
6. 记录 imported SHA、merge SHA、差异、修复和门禁；通过后交 Supervisor 合并集成分支。

## 验收条件（全部满足）

- **T02-AC1**：现有 Phase11 的 reconcile/deepread/preparation/Slurm/HumanLab/服务管理/角色界面代码已存在于候选提交，并在本轮完成确定性验收。
- **T02-AC2**：所有 migration 在独立测试库从空库升级、从本地 Phase10 数据快照升级均成功；既有记录可读且不被篡改。
- **T02-AC3**：源码与测试明确 simulated、真实 research、真实 HPC、真人实验的不同状态；外部未验证保持 BLOCKED_EXTERNAL。
- **T02-AC4**：Reviewer 审查候选完整 diff 和集成候选树，旧 V0/Phase10 不出现未解释回归。

## 门禁与交付

- `make lint`
- `隔离锁内 .venv/bin/python -m pytest tests/unit tests/integration tests/e2e`
- `legacy inventory 中全部 internal acceptance 项`
- `make release-gate（保留原退出码和所有 external 行）`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- PHASE11_ADOPTION.md
- 迁移与回归报告
- Phase11 源码历史
- 更新的 legacy-inventory

记录：`tasklogs/T02.md`、`reviews/T02/<attempt>.json`、`evidence/T02/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
