# RAVEL 高端版开发任务书 v1

作者职责：本包给出架构、约束、接口和验收。执行者职责：Claude Code 按本包组织开发、独立审查、验证和本地Git集成。用户已授权这套本地开发分工，包括新建本地分支、worktree、commit和符合门禁的本地merge。

## 1. 目标与起点

在 `C:/Users/lishi/code/ravel` 的现有实现上开发。先审查引入已存在的Phase11，再实现云端常驻可靠性、Desktop-first、双向邮件、计算队列与预算、证据报告、Docker优先和可插拔VM。完整产品范围以 `SPEC.md` 为准。范围中每个验收项必须落到任务ID，任何功能缺失都不能靠改完成定义消失。

本次不再次确认旧40道问题；可展开原记录不完整，不能伪造逐题结果。本包落实当前明确请求和现有RAVEL产品事实，Q40=C原样执行。无法从原记录确定的配置采用本包显式默认；仅不可推断的私密资源/实验授权列为外部输入。

## 2. 开发角色与权力

| 开发角色 | 必须做 | 权限边界 |
|---|---|---|
| Supervisor | 读取本包与仓库规则；维护roadmap/progress/log/资源锁；选择READY任务；派发最多3个开发Subagent；安排Reviewer；管理共享文件与测试锁；集成、修冲突、门禁、本地main合并和报告。 | 不替科学Master决策，不自行删AC或放宽gate，不以自审代替Reviewer。只可直接写集成胶水/冲突修复/运行入口；核心功能仍交Subagent，其直接改动同样接受独立Review。 |
| Reviewer | 固定candidate commit及source digest；读任务AC和完整相关diff；查权限/证据/状态/异常/迁移/测试覆盖；独立复现必要失败；输出APPROVE或CHANGES_REQUESTED；复审修复及最终集成候选。 | 在独立只读/测试worktree，不修改被审代码，不写实现commit，不merge，不改完成标准，不自创产品需求；与产品内Review Agent完全不同。 |
| Development Subagent | 只在指派的task worktree/分支写白名单路径；实现、必要测试、记录测试与风险、小步commit；提供交接SHA及AC证据。 | 不改main/集成分支，不merge/push，不修改其他Agent目录，不自行改共享契约或全局进度，不把mock说成真实外部验证。 |
| 用户 | 决定研究/产品重大范围、明确授权费用/资源/实验场景。 | 开发团队将确需用户决定的问题聚合为一个明确问题；常规实现选择由Supervisor依据本包决定。 |

Reviewer必须是独立上下文的Claude Code子Agent，不能同一个实现者换称谓充当。角色常驻复用；开发Subagent按任务创建/复用时清楚重置任务边界。默认一个Reviewer串行审查，三个开发Subagent并行；数据库测试一份一份执行。计算资源并发和开发Agent并发是两个配置。

## 3. 两套团队不可混淆

产品内只有Master、Research、Review、Compute Worker、Experimental Worker。开发Supervisor是写软件的负责人；产品ProjectSupervisor是确定性服务；Temporal Execution Worker是基础设施。开发任务里写“Reviewer”专指代码审查者，写“产品Review”专指科学验收Agent。

只有产品Master可以请求Scientific DAG/科学Contract的正式变更。Research登记真实证据，产品Review验收，Compute Worker执行冻结计算包，Experimental Worker协调真人实验包。桌面用户提出目标、批准、暂停或设置Envelope，不直接编辑DAG。

## 4. 任务执行规则

每次派发包含：task_id、任务卡全文、base SHA、task branch/worktree、允许写路径、共享文件锁、前置版本、AC编号、准确命令、必须产物及禁止动作。用 `PROMPTS/DISPATCH_TEMPLATE.md`，禁止只发一句“做邮箱/做前端”。

依赖全部DONE且所需共享文件锁空闲才为READY。T00文档接管和T01环境建立先做；T02完整引入/验收Phase11后，T03冻结接口，之后按ROADMAP并行。准备性源码阅读/来源调查可提前做，但不能将依赖未完成的实现合并。没有Linux执行环境时先完成能证据化的接管、规格核查、资源缺口记录；若所有后续代码门禁都依赖它，则明确报告环境阻塞。

已存在的功能先检查复用，禁止为满足任务名称再建一套。能证明现有功能满足AC时，提交新增的回归证据与必要文档即可；状态仍经过Reviewer和集成门禁。失败修复由原任务作者承担。迁移、公共schema、配置和Makefile通过共享文件锁串行变更。

## 5. 必须维护的开发记录

- `PROGRESS.json` 是开发任务状态权威；`PROGRESS.md` 从它生成，Supervisor统一写。
- `ROADMAP.md` 保存波次、依赖与里程碑；顺序改变写明理由和受影响任务。
- `DEVLOG.md` 只追加：时间、角色、任务、动作、commit/证据、结果和下一步。
- `tasklogs/Txx.md` 每个任务独立开发日志，由对应作者写；Supervisor集成其记录。
- `reviews/Txx/<attempt>.json` 每次Review绑定实际候选SHA/digest与AC。
- `BLOCKERS.md` 列资源缺口、精确影响、仍可做的任务、解锁条件、负责人。
- `DECISIONS.md` 记录已确定的技术决定和后续变更依据；重大范围变更写新ADR。
- `FILE_LOCKS.json` 记录文件拥有者、任务、分支和释放条件；不是计时自动失效的锁。
- `evidence/Txx/<run-id>/manifest.json`、日志与JUnit保存实际运行证据；大二进制/秘密不入Git，以哈希和受控路径引用。

事件粒度：任务派发、每个实现commit、测试结果、Reviewer决定、修复、集成、阻塞与恢复都要更新日志；状态变更当次更新PROGRESS；每个里程碑更新ROADMAP_STATUS实际进展；只有依赖计划改变才改ROADMAP。长任务每30分钟至多一条有实质内容的checkpoint，空等不刷日志。开发进度=`DONE任务数/30`；external另列，不以代码行数或估算百分比冒充完成。

## 6. 两个完成节点

T23通过且本地main合并：实现任务T00–T22完成，状态 `INTERNAL_READY_EXTERNAL_PENDING` 或 `INTERNAL_READY_EXTERNAL_BLOCKED`。这允许用户审查可运行的内部构建。

T29通过：T00–T28全部DONE，真实腾讯云/COS、HPC计算、真人实验、邮箱和VM均有证据，全量gate与旧release-gate通过，才可标 `FULLY_CERTIFIED`。科学假设可以失败或不确定，工程链路和证据必须正确。

任务若缺外部资源：标BLOCKED_EXTERNAL，列明资源，不算DONE；所有其他READY任务继续。全体剩余任务都被同一个外部缺口阻塞时，Supervisor交出当前branch/SHA、内部状态、下一条命令与资源清单，等待输入；不得虚构完成或持续空转。
