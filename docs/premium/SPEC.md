# 产品与技术范围：执行版

## 已确定的产品选择

1. 在现有RAVEL继续开发，RAVEL拥有唯一权威Project/DAG/Evidence；不Fork Rakazo替换科研内核。
2. Python3.12/FastAPI/PostgreSQL/Temporal/S3对象模式保留。DSH是唯一科研harness，保留现有已验证pin和每(project,role)进程隔离；本轮不做单Host合并、不换Pi、不实现多harness。
3. Desktop-first：Electron+React+TypeScript薄客户端，通过Gateway管理云端科研团队；TUI保留兼容。Windows x64为本机首要桌面产物，后端Ubuntu24.04 x86_64。
4. 云端目标腾讯云CVM与同地域COS；外接已有Slurm HPC，腾讯云THPC也先经其Slurm SSH路径。首版不同时实现THPC云API提交。
5. Q40=C：Docker首选；另实现operator预配专用SSH VM的可替换provider。Agent不持Docker socket/集群/云凭据。
6. 双向邮件首版一个专用IMAP TLS+SMTP TLS账号；出站默认Owner对具体Action批准并限测试/业务allowlist。腾讯云SES仅保留未来接口，避免先开发两个发送链路。
7. 计算首条科学软件路线固定现有RASPA materializer对应的气体吸附/GCMC；不虚报其他科学软件支持。
8. 科学计算由容器/HPC软件执行；真实实验室实验由获准人员按现有SOP执行，AI设计、交接、分析反馈。软件不能确认仪器停止，不能代替真人签名。
9. 科学决策、执行、Review分权，Evidence真实、可定位、可核对，simulated不得成为真实科研证据。
10. 使用通用稳定ID、版本、provenance、relations、artifact refs；POST保持独立，不在本轮集成。

## 对旧仓库规格的精确更新

本包是用户明确要求的高端版扩展，因此对CLAUDE.md、START_PROMPT.md、docs/00/08/11/15中V0限制做以下更新：TUI-only/无Web前端改为新增Electron客户端及其共享Web renderer；mock执行足够完成V0改为内部测试允许模拟、最终认证要求真实HPC与真人；仅V0阶段流程改为本包T00–T29；开发当天必须重新选择DSH改为保留当前pin，只有遇到实际阻塞才另立兼容升级任务。更新均在T00/T03文档commit中写明，不让开发者在冲突规则间猜测。

其余权限、科学角色、DB权威、Temporal职责、真实研究、DSH-only要求保留。若旧设计“一Host”与现有实现冲突，采用已有IMPLEMENTATION_DEVIATIONS D-001的每(project,role)运行时，不在本轮修复成共享Host。

## 本轮排除项

移动端、语音、机器人/仪器自动操作、LIMS、通用GUI电脑控制、多harness、任意第三方插件市场、Kubernetes、Kafka、Neo4j、多地域HA、计费SaaS、自动发表论文/专利不属于本轮交付。沙箱用于受控代码/研究浏览器，不扩成远程桌面产品。涉及这些扩展须新建任务和ADR，不能挤占本包AC。

## 外部授权与资源

允许直接完成本地源码、分支、commit、merge、依赖安装到项目环境、隔离测试和内部构建。真实CVM部署、付费资源创建、DNS修改、真实邮件收件人、HPC allocation和真人实验须有明确目标、预算与operator授权记录。T24–T29可利用已存在授权；没有时记录缺口，禁止把缺失授权当已有。

## 实施前事实

本地main=79db86e（Phase10），远端候选c9815f1（Phase11）。两个提交图的祖先关系已在只读研究副本核对。本地仓库不会被强制同步或重置到远端；T02通过任务分支审查引入现有实现，并重新运行本轮验证。

Rakazo、Pi、DSH源码调查是选型证据，不是项目的新依赖授权。若复制Rakazo源码，须逐文件记录来源commit、Apache-2.0许可、依赖和实际测试；本轮默认重用设计边界自行实现，不直接整体引入其monorepo。

## T03 接口冻结（2026-10-17）

T03 将本文件的产品范围冻结为 v1 接口基线：`docs/premium/CONTRACTS.md` 增加字段映射、API/Event 示例和错误码；`schemas/premium/**` 定义 Lease、Action、Approval、BudgetReservation、Mailbox、MailMessage、SandboxHandle、ProjectEvent、HpcTarget、CalculationManifest、HumanLabEvent、ScientificReport；`docs/premium/acceptance-map.json` 为 T04–T29 的 102 个 AC 写入唯一 ID、类型和预期证据；`docs/premium/ADR-001.md` 记录范围、权限和实现边界。T03 不改变五类产品角色、DSH-only、真实研究证据、Master 唯一 DAG/Contract 决策权或 PostgreSQL 权威状态。
