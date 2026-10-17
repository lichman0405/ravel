# 跨任务契约 v1

T03把以下语义转为项目schema与接口示例，不能另行猜测。字段可以复用现有名称，但映射必须逐项列出；不能借改名删语义。

## 1. 标识、时间与真实性

Project/node/contract/artifact/source沿用现有稳定ID。新Action/attempt/message等用UUID；版本单调整数；API时间UTC RFC3339，日志可同时显示+08:00。文件哈希为SHA256、字节长度整数；参数需单位；费用使用CNY定点decimal字符串。UNKNOWN不能转换成0、空PASS或科学失败。

证据执行类型枚举：RESEARCH_SOURCE、COMPUTATION、HUMAN_LAB、SIMULATED。Claim类型FACT、INFERENCE、HYPOTHESIS。导入历史类型字段时保存原record与新投影的对应，不回写篡改历史真实性。

## 2. 租约与权限上下文

Lease字段：project_id PK、owner_instance_id、epoch、expires_at、renewed_at。首次epoch=1；接管原子增加epoch；数据库时间比较expiry；owner+epoch匹配才续租/释放。

服务端工具调用scope包含project_id、agent_role、binding_id、owner_instance_id、lease_epoch。role和project来自可信runtime binding或Gateway Principal，不来自LLM参数。旧DSH每(project,role)进程的MCP环境可以载可信binding ID；该binding在PG中必须绑定签发时的owner和epoch，每次校验它们仍匹配当前租约。新owner接管必须撤销旧binding并建立新binding；严禁把旧binding自动升级为当前epoch，否则失效Agent会重新获得权威。Gateway用户审批读取用户membership，不能凭产品Agent租约充当人类Owner。

T04把fencing检查放在所有会产生科研/执行副作用的确定性入口，包括turn dispatch、Master mutation和execution start；仍在运行的外部作业需要核对，不能因租约接管重复执行。

## 3. Action 与审批

Action：id、project_id、kind(COMPUTE_SUBMIT/MAIL_SEND/LAB_HANDOVER/SANDBOX_EXEC)、payload_hash、payload_artifact_ref、contract_id/version、requesting_binding、target_ref、required_approval_ref、envelope_version、budget_reservation_ref、expires_at、status、created_at、attempts。

状态：DRAFT→AWAITING_APPROVAL→READY→CLAIMED→ACCEPTED→COMPLETED；不需要人工审批但符合Envelope的Action可从DRAFT校验至READY。REJECTED/EXPIRED/CANCELLED是终止状态；CLAIMED发生网络/进程不确定进入UNKNOWN，核对后进入ACCEPTED或FAILED_CONFIRMED。需要重做已终止/改变payload的动作必须新Action并引用prior_action_id。MAIL的UNKNOWN投影名UNKNOWN_DELIVERY。

Approval绑定payload_hash、contract/envelope版本、完整to/cc/bcc与附件hash、cost upper bound、expiry及人类principal。每次执行前重新鉴权；成员撤权后已批准但未执行的Action不能执行。expire默认24小时。Action唯一执行claim用数据库原子CAS，不靠进程map。Action ACCEPTED仅表示外部系统接受，不等于科学成功或邮件到达对端。

预算预留必须先于外部submit；unknown保留预留。实耗大于预估时记真实值与超额事件，暂停新提交，不改小账目。成本未知保留资源时数和UNKNOWN，不自称硬费用保证。若site只能给资源数，则强制资源时数上限并在UI说明费用未核实。既有Compute/实验入口和新增邮件/沙箱入口都必须接入同一Action/Authority检查；旧start_execution不能成为绕过审批、配额和租约的后门。T06完成既有入口接线，T09/T11/T17/T19各自验收调用链。

## 4. HPC Target 与计算包

Target：id、kind=SLURM_SSH、host/port、username、known_hosts_ref、credential_ref、partition/account、remote_root、allowed_software、max_jobs、cpu/gpu/hour_limits、enabled。敏感ref只在执行进程解析。JobRequest复用WorkBackend接口；新增target_id/manifest_ref，不把host/password拼进模型工具参数。

submission_key=hash(project_id,node_id,contract_version,attempt)。外部reference必须绑定target，不能用可猜JobID越权取消。sbatch返回不确定必须先核对远端记录、队列与accounting；无法查清为UNKNOWN。记录Slurm状态原文和规范化状态，保留ExitCode/原因。

calculation_manifest至少包含contract版本、source/evidence refs、inputs+hash、软件版本和执行环境digest/模块、参数与单位/seed、resource request、required_outputs、stop_conditions、物化器版本。RASPA具体温压、力场、结构和容差由真实Research/冻结contract确定，不由开发者硬填通用默认。

## 5. 实验人员事件

LabHandover复用Phase11模型，补充actor、protocol_version、sample_id、SOP_ref、负责人确认、event_type、reported_at、原始output ref。事件ACKNOWLEDGED/START_REPORTED/DEVIATION_REPORTED/OUTPUT_UPLOADED/STOP_CONFIRMED均明确person-reported。系统withdraw只改变交接状态，不产生STOP_CONFIRMED。实验物理执行不由Dev Agent或自动测试完成。

## 6. Mailbox、邮件与发送

Mailbox：id、owner/project授权范围、imap_host/port/TLS、smtp_host/port/TLS、username、credential_ref、fixed_from、recipient_allowlist、enabled；默认不允许明文连接或任意SMTP端点。

MailMessage：id、mailbox_id、imap_uidvalidity/uid、message_id、references/in_reply_to、thread_id、project_id(nullable)、raw_eml_artifact、attachments、content_hash、trust=EXTERNAL_UNTRUSTED、received_at。UNASSIGNED由用户绑定，绑定需项目权限并审计。MIME正文是内容，不能被解释为Owner的系统指令。

SMTP发送状态：DRAFT→APPROVAL_PENDING→APPROVED→SENDING→ACCEPTED；失败已确认可FAILED_CONFIRMED，DATA发起后任何不确定为UNKNOWN_DELIVERY。Message-ID固定并帮助核对，但不能承诺SMTP服务端据此去重。SENDING进程重启先进入UNKNOWN，人工/可靠provider核对后再决定；不会自动回队重发。对端收件确认与SMTP ACCEPTED分字段。

T16/T17内部使用真实协议测试服务；其发信只能送隔离服务。T27外部账号必须operator明确授权收件人并设置LIVE_ENABLE，不能因有SMTP凭据就任意外发。

## 7. SandboxProvider

`provision(project_id, authority, limits)->handle`；`reconnect(handle, authority)->handle`；`execute(handle, argv, cwd, env_refs, timeout, action_id)->run_ref`；`status(run_ref)`；`cancel(run_ref)`；`files.list/read/write(handle, relative_path)`；`stop(handle)`；`destroy(handle)`；`collect(run_ref)->artifact_refs`；`capabilities()`。接口需typed schema，命令优先argv；shell脚本本身是受审计输入产物而非字符串拼接。

Handle：id/provider/project_id/epoch/capabilities/state；secret不在handle。生命周期PROVISIONING→READY→STOPPED/DESTROYED/FAILED；VM池AVAILABLE→LEASED→QUARANTINED，重建确认才重新AVAILABLE。VM部署仅operator预配，host key验证，单项目独占。缺能力返回UNSUPPORTED，不偷偷降级在host执行。默认资源和网络限制见T19。

## 8. Gateway v1 API 与事件

保留已有API供TUI使用；新增/v1适配路径：

| 路径 | 方法/行为 |
|---|---|
| /v1/auth/login、refresh、logout | POST；沿用既有会话语义与membership，不让前端持harness凭据 |
| /v1/projects | GET分页、POST创建项目；创建不直接写科学DAG |
| /v1/projects/{id}/snapshot | GET；聚合状态与anchor_seq，读一致性快照 |
| /v1/projects/{id}/conversation | GET分页、POST给Master消息；client_message_id防重复 |
| /v1/projects/{id}/dag | GET只读 |
| /v1/projects/{id}/sources、/evidence、/computations | GET分页及单条详情；source读取有bounded range/page |
| /v1/projects/{id}/actions | GET；approve/reject采用POST /actions/{action_id}/approve或reject，payload_hash必填 |
| /v1/projects/{id}/authority-envelope | GET/POST新版本；复用现有授权 |
| /v1/projects/{id}/pause、resume | POST；权限和lease协调 |
| /v1/projects/{id}/lab/tasks | GET；下载包/签收/偏差/按required_output上传复用已有服务 |
| /v1/projects/{id}/mail | GET；草稿POST；approve/send仅通过Action；未知结果人工核对单独审计 |
| /v1/projects/{id}/events?after_seq=N | WebSocket；项目内有序，首次snapshot或replay |

所有列表limit默认50、最大100，稳定cursor。Action、mail、lab等单条操作都从resource id回查project，不仅相信URL。API错误统一 `{code,message,request_id,details}`；典型code=FORBIDDEN/STALE_VERSION/APPROVAL_REQUIRED/BUDGET_EXCEEDED/RESYNC_REQUIRED/UNKNOWN_EXTERNAL_RESULT/UNSUPPORTED。

Event：id、project_id、seq、type、aggregate_id/version、occurred_at、payload。seq在project内单调且与snapshot anchor一致；重连可重复返回已送事件但不能漏，客户端按seq去重。cursor超保存窗需RESYNC_REQUIRED；snapshot+replay取一致边界。权限重检<=5秒。前端失败重试不得重复创建消息或Action。

## 9. 证据与 gate 结果

任务状态只用TODO/READY/RUNNING/REVIEW/FIX_REQUIRED/INTEGRATING/DONE/BLOCKED_EXTERNAL/BLOCKED_USER_INPUT。ALREADY_SATISFIED为实现结论字段，不是绕过Review的任务状态。

gate row只用PASS/FAIL/BLOCKED_EXTERNAL/MISSING。pytest SKIP/XFAIL必须转FAIL，唯一例外是明确缺少外部资源且该行预先分类external，转BLOCKED_EXTERNAL。内部必须的Linux/Docker环境缺失即internal gate不通过。

项目工程状态：IN_PROGRESS、INTERNAL_READY_EXTERNAL_PENDING、INTERNAL_READY_EXTERNAL_BLOCKED、PARTIALLY_CERTIFIED、FULLY_CERTIFIED。旧 `make release-gate` 保留CERTIFIED/PARTIALLY_CERTIFIED/NOT_CERTIFIED和非零规则；仅全部旧新行PASS才能映射FULLY_CERTIFIED。
