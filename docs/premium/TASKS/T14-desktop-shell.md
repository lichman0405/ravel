# T14 — Electron 桌面外壳、登录与 Windows 构建

任务通道：DESKTOP；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03, T07；必须全部DONE后READY。分支：`cc/T14-desktop-shell`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- apps/desktop/**（新）
- package.json/pnpm-workspace.yaml/pnpm-lock.yaml（新，共享锁）
- tests/premium/T14/**（入口映射至桌面测试）

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. Electron+React+TypeScript；Node 22 LTS、pnpm9，初始化时记录当前选定稳定包版本并锁定lockfile，不使用浮动latest。
2. 默认连接已有RAVEL云端HTTPS；只允许operator配置的服务origin和开发loopback；不在桌面自动安装Docker或云端后端。
3. contextIsolation=true、nodeIntegration=false、sandbox=true；renderer只调用白名单IPC，外部URL经验证在系统浏览器打开。
4. 用户token存系统安全存储；不可用时只放内存，不落明文；登出清除会话。DSH/HPC/COS/邮箱服务凭据不进入renderer。
5. 生成Windows x64内部构建和Linux x64开发构建；内部未签名明确标识，外部签名发布需操作员证书。

## 验收条件（全部满足）

- **T14-AC1**：Windows真实Electron窗口可登录/退出、重启后按安全存储策略恢复；截图作为辅助证据。
- **T14-AC2**：恶意renderer/导航/IPC不能执行任意shell、读取.env或转发token给非配置origin。
- **T14-AC3**：服务离线有明确状态且可重连；关闭桌面不停止云端Supervisor。
- **T14-AC4**：打包后的产物完成安装或portable启动验证，只有浏览器截图不算Windows桌面通过。

## 门禁与交付

- `make premium-task TASK=T14`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 桌面shell
- 内部Windows构建与SHA256
- Electron安全测试
- 依赖许可清单

记录：`tasklogs/T14.md`、`reviews/T14/<attempt>.json`、`evidence/T14/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
