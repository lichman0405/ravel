# T12 — 证据类型和可核对的科研报告

任务通道：EVIDENCE；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03；必须全部DONE后READY。分支：`cc/T12-evidence-report`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/reports/**（新）
- src/ravel/research/**（限provenance读写）
- tests/premium/T12/**
- Evidence投影/schema：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 复用既有EvidenceSource/Artifact/ResearchRecord，补足FACT/INFERENCE/HYPOTHESIS、COMPUTATION/HUMAN_LAB/SIMULATED分类与外键；已存在字段不另建重复语义。
2. 报告先由确定性代码渲染任务执行次数、模拟/真实、状态、Review和证据索引，再由Master写解释；每个结论引用存在的source/record及页码或范围。
3. 发现报告陈述与Ledger不符标为REPORT_CONFLICT，禁止作为已认证报告导出；用户可查看冲突及原记录。
4. snapshot不允许存储时保留许可/来源和可访问证据定位；paywall/auth失败不能生成虚构内容。

## 验收条件（全部满足）

- **T12-AC1**：模拟产物永远不能成为真实测量或真实计算证据；修改标签不能绕过原始provenance。
- **T12-AC2**：捏造source ID、hash不符、无页码范围与错误执行次数被拒绝或显式标冲突。
- **T12-AC3**：独立Review读取的是冻结验收标准与原始产物；Master报告不覆盖Ledger事实。
- **T12-AC4**：真实Research调用在后续live gate，parser fixture测试标明非真实来源采集。

## 门禁与交付

- `make premium-task TASK=T12`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 报告生成/冲突验证
- 证据分类映射
- 可审计项目报告样例

记录：`tasklogs/T12.md`、`reviews/T12/<attempt>.json`、`evidence/T12/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
