# 外部资源与阻塞

当前还未由Claude Code执行T00，未把任何任务预先声明为已阻塞或已完成。以下仅是资源待检查表，实际缺失须创建Bxx记录，不能凭猜测标已存在。

| 资源 | 影响任务 | 接管时检查 | 缺少时的处理 |
|---|---|---|---|
| Ubuntu24.04/Python3.12/Docker | T01及后端内部gate | 已有WSL2或获准Linux主机 | 记录环境阻塞；不自动安装OS |
| 模型/真实Research接入 | 真实DSH与Research行、T29 | 现有配置键是否可用 | 代码/确定性验证继续，live不造假 |
| 已授权CVM、COS、预算 | T24 | 目标/allowlist/权限记录 | BLOCKED_EXTERNAL |
| Slurm队列、软件、allocation | T25 | target/known_hosts/软件许可 | BLOCKED_EXTERNAL |
| 真人、负责人、SOP、设备 | T26/T29 | 确认记录/工作台交接 | BLOCKED_EXTERNAL，不能替真人签名 |
| 专用邮箱和允许收件人 | T27 | IMAP/SMTP/LIVE_ENABLE及授权 | BLOCKED_EXTERNAL，不能任意探测发信 |
| 专用VM及干净基线 | T28 | VM池登记和授权 | BLOCKED_EXTERNAL |

每项实际阻塞使用TEMPLATES/blocker.md，列下一条命令与仍能做的任务。秘密不入本文件。
