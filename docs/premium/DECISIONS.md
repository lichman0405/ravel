# 技术决定记录

| ID | 日期 | 决定 | 依据与影响 |
|---|---|---|---|
| ADR-001 | 2026-10-17 | 基于本机既有RAVEL，先审查引入固定Phase11，再扩展高端版 | 用户明确继续code/ravel；本地Phase10与远端Phase11不同，禁止reset/重做 |
| ADR-002 | 2026-10-17 | RAVEL科研核心/DSH-only，Electron薄客户端；不整体Fork Rakazo | 保留已有科学权限与权威状态，界面独立演进 |
| ADR-003 | 2026-10-17 | 根Supervisor、独立Reviewer、最多3个实现Subagent | 用户指定工程组织；每task独立分支/worktree与本地Review/merge |
| ADR-004 | 2026-10-17 | 内部实现完成与全真实认证分开 | 真实集群、真人实验、邮箱、COS、VM不能靠模拟替代 |
| ADR-005 | 2026-10-17 | 首版IMAP+SMTP；不确定发送人工核对 | SMTP无绝对exactly-once保证，禁止自动重发不确定结果 |

执行时若要改范围/协议，追加ADR，列实际证据、影响任务和回归；已冻结需求不能由实现者自行删改。
