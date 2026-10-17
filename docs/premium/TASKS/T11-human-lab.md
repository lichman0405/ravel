# T11 — 真人实验签收、偏差和原始数据链

任务通道：LAB；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T02, T06, T12；必须全部DONE后READY。分支：`cc/T11-human-lab`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/backends/lab/**
- src/ravel/gateway/routes/lab.py
- src/ravel/preparation/lab.py
- tests/premium/T11/**
- domain/state：共享锁

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 扩展已有HumanLabBackend，保存lab_user的签收/开始报告/结果交付事件，事件均为person-reported；不得把系统推测当仪器状态。
2. 实验包绑定protocol版本、sample_id、必交原始文件、设备标识和既有SOP引用；没有实验负责人确认SOP/场地/设备适用性，不把包标为可实际开做。
3. 真人通过Gateway签收、报告偏差、上传命名输出；实验条件变更回Master形成新contract并经过审批，旧版本上传保留来源但不能完成新版本run。
4. 系统取消只撤销交接和接收资格，界面显示待人工确认停止；保留迟到上传的拒绝/补充记录，不能声称停止仪器。

## 验收条件（全部满足）

- **T11-AC1**：错误角色、跨项目、错误contract版本和未要求的输出不能完成实验run。
- **T11-AC2**：偏差原文、操作者、时间、文件hash与样品ID可追踪；上传不等于科学PASS。
- **T11-AC3**：实验持续WAITING_EXTERNAL且无真人信号时不伪造RUNNING或结果；签收事件明确为人类报告。
- **T11-AC4**：真实实验验收在T26，自动化模拟人员只能证明软件交接。

## 门禁与交付

- `make premium-task TASK=T11`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 人类交接API与状态
- 样品/原始数据追溯
- 偏差回路验证

记录：`tasklogs/T11.md`、`reviews/T11/<attempt>.json`、`evidence/T11/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
