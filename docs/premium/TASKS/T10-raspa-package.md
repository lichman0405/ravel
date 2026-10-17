# T10 — 第一条科学计算路线：RASPA 输入到 Review

任务通道：SCIENCE；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T02, T09, T12；必须全部DONE后READY。分支：`cc/T10-raspa-package`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- src/ravel/preparation/raspa.py
- src/ravel/compute/raspa/**（新）
- tests/premium/T10/**
- schemas/premium/compute/**

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 第一条支持路线固定 RASPA 的气体吸附/GCMC；不使用它替代电导率、DFT或其他未支持方法。先检查materializer语法对应的软件主版本，固定兼容版本与digest/模块标识。
2. 从官方软件实例或原始论文选一套可再分发输入，登记真实来源、结构/力场/分子定义许可；把方法、单位、温压、采样参数及seed写进冻结contract，不从模型记忆补缺。
3. 生成不可变manifest和输入包，确定 required_outputs、解析器schema、收敛/数值容差及Review标准；这些标准必须在真实run之前固定。
4. 增加软件环境探测与结果解析；软件缺失/版本不符/许可不可用拒绝start并返回Master。parser测试可用明确标示的fixture，不能登记为真实结果。

## 验收条件（全部满足）

- **T10-AC1**：缺温度/压力/力场/结构/单位时拒绝物化；路径注入与非支持方法拒绝。
- **T10-AC2**：同一contract输入包hash可复现，版本变化产生新包；解析器区分软件失败、非收敛、正常结果。
- **T10-AC3**：结果单位、输入和来源可逐项追到Ledger；Review标准没有根据结果反改。
- **T10-AC4**：真实运行另由T25验收；本任务只声明实现及确定性验证完成。

## 门禁与交付

- `make premium-task TASK=T10`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- RASPA支持矩阵
- 来源与许可记录
- 输入/结果schema
- Review案例

记录：`tasklogs/T10.md`、`reviews/T10/<attempt>.json`、`evidence/T10/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
