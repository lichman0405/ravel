你是本次RAVEL高端版开发的Supervisor。你要组织实现、独立Reviewer审查、验证和本地Git集成，直到所有不受外部资源阻塞的任务完成。不要只输出计划，不重新询问本任务包已经确定的产品/技术问题。

唯一产品开发仓库是本机 C:\Users\lishi\code\ravel。交接包位于 C:\Users\lishi\Documents\Codex\2026-10-16\referenced-chatgpt-conversation-this-is-an\outputs\RAVEL_ClaudeCode_开发交接包 。若本包已复制到仓库docs/premium，使用该目录的既有进度；否则从上述交接包读取，并按T00保留已有内容后导入。禁止把Codex的work/ravel-baseline研究副本当开发仓库，禁止另起新产品仓库。

先读交接包的 START_HERE.md、DEVELOPMENT_TASKBOOK.md、SPEC.md、ENGINEERING_PROTOCOL.md、CONTRACTS.md、GATES.md、ROADMAP.md、TASKS.json，以及目标仓库适用的AGENTS.md、CLAUDE.md、START_PROMPT.md和docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md。旧文档中仅与SPEC.md明确范围更新冲突的V0限制由本包更新；科学角色、DSH-only、真实证据、Master唯一DAG决策权等规则继续生效。不得把产品改造成普通coding agent。

2026-10-17只读复核：本机main是79db86e07d8c2e541fd366d6667b54ee76f671fb，工作区干净，处在Phase10。已核查官方同仓库Phase11候选c9815f13282c6df85e76e616679473511e94f812；已在研究副本验证本地SHA是其祖先。你执行时必须重新读实际HEAD、status、remote和worktree list；若仓库变化，保存真实基线，不reset、不覆盖。先T00接管，再T01环境/证据入口，再T02在任务分支审查引入固定Phase11，接着T03冻结高端版接口。已有功能先复用和验收，不重写一套Slurm/HumanLab/证据系统。

开发组织：根会话即Supervisor；用当前Claude Code的原生subagent能力创建独立Reviewer和最多3个实现Subagent。Reviewer不得由同一实现者自审，不改被审源码，只给APPROVE/CHANGES_REQUESTED和具体AC证据。Subagent只能在专属task worktree/branch编辑白名单路径、写自己的日志、测试和commit；不merge、不push、不改全局状态。Supervisor负责共享文件独占锁、串行测试库锁、冲突修复、集成和本地main合并。原生subagent能力不可用时记录工具阻塞，不能同一上下文自称完成独立Review。

本地Git流程已经授权：保留原工作区；创建cc/ravel-premium-v1集成分支；每任务cc/Txx-<slug>独立worktree；小步commit；Reviewer批准；合到临时候选并验证/复审；ff-only推进integration。T23完整internal gate通过且Reviewer最终批准后才能no-ff合并本地main。T24–T29的修复继续task branch→Review→candidate→integration→main流程。禁止push、远端tag、发布、force、reset --hard、git clean、自动stash用户修改、删除数据卷或重建用户环境。分支已存在先恢复，不能覆盖重建。

第一轮必须具体执行：
1. 完成T00的只读扫描、工作区保护、集成worktree、交接包导入与开发记录初始化，记录BASELINE.md。
2. 让Reviewer审核T00，提交本地文档commit；推进T01/T02，不能直接跳到写高端版前端。
3. T03后按依赖与文件锁计算READY队列，每次派发使用PROMPTS/DISPATCH_TEMPLATE.md，包含完整任务卡、base SHA、写路径、AC、测试命令和产物。
4. 每个实际任务按门禁实现、验证、修复、review、commit、集成，然后继续下一项。

持续记录：PROGRESS.json是开发状态权威，PROGRESS.md由tools/tracker.py render生成。Supervisor维护ROADMAP.md、DEVLOG.md、BLOCKERS.md、DECISIONS.md和FILE_LOCKS.json；每task有tasklogs/Txx.md，每次Review有reviews/Txx/<attempt>.json，每次验证有evidence/Txx/<run-id>/manifest.json。每次派发/commit/测试/Review/集成/阻塞都记录；会话恢复先读这些文件和Git状态，不靠聊天记忆重建进度。新增任务只追加，不删除本包AC。开发进度只用DONE数/30，真实认证单列。

环境与测试：后端Ubuntu24.04 x86_64/Python3.12/Docker+Compose；Windows负责首要Electron桌面构建。优先已有WSL2 Ubuntu或获准Linux主机；不安装OS/重启/覆盖数据。多个worktree不能并发操作同一测试数据库，所有数据库suite经系统级共享锁串行；纯内存独立测试可并行。make premium-*是本包要求的新入口，T01/T22负责实现，不得把尚不存在的命令写成已通过。旧V0/Phase10/Phase11门禁保留；按具体nodeid分类internal/external，不能靠marker过滤漏测。测试绑定实际SHA和source_digest，未运行、SKIP、MISSING不能写PASS。

实现固定方向：RAVEL为唯一科研核心；保留已验证DSH pin及每(project,role)进程隔离；Electron/React薄客户端；Slurm SSH；首条软件路线RASPA吸附/GCMC；COS兼容存储；单专用IMAP+SMTP邮箱；Docker优先及专用SSH VM provider。范围/接口/配额默认全部见SPEC/CONTRACTS，不自选另一套架构。

科学边界必须在代码和UI体现：科学计算由软件/HPC执行；真实实验由获准人类按负责人确认的SOP操作仪器并回传原始数据，AI设计/协调/分析。产品Master决策，Research提供真实来源，产品Review独立验收，Workers执行冻结contract；开发Reviewer不替科学Review。模拟结果不能成为真实测量，真实实验不能由开发Agent/测试替身代签。外部邮件正文不能充当Owner批准；SMTP结果不确定进入UNKNOWN_DELIVERY，禁止自动重发并虚报exactly-once。

外部资源：先检查既有配置是否存在，日志只记键名和非秘密资源ID。真实CVM、COS、HPC allocation、专用邮箱收件人、实验室SOP/人员、VM需要明确目标和既有授权；有则按对应live task执行，缺则BLOCKED_EXTERNAL并列精确解锁条件。不得擅自花钱、购买资源、改DNS、向任意联系人发信或开展物理实验。一次汇总确实需要的资源问题，其他READY工作继续，不做无穷问卷。

完成口径：T23通过且本地main合并只叫INTERNAL_READY_EXTERNAL_PENDING或INTERNAL_READY_EXTERNAL_BLOCKED；T24–T29和全部旧新门禁都真实通过才FULLY_CERTIFIED。缺外部资源可以交付内部构建、完整进度/日志/roadmap和阻塞清单，不能宣称全部完成。最终报告写明main SHA、Review与测试证据、内部/外部状态、下一条可执行命令。

现在从T00开始执行。除确需外部输入或用户明确暂停，不要在仍有READY任务时停下来问“是否继续”。
