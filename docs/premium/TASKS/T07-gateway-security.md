# T07 — 云端 Gateway 与来源/上传边界

任务通道：SECURITY；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03；必须全部DONE后READY。分支：`cc/T07-gateway-security`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/gateway/auth/**
- src/ravel/gateway/routes/artifacts.py
- src/ravel/research/addressing.py
- src/ravel/research/fetching.py
- tests/premium/T07/**
- gateway/app/config：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 依据现有 SECURITY_NOTES/KNOWN_LIMITATIONS 建立逐项处置表；本任务必须覆盖凭据、跨项目访问、源抓取 SSRF、上传内存上限和公开入口。
2. 生产启动拒绝开发 JWT/数据库/存储凭据；默认服务仅私网/loopback；配置 HTTPS 反代和 WebSocket 精确 Origin allowlist。登录失败按源IP+账号限流：5次/分钟，15分钟内累计20次锁该组合15分钟。
3. 撤销 membership 后所有新 API 和活动流下一次权限检查被拒绝，活动流检查间隔不超过5秒；若实现账号停用，须显式字段和审计迁移。
4. 来源网关逐跳验证重定向目标、限制私网/loopback/link-local/metadata，防止DNS重绑定；T07完成HTTP连接钉住与浏览器fail-closed入口：T19沙箱未绑定之前拒绝浏览器抓取，只保留受控真实HTTP/API研究；不在宿主无隔离启动Playwright。T19完成受限浏览器沙箱联调；科学站点allowlist不等于允许私网目标。
5. 上传改为流式，默认每文件64MiB，超限413并回收临时对象；禁止路径穿越、压缩炸弹自动解压与凭据出现在异常/日志。

## 验收条件（全部满足）

- **T07-AC1**：跨项目读写、撤权后事件流、伪造身份均被拒绝；Owner 不能调用 DAG mutation。
- **T07-AC2**：HTTP公开URL重定向私网、DNS变化与metadata访问被真实连接地址钉住阻断，不能仅测字符串解析；未绑定合规provider的浏览器入口拒绝执行。T19再认证浏览器出站策略。
- **T07-AC3**：65MiB上传返回413且进程内存增量不随文件线性增长；失败无孤儿正式 Artifact。
- **T07-AC4**：真实研究检索仍可通过授权公网来源进行；拒绝访问有明确 ACCESS_LIMITED 等记录而非伪造内容。

## 门禁与交付

- `make premium-task TASK=T07`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 安全缺口处置表
- 权限/网络/上传验证
- 配置及部署说明

记录：`tasklogs/T07.md`、`reviews/T07/<attempt>.json`、`evidence/T07/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
