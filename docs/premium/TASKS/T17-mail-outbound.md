# T17 — SMTP 草稿、审批、Outbox 与发送不确定性

任务通道：MAIL；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T06, T16；必须全部DONE后READY。分支：`cc/T17-mail-outbound`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/correspondence/outbound.py（新）
- src/ravel/correspondence/service.py（新）
- tests/premium/T17/**
- Action/Outbox投影/config：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 第一版固定同一专用企业邮箱的SMTP TLS发信；腾讯云SES API只预留适配接口，不在本版增加第二个生产发送路径。固定From，收件人默认仅operator allowlist。
2. 正文、to/cc/bcc、附件hash、reply线程作为Action payload，Owner批准exact payload后由服务端发送；默认没有自动对外发信委托。
3. SMTP连接前失败可有限重试3次；一旦进入DATA发送阶段后结果不确定则UNKNOWN_DELIVERY，不自动重发。确定250接受后记ACCEPTED，不能记为对方已收到。
4. 稳定Message-ID及In-Reply-To/References，记录发送尝试与Provider响应；operator可在核对Sent/provider记录后标已接受，或显式批准新尝试。明确SMTP无法提供绝对exactly-once保证。

## 验收条件（全部满足）

- **T17-AC1**：相同Action并发只能有一个发送者；审批后修改to/cc/bcc或附件使原审批无效。
- **T17-AC2**：SMTP DATA后断线/进程强停进入UNKNOWN_DELIVERY，重启不再次send。
- **T17-AC3**：隔离SMTP真实协议服务记录收件人/正文/线程headers；未审批/越allowlist为零SMTP交付。
- **T17-AC4**：外部实时邮箱验收由T27控制；内部fixture邮件不计外部邮件已发送。

## 门禁与交付

- `make premium-task TASK=T17`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- SMTP/Outbox实现
- 发送状态机
- 故障注入证据
- 人工核对操作说明

记录：`tasklogs/T17.md`、`reviews/T17/<attempt>.json`、`evidence/T17/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
