# T29 — 全真实科研闭环与最终交付

任务通道：SUPERVISOR；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T24, T25, T26, T27, T28；必须全部DONE后READY。分支：`cc/T29-certified-release`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- tests/premium/live/T29/**
- docs/premium/{FINAL_REPORT,RELEASE_MANIFEST,ROADMAP,PROGRESS,BLOCKERS}.*
- 只在本地发布分支集成验证修复

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 选择一个T25支持的计算与T26获准SOP确实相关的研究问题；由Master/Research基于原始来源建立统一metric和研究路线。兼容研究问题或SOP未提供时明确BLOCKED_EXTERNAL。
2. 同一Project运行五类真实产品Agent、真实检索、真实HPC、真人实验、独立Review和Master的Decision；科学终局允许SUCCESS/FAILED/INCONCLUSIVE，不能预设成功。
3. 桌面观察全过程并完成一次真实审批/邮件回路；验证Evidence->Contract->PreparedPackage->Execution->Review->Decision链，模拟结果一律不作真实证据。
4. 运行certified gate及原有release-gate，Reviewer审最终版本与真实证据；修复走新任务分支、commit、复审、本地merge，禁止直接修改main。
5. 全部门禁通过才标FULLY_CERTIFIED并制作本地release manifest；不自动push、tag远端、发GitHub Release或公开部署。

## 验收条件（全部满足）

- **T29-AC1**：T00–T28均DONE，internal+external所有行PASS，无SKIP/BLOCKED/未映射AC。
- **T29-AC2**：同一Project所有真实链路可追溯，产品Review与开发Reviewer的记录分开保存。
- **T29-AC3**：最终main code tree有有效Review及测试证据；最终状态、日志、roadmap和版本manifest一致。
- **T29-AC4**：任何无法真实证明的环节使certified非零并保持PARTIALLY_CERTIFIED，禁止“全部完成”文案。

## 门禁与交付

- `make premium-gate PROFILE=certified`
- `make release-gate`
- `Reviewer最终认证审查`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- FINAL_REPORT.md
- RELEASE_MANIFEST.json
- 完整门禁证据
- 最终main SHA与本地构建

记录：`tasklogs/T29.md`、`reviews/T29/<attempt>.json`、`evidence/T29/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
