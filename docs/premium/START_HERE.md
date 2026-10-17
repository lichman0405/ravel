# RAVEL Claude Code 开发交接包

本包用于在现有 `C:\Users\lishi\code\ravel` 上继续开发。打开该仓库的 Claude Code，粘贴同目录 `CLAUDE_CODE_START_PROMPT.md` 的全文。不要把只读研究副本当开发仓库，不要初始化新仓库。

交接基准（2026-10-16读取、2026-10-17复核）：本地 `main` 为 `79db86e07d8c2e541fd366d6667b54ee76f671fb`，工作区干净，属于 Phase10。已核查远端 Phase11 的固定提交 `c9815f13282c6df85e76e616679473511e94f812`；在研究副本中验证前者是后者祖先，共29个后续提交、174个差异文件。目标仓库没有被本次任务修改；此包只创建开发说明和记录模板。执行时必须重新扫描实际HEAD，不假定它仍未变化。

读取顺序：

1. `DEVELOPMENT_TASKBOOK.md`：总任务书与分工。
2. `SPEC.md`：产品范围、固定选型和旧规格的精确更新。
3. `ENGINEERING_PROTOCOL.md`：分支、worktree、commit、review、合并、记录。
4. `CONTRACTS.md`：跨任务接口与确定的默认策略。
5. `GATES.md`：验证与完成状态。
6. `ROADMAP.md`：依赖与并行顺序。
7. `TASKS/T00-takeover.md` 起的30份任务卡。

本包里的 `make premium-*` 是要求开发的新入口，初始仓库中不存在。T01负责preflight、task runner和source digest；T22负责全量gate、live入口和dry-run。没有实现或没有运行的命令不得写成已通过。

交付物规范：每项任务均有依赖、写入路径、步骤、逐条AC、门禁和产物。代码任务用真测试验证行为；文档/接管任务做文档与Git检查，不制造镜像测试。新外部资源或真人实验的证据不能由开发Agent伪造。

开始前只需提供现有仓库和本包。私密凭据只通过本机既有安全配置/密钥文件提供，不粘贴到任务日志。缺少外部资源时执行不受影响的工作，明确记录缺什么，不重新启动需求问卷。
