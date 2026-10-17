# T19 — Docker 首选沙箱与受控 supervisor

任务通道：SANDBOX；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T03, T06, T07；必须全部DONE后READY。分支：`cc/T19-docker-sandbox`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/sandbox/**（新，除vm.py）
- infra/sandbox/**（新）
- tests/premium/T19/**
- 执行/路由/config：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 实现CONTRACTS的SandboxProvider；Docker调用只在独立sandbox-supervisor服务，API/DSH/Agent容器不挂Docker socket。
2. 默认一个Project一个非特权容器：read-only rootfs、非root UID、cap-drop ALL、no-new-privileges、seccomp、CPU=2、RAM=4GiB、PID=256、scratch=10GiB、单命令120秒。生产配额上限写入配置并尊重Envelope。
3. 默认禁网；Research浏览器通过显式公网代理allowlist，拦截metadata/内网，操作员单独批准目标。宿主目录仅挂载该Project工作目录，禁止host PID/network和privileged。
4. 读写文件路径以项目根为界，拒绝symlink/hardlink逃逸；产物经Gateway注册和hash验证，不能直接修改权威PG或实验结果。
5. 把已有Research Playwright接到本provider及受控代理；解除T07浏览器fail-closed仅限合规已绑定沙箱，来源网关照常登记真实抓取快照与hash。
6. 支持stop/destroy/reconnect，关闭前后资源与日志归属清楚；不在本任务加入不受控GUI电脑操作。

## 验收条件（全部满足）

- **T19-AC1**：真实Docker容器及真实沙箱浏览器验证跨Project文件、宿主secret、Docker socket、私网和metadata不可达，不能只有docker参数字符串测试；浏览器回传与来源网关注册链有内部集成验证，真实公网来源抓取另由external Research行认证。
- **T19-AC2**：CPU/RAM/PID/磁盘配额及超时确实执行；停止后进程被结束且记录可查询。
- **T19-AC3**：容器Agent不能直接连接权威DB或HPC；命令日志脱敏，重连保持正确project绑定。
- **T19-AC4**：无Docker环境时内部隔离gate为BLOCKED_EXTERNAL（resource=Linux/Docker），代码可以review但T19不标DONE。

## 门禁与交付

- `make premium-task TASK=T19`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- Docker provider
- 沙箱supervisor
- 真实容器隔离报告
- provider契约测试

记录：`tasklogs/T19.md`、`reviews/T19/<attempt>.json`、`evidence/T19/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
