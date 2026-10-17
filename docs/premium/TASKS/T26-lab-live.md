# T26 — 真人实验员执行与原始测量认证

任务通道：EXTERNAL；实现者由Supervisor指派；独立审查者：Reviewer；集成者：Supervisor。

依赖：T23；必须全部DONE后READY。分支：`cc/T26-lab-live`。base SHA与worktree派发时写入PROGRESS，禁止留空开始编码。

## 写入边界

- tests/premium/live/T26/**（只验证证据和软件交接）
- docs/premium/evidence/T26/**

未列路径默认只读；共享文件必须取得FILE_LOCKS中的独占授权。发现必要额外路径先由Supervisor扩充本task白名单并记录，不得悄悄跨界。

## 执行步骤

1. 由operator指定实验负责人、获准场所/设备/既有SOP和参与人员；缺少任一条件则BLOCKED_EXTERNAL，开发Agent不能代替人执行或签名。
2. 产品Master依据真实Research生成与SOP兼容的包，负责人确认；真人在工作台签收执行并提供原始仪器文件、样品ID、时间和偏差。
3. 产品Review依据冻结标准评估，Master据结果决定下一步；软件测试只验证签收/来源/状态，不证明物理安全或测量正确。

## 验收条件（全部满足）

- **T26-AC1**：至少一个真人已实际执行的实验，人员确认与原始数据hash可追溯。
- **T26-AC2**：实验偏差或条件修改进入正式审批/contract版本链，模拟stub不被接纳为测量。
- **T26-AC3**：真实测量科学结果可为PASS/FAIL/PARTIAL；系统验收要求边界和证据正确，不要求假设必然成功。

## 门禁与交付

- `make premium-live TASK=T26`
- `实验负责人真实签收与回传证明`

上述premium入口由T01/T22实现；接管时不存在的入口不能假装运行。UI任务由T22映射到JS/Electron命令，不能用没有收集到测试的Python目录替代。Task runner运行acceptance-map的底层execution_commands，不递归调用自身。

产物：

- 真人确认记录
- 仪器原始文件索引
- Review/Decision证据

记录：`tasklogs/T26.md`、`reviews/T26/<attempt>.json`、`evidence/T26/<run-id>/manifest.json`。交接须含base/head SHA、source digest、changed files、逐AC结果、门禁和已知缺口。

## 完成与阻塞

实现/文档完成 → 必需门禁通过 → Reviewer APPROVE → 集成候选验证/复审 → Supervisor合并 → 更新记录，才是DONE。T23另要求本地main合并；T29另要求全部真实认证。

缺真实资源/授权必须BLOCKED_EXTERNAL并给具体解锁条件，不能改用mock宣布本task完成；代码实现与外部认证已分任务的，内部task只验其明确内部范围。代码缺陷、漏测或未实现不是外部阻塞。科学计算、真人实验、模拟证据必须保持不同类型。
