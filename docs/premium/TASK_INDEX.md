# 任务索引

| ID | 任务 | 依赖 | 通道 |
|---|---|---|---|
| [T00](TASKS/T00-takeover.md) | 接管本地仓库与建立开发记录 | 无 | SUPERVISOR |
| [T01](TASKS/T01-environment-gates.md) | Linux 环境、测试隔离及证据执行入口 | T00 | PLATFORM |
| [T02](TASKS/T02-phase11-adoption.md) | 审查并引入已存在的 Phase 11 | T00, T01 | BASELINE |
| [T03](TASKS/T03-contracts.md) | 冻结高端版接口、范围与 AC 映射 | T02 | CONTRACT |
| [T04](TASKS/T04-project-lease.md) | Supervisor 项目租约与过期进程隔离 | T03 | RUNTIME |
| [T05](TASKS/T05-bounded-retry.md) | 持久化重问限制、冷却与停滞通知 | T04 | RUNTIME |
| [T06](TASKS/T06-actions-budget.md) | 有版本审批、执行 Action 与资源预算 | T03 | AUTHORITY |
| [T07](TASKS/T07-gateway-security.md) | 云端 Gateway 与来源/上传边界 | T03 | SECURITY |
| [T08](TASKS/T08-cos-backup.md) | COS 兼容存储与恢复演练 | T03 | STORAGE |
| [T09](TASKS/T09-hpc-targets.md) | HPC 目标配置、并行提交和幂等恢复 | T02, T04, T06 | COMPUTE |
| [T10](TASKS/T10-raspa-package.md) | 第一条科学计算路线：RASPA 输入到 Review | T02, T09, T12 | SCIENCE |
| [T11](TASKS/T11-human-lab.md) | 真人实验签收、偏差和原始数据链 | T02, T06, T12 | LAB |
| [T12](TASKS/T12-evidence-report.md) | 证据类型和可核对的科研报告 | T03 | EVIDENCE |
| [T13](TASKS/T13-api-events.md) | 桌面 API v1、事件重连与生成客户端 | T03, T06, T07 | API |
| [T14](TASKS/T14-desktop-shell.md) | Electron 桌面外壳、登录与 Windows 构建 | T03, T07 | DESKTOP |
| [T15](TASKS/T15-desktop-project.md) | 桌面真实项目、DAG、证据和计算视图 | T13, T14, T09, T11, T12 | DESKTOP |
| [T16](TASKS/T16-mail-inbound.md) | 单账号 IMAP 收件、归档与项目关联 | T06, T07, T08 | MAIL |
| [T17](TASKS/T17-mail-outbound.md) | SMTP 草稿、审批、Outbox 与发送不确定性 | T06, T16 | MAIL |
| [T18](TASKS/T18-desktop-actions.md) | 桌面审批、预算、实验工作台和双向邮件 | T15, T16, T17, T06 | DESKTOP |
| [T19](TASKS/T19-docker-sandbox.md) | Docker 首选沙箱与受控 supervisor | T03, T06, T07 | SANDBOX |
| [T20](TASKS/T20-vm-sandbox.md) | 可插拔专用 SSH VM 沙箱 | T19 | SANDBOX |
| [T21](TASKS/T21-cloud-ops.md) | 腾讯云部署包、监控、备份与回滚 | T04, T05, T07, T08, T09, T17, T19 | OPS |
| [T22](TASKS/T22-release-gates.md) | 全量验收矩阵和不可伪造的完成状态 | T01, T03 | QA |
| [T23](TASKS/T23-internal-integration.md) | 内部全链集成、复审与本地 main 合并 | T04, T05, T06, T07, T08, T09, T10, T11, T12, T13, T14, T15, T16, T17, T18, T19, T20, T21, T22 | SUPERVISOR |
| [T24](TASKS/T24-cloud-live.md) | 真实腾讯云 CVM 与 COS 验证 | T23 | EXTERNAL |
| [T25](TASKS/T25-hpc-live.md) | 真实 HPC 调度与 RASPA 计算认证 | T23 | EXTERNAL |
| [T26](TASKS/T26-lab-live.md) | 真人实验员执行与原始测量认证 | T23 | EXTERNAL |
| [T27](TASKS/T27-mail-live.md) | 真实邮箱双向回路 | T23 | EXTERNAL |
| [T28](TASKS/T28-vm-live.md) | Docker 与真实专用 VM 可替换验证 | T23 | EXTERNAL |
| [T29](TASKS/T29-certified-release.md) | 全真实科研闭环与最终交付 | T24, T25, T26, T27, T28 | SUPERVISOR |
