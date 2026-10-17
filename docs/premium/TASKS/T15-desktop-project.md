# T15 — 桌面真实项目、DAG、证据和计算视图

任务通道：DESKTOP；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T13, T14, T09, T11, T12；必须全部DONE后READY。分支：`cc/T15-desktop-project`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- apps/desktop/src/features/projects/**
- apps/desktop/src/features/evidence/**
- apps/desktop/src/features/compute/**
- tests/premium/T15/**

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 基于生成客户端实现项目列表/创建、Master聊天、时间线、只读DAG、来源证据深读、计算Job和实验任务投影。
2. DAG明确依赖/并行/join、READY/RUNNING/WAITING_EXTERNAL/Review等现有真实状态；只读视图不提供拖拽修改科学DAG。
3. 计算界面显示集群/JobID/软件/排队/资源/原始日志/Review；实验界面突出真人等待与person-reported事件；模拟结果显著标记。
4. 聊天消息由Gateway持久化，重连不会重复发送；加载/空态/错误态均对真实API实现。

## 验收条件（全部满足）

- **T15-AC1**：两项目切换与跨项目权限测试通过，界面对应PostgreSQL真实记录。
- **T15-AC2**：至少一条真实后端事件由WebSocket进入DAG/作业视图，断线恢复无丢失或重复写入。
- **T15-AC3**：证据点击能回到真实存储的source/范围/哈希，mock数据显示仅在测试fixture模式。
- **T15-AC4**：桌面创建Project、与Master沟通后能读取持久状态；live模型缺key单独阻塞，不能伪造模型回复。

## 门禁与交付

- `make premium-task TASK=T15`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 项目/证据/计算界面
- 真实后端E2E证据
- 桌面页面状态矩阵

记录：`tasklogs/T15.md`、`reviews/T15/<attempt>.json`、`evidence/T15/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
