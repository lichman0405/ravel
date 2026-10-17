# T16 — 单账号 IMAP 收件、归档与项目关联

任务通道：MAIL；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T06, T07, T08；必须全部DONE后READY。分支：`cc/T16-mail-inbound`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/correspondence/inbound.py（新）
- src/ravel/correspondence/models.py（新）
- tests/premium/T16/**
- mail表/config/路由：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 第一版固定一个operator配置的IMAP TLS邮箱；支持按UIDVALIDITY+UID保存持久cursor，Message-ID和内容hash辅助去重，UIDVALIDITY变化必须重建索引防重复。
2. 存原始.eml和附件到现有Artifact Store，解析只处理受限MIME；默认原邮件含附件总量25MiB，超过上限记录oversize并不下载执行。附件绝不自动运行/解压。
3. 线程按References/In-Reply-To与本项目thread映射关联；新外部联系人或未知项目进入UNASSIGNED，只允许已授权用户绑定，不让模型猜项目拥有者。
4. 外部正文和附件始终为不可信内容，不能变成Owner身份、审批、工具调用或科研证据；Research核验后才能登记正式Evidence。

## 验收条件（全部满足）

- **T16-AC1**：重启、重复拉取、UIDVALIDITY改变和邮件相同Message-ID但内容不同均有确定去重/冲突记录。
- **T16-AC2**：伪造From/Project-ID/“批准”正文不能授予权限或触发计算/发信。
- **T16-AC3**：隔离真实IMAP测试服务能收取MIME/附件并校验hash，不以手写JSON替代IMAP解析链。
- **T16-AC4**：25MiB上限、恶意附件名、跨项目References不能泄露或写错项目。

## 门禁与交付

- `make premium-task TASK=T16`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- IMAP适配
- 持久cursor/归档
- 不可信内容与线程测试

记录：`tasklogs/T16.md`、`reviews/T16/<attempt>.json`、`evidence/T16/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
