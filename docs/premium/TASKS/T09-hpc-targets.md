# T09 — HPC 目标配置、并行提交和幂等恢复

任务通道：COMPUTE；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T02, T04, T06；必须全部DONE后READY。分支：`cc/T09-hpc-targets`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/backends/slurm/**
- src/ravel/execution/backends.py
- scripts/run_temporal_worker.py
- tests/premium/T09/**
- config/schema：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 扩展已有 SlurmComputeBackend 以 target_id 选择固定白名单集群，目标包含 host/known_hosts/user/partition/account/workdir/software_catalog；凭据只在执行进程解析。
2. 严格主机密钥验证，SSH失败不得自动接受未知主机；上传路径和远端文件类型检验，拒绝符号链接与目录越界。
3. 同一输入的 project/node/contract_version/attempt 构成稳定提交键；先查submission record/队列/accounting再提交，提交响应丢失进入核对，不盲发第二次sbatch。
4. 并行只放行 DAG READY 且依赖/Review/预算允许的节点，绑定 T06 配额；不因新增 Agent 放大集群资源。
5. 收集 JobID、调度状态、ExitCode、Elapsed、AllocTRES/可得计量、原始日志及manifest。站点不提供计量时标UNKNOWN。

## 验收条件（全部满足）

- **T09-AC1**：两个相同提交并发、响应丢失和worker重启均不产生可观测重复Job；无法确认时状态UNKNOWN且禁止自动重提。
- **T09-AC2**：两条独立计算在quota=2时并行，第三条排队，join必须等所需两条完成；quota=1时串行。
- **T09-AC3**：取消仅针对相同project/target的job；known_hosts错、partition拒绝、sacct延迟各有明确状态。
- **T09-AC4**：凭据未进入模型、manifest、错误和前端；此处使用scripted scheduler的内部测试标明非真实HPC。

## 门禁与交付

- `make premium-task TASK=T09`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 多目标Slurm配置
- 并发/恢复测试
- 真实HPC检查脚本

记录：`tasklogs/T09.md`、`reviews/T09/<attempt>.json`、`evidence/T09/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
