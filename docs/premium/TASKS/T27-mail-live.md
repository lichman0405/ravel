# T27 — 真实邮箱双向回路

任务通道：EXTERNAL；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T23；必须全部DONE后READY。分支：`cc/T27-mail-live`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- tests/premium/live/T27/**
- docs/premium/evidence/T27/**

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. operator明确授权专用测试邮箱、From及允许收件人，配置IMAP/SMTP凭据和LIVE_ENABLE；禁止向任意外部联系人发送探测邮件。
2. 从获准测试对端发入一封带无害附件的邮件，RAVEL归档/关联/草拟；Owner在桌面批准后发回复，对端确认接收并再次回复。
3. 记录每跳Message-ID/References、时间、审批Action、附件hash和对端确认；测试邮件正文不得含真实敏感科研数据。

## 验收条件（全部满足）

- **T27-AC1**：IMAP入站、草稿、审批、SMTP接受、对端接收、回信关联全链真实完成。
- **T27-AC2**：重启收件不重复登记；出站状态区分ACCEPTED和对端已确认接收。
- **T27-AC3**：From伪造/正文批准不触发越权；缺授权收件人时BLOCKED_EXTERNAL。

## 门禁与交付

- `make premium-live TASK=T27`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 真实邮件线程证据
- 审批与发送日志
- 对端接收确认

记录：`tasklogs/T27.md`、`reviews/T27/<attempt>.json`、`evidence/T27/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
