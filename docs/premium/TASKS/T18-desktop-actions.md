# T18 — 桌面审批、预算、实验工作台和双向邮件

任务通道：DESKTOP；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T15, T16, T17, T06；必须全部DONE后READY。分支：`cc/T18-desktop-actions`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- apps/desktop/src/features/approvals/**
- apps/desktop/src/features/lab/**
- apps/desktop/src/features/mail/**
- tests/premium/T18/**

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 待处理列表展示Action完整收件人、附件、成本上限、contract版本、有效期和批准/拒绝；过期/内容改变/撤权后按钮状态跟服务端。
2. Owner可在现有权限下配置预算与pause/resume；Lab用户仅看其获准项目任务，下载包、person-reported签收/偏差及原始文件上传。
3. 邮件界面显示收件、待关联、草稿、审批、ACCEPTED与UNKNOWN_DELIVERY，支持人工核对；不存在单凭邮件正文批准的快捷路径。
4. 状态跨桌面重启从Gateway恢复，不把renderer本地state当执行依据。

## 验收条件（全部满足）

- **T18-AC1**：Owner/Lab/Admin各权限对照通过，隐藏按钮以外还验证服务端拒绝。
- **T18-AC2**：邮件审批展示所有to/cc/bcc，修改后旧批准无法使用；不确定发送明确禁止一键自动重试。
- **T18-AC3**：真人实验包可下载、错版本上传拒绝、偏差记录可见且进入Master待决策。
- **T18-AC4**：真实Gateway连接E2E通过，模拟界面只用来测视觉不能通过业务gate。

## 门禁与交付

- `make premium-task TASK=T18`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 审批/实验/邮箱工作台
- 权限与版本E2E
- 桌面操作手册

记录：`tasklogs/T18.md`、`reviews/T18/<attempt>.json`、`evidence/T18/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
