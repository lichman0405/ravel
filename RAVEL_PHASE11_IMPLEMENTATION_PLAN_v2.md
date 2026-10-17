# RAVEL Phase 11 实施任务书（修订版）

**目标读者：** Claude Code / Codex / RAVEL 开发者  
**基线仓库：** `https://github.com/lichman0405/ravel`  
**文档目的：** 在不改变现有五 Agent 职责边界的前提下，把 RAVEL 从“架构真实、Research 真实、计算/实验执行后端主要为 mock 的 V0”推进到“具备真实科研执行闭环、真实 HPC、真实人工实验、可靠长期运行”的下一阶段。

---

# 0. Phase 11 的职责基线

本文件严格以当前仓库既有架构为准，不重新发明 Agent 职责。

## 0.1 Project Owner

Project Owner 是人类项目负责人。

职责：
- 用自然语言给出研究目标。
- 查看项目状态、DAG、Evidence、Decision、Review、Execution。
- 与 Master 沟通研究方向。
- 在 Authority Envelope 要求时进行人工审批。
- Pause / Resume Project。
- 管理项目成员（Phase 11 补齐）。

不负责：
- 直接修改 Scientific DAG。
- 直接执行计算或实验。
- 直接给科学结果判 PASS/FAIL。

## 0.2 Master

Master 是科研项目总负责人、规划者和唯一 Scientific DAG 决策者。

职责：
- 将 Project Owner 的自然语言目标转成 `ResearchContract`。
- 定义 `ProjectSuccessContract`。
- 设计 Roadmap。
- 设计和修改 Scientific DAG。
- 判断哪些问题需要 Research。
- 判断哪些步骤需要 Computation。
- 判断哪些步骤需要 Experiment。
- 根据 Research Evidence、Review 结果、Execution 结果进行科学决策。
- 为 COMPUTATION / EXPERIMENT node 建立 Execution Contract 和 Acceptance Contract。
- 记录所有 material research-route change 的 Decision Record。
- 根据 Review / deviation 进行 replan。
- 最终 `conclude_project`。

Master 必须遵守：
- Evidence 不得来自工作记忆。
- 缺乏可靠依据时必须创建 RESEARCH node。
- Workers 只执行，不承担科学决策。
- Review 独立验收。
- 只有 Master 可以请求 Scientific DAG mutation。

Master 不负责：
- 自己直接执行网络搜索。
- 自己直接调用 HPC。
- 自己直接做实验。
- 自己审核自己的计算/实验结果。

## 0.3 Research Agent

Research Agent 是专门的科研检索与证据构建 Agent。

职责：
- 执行 Master 创建的 RESEARCH task。
- 使用真实网络和真实数据库进行检索。
- 搜索论文、数据库、网页、软件文档、科学方法资料。
- 打开和验证原始来源。
- 建立 EvidenceSource。
- 记录 FACT / INFERENCE / HYPOTHESIS。
- 记录 conflicting evidence。
- 评估 evidence sufficiency。
- 形成 `ResearchRecord`。
- 将不足之处如实标记为 INCOMPLETE。

Research Agent 的检索范围包括但不限于：
- 研究背景与前沿。
- 候选材料。
- 实验条件。
- 计算方法。
- GCMC / MD / DFT 的文献依据。
- force field / potential / pseudopotential。
- charge method。
- 参数范围。
- 晶体结构来源。
- 软件方法文档。
- 与计算和实验执行相关的可靠参数来源。

Research Agent 不负责：
- 修改 DAG。
- 决定整个项目研究路线。
- 正式启动 computation / experiment。
- 自行把“搜索结果”变成项目决策。
- 替代 Review。

## 0.4 Compute Worker

Compute Worker 是当前架构中的**计算执行控制 Agent**。

职责：
- 读取 frozen Execution Contract。
- 检查任务是否允许开始。
- 调用 `start_execution()`。
- 查看 execution status。
- 监控 backend execution。
- 对 contract 外动作使用 `request_action()`。
- 遇到 deviation 时升级给 Master。
- 等待 durable execution 完成。
- 将 ExecutionRecord / artifacts 送入 Review 流程。

Compute Worker 不负责：
- 搜索论文、网页或数据库。
- 选择研究路线。
- 决定 GCMC / MD / DFT。
- 独立决定 scientific method。
- 独立修改 scientific parameters。
- 修改 Scientific DAG。
- 给结果判 PASS / FAIL。

**Phase 11 不改变这一职责边界。**

## 0.5 Experimental Worker

Experimental Worker 是实验执行协调 Agent。

职责：
- 读取 frozen Execution Contract。
- 发起实验 execution。
- 与实验 backend / HumanLabBackend 协作。
- 查看实验状态。
- 请求缺失信息。
- 报告 deviation。
- 接收实验结果。
- 进入 Review。

不负责：
- 自己修改实验方案。
- 改变 DAG。
- 自己决定 deviation 是否科学合理。
- 给实验结果判 PASS/FAIL。

## 0.6 Review Agent

Review Agent 是独立科研验收 Agent。

职责：
- Pre-run Review。
- Final Review。
- 针对 frozen Acceptance Criteria 逐项评价。
- 输出 PASS / FAIL / PARTIAL。
- 给出 diagnosis / recommendation。
- 检查 Evidence、ExecutionRecord、Artifacts、provenance。

不负责：
- 修改 DAG。
- 修改 Acceptance Criteria。
- 决定项目下一步。
- 替代 Master。

---

# 1. Phase 11 最终目标

完成后，RAVEL 至少应支持以下真实闭环：

```text
PROJECT OWNER
    ↓ natural-language objective
MASTER
    ↓
ResearchContract / SuccessContract / Roadmap / DAG
    ↓
RESEARCH node
    ↓
Research Agent
    ↓
Real Sources / Evidence / ResearchRecord
    ↓
MASTER reads Research result
    ↓
Scientific Decision
    ↓
COMPUTATION node
    ↓
Execution Contract
    ↓
Execution Preparation / Materialization
    ↓
Compute Preparation
    ↓
Compute Worker
    ↓
Temporal
    ↓
SlurmComputeBackend
    ↓
Real HPC
    ↓
Artifacts / ExecutionRecord
    ↓
Review
    ↓
Master

and

MASTER
    ↓
EXPERIMENT node
    ↓
Execution Contract
    ↓
Execution Preparation / Materialization
    ↓
Lab Preparation
    ↓
Experimental Worker
    ↓
HumanLabBackend
    ↓
Human Lab User
    ↓
Real Experimental Result
    ↓
Review
    ↓
Master
```

Phase 11 结束时必须满足：

1. Research Agent 能搜索并深读真实来源。
2. Master 能读取 ResearchRecord / Evidence，并基于它进行决策。
3. Execution Contract 能通过统一的 Execution Preparation / Materialization 层被转换为可执行包。
4. COMPUTATION node 能被物化为真实计算工作目录和软件输入文件。
5. EXPERIMENT node 能被物化为清晰、可执行、可审计的实验包。
6. Compute Worker 与 Experimental Worker 仍保持执行控制角色。
7. Slurm 能真实提交、查询、取消并收集任务。
8. Human Lab 能真实接收实验包并回传结果。
9. Temporal 异常不会让 node 永久卡在 RUNNING。
10. 不同人类角色有正确的 TUI。
11. Release certification 可以证明真实闭环，而不是只证明 mock backend。

---

# 2. P11-01：Temporal Execution Reconciliation

**优先级：P0，必须最先完成。**

当前风险：
- Temporal Workflow 可能已经 FAILED / LOST / TERMINATED。
- PostgreSQL node 或 backend job 仍保持 RUNNING。
- ProjectLoop 将其视为仍在执行。
- 项目永久卡死。

## 2.1 实现 Execution Reconciler

建议：

```text
ProjectSupervisor
    ↓
ExecutionReconciler
    ↓
PostgreSQL authoritative state
    ↕
Temporal workflow state
```

至少识别：

- PostgreSQL RUNNING，但 Temporal workflow 不存在。
- PostgreSQL RUNNING，但 workflow FAILED。
- PostgreSQL RUNNING，但 workflow TERMINATED。
- PostgreSQL RUNNING，但 workflow CANCELLED。
- backend terminal，但 DAG 未同步。
- Temporal completed，但 supervisor 在消费结果前崩溃。

## 2.2 Failure classification

必须区分：

- infrastructure failure
- workflow lost
- backend failure
- cancelled
- scientific result failure

不要把 infrastructure failure 自动当作 scientific failure。

## 2.3 Idempotency

Reconciler 连续扫描多次不得：

- 重复 submission。
- 重复 Decision。
- 重复 Review。
- 重复 terminal event。
- 破坏已有 ExecutionRecord。

## 2.4 Acceptance

至少覆盖：

- activity retry exhaustion 后 node 不永久 RUNNING。
- supervisor restart 后恢复。
- reconciler 连续跑两次结果一致。
- workflow 完成但 supervisor 崩溃后恢复。
- lost workflow 不污染 scientific conclusion。

---

# 3. P11-02：Research Deep Read

**优先级：P0。**

Research Agent 已经可以真实搜索，但必须进一步具备稳定的 source deep-read 能力。

## 3.1 新工具

建议增加：

```text
read_source(source_id, start, length)
search_source(source_id, query)
source_metadata(source_id)
```

PDF：

```text
extract_pdf_text(source_id)
read_pdf_pages(source_id, pages)
search_pdf(source_id, query)
```

## 3.2 格式

最低支持：

- HTML
- text/plain
- JSON
- XML
- PDF

扫描 PDF 如暂不支持 OCR，必须显式报告。

## 3.3 Bounded read

禁止一次把整篇论文无限塞入模型。

必须：
- bounded read
- page/range read
- maximum chars
- snapshot hash
- retrieved_at
- provenance linkage

## 3.4 Research scope

Research Agent 必须明确支持 computation-related research，例如：

```text
- GCMC / MD / DFT methodology
- force field
- potential
- pseudopotential
- charge method
- temperature / pressure conditions
- convergence criteria
- structure source
- crystallographic database
- software documentation
- published parameter sets
```

这些信息必须作为 Evidence，而不是直接进入 Master 的 model memory 后丢失 provenance。

## 3.5 Acceptance

Live acceptance 至少证明：

- 搜索真实论文。
- 保存 source。
- 从正文/PDF 提取摘要外具体信息。
- 形成 Evidence。
- Evidence 有 snapshot/hash。
- ResearchRecord 可提交。
- Review 可以检查 Evidence。

---

# 4. P11-03：Research → Master Result Readback

**优先级：P0。**

当前架构设计要求：

```text
Master
→ Research Agent
→ Evidence / ResearchRecord
→ Master
→ Scientific Decision
```

但 Master 必须有明确工具读取 Research Agent 的实际成果。

## 4.1 新增 Master read tools

建议至少增加：

```text
list_research_results()
read_research_result(node_id)
```

`read_research_result(node_id)` 应返回：

- Research node objective。
- ResearchRecord。
- completion status。
- claims。
- Evidence。
- supporting sources。
- conflicts。
- sufficiency。
- gaps / unresolved questions。
- provenance references。

必要时增加：

```text
read_evidence(evidence_id)
read_source_metadata(source_id)
```

Master 可以读取 Evidence 内容，但不能伪装成 Research Agent 去重新注册 Evidence。

## 4.2 Authorization

这些工具：

```text
MASTER → read only
RESEARCH → write ResearchRecord/Evidence
```

必须保持 authorship 分离。

Master 不得：
- 修改 Evidence。
- 修改 ResearchRecord。
- 把自己的 recollection 写成 Evidence。

## 4.3 DAG integration

当 RESEARCH node PASS / PARTIAL / FAIL 后，Master 下一轮必须能够看到：

```text
research result available
```

并主动读取。

Project state 可以增加 summary，例如：

```text
completed_research:
- node_id
- display_id
- review_outcome
- research_record_ref
```

但不要把所有 Evidence 内容直接塞进 `read_project_state()`。

## 4.4 Acceptance

测试：

```text
Master creates RESEARCH node
Research performs real search
Research submits ResearchRecord
Review evaluates
Master receives next turn
Master calls read_research_result
Master can reference Research evidence
Master creates downstream computation based on result
```

---

# 5. P11-04：Execution Preparation / Materialization

**优先级：P0/P1。**

这是当前真实执行链路中缺失的一层。

当前架构已经有：

```text
Master
→ COMPUTATION / EXPERIMENT node
→ Execution Contract
→ Worker
→ Backend
```

但真实世界执行还需要：

```text
Execution Contract
→ executable package
→ Worker
→ Backend
```

因此 Phase 11 必须增加统一的：

```text
ExecutionPreparation
├── ComputePreparation
└── LabPreparation
```

这不是新的 Agent，而是**确定性的执行准备 / 物化层**。

## 5.1 核心原则

职责链必须保持：

```text
Research Agent
→ 提供 Evidence

Master
→ 做科学决策
→ 冻结 Execution Contract

Execution Preparation
→ 将已决定的方案物化成可执行包

Worker
→ 发起并监控执行

Backend
→ 与真实执行环境交互
```

Preparation 层不能承担新的 scientific judgment。

它只能：
- 读取 frozen Execution Contract。
- 根据 contract 生成结构化执行包。
- 检查 completeness。
- 检查格式。
- 生成 manifest。
- 对缺失信息明确拒绝 materialization。
- 将失败原因返回给 Master / execution flow。

它不能：
- 自己选择研究方法。
- 自己补科学参数。
- 自己改变温度、压力、functional、force field、实验条件。
- 自己改变 project objective。
- 修改 DAG。

## 5.2 统一接口

建议：

```text
ExecutionPreparation
    prepare(contract, inputs)
        → PreparedExecution
```

`PreparedExecution` 至少包含：

```text
workspace_path
prepared_files
manifest
validation_report
required_outputs
execution_metadata
```

具体实现：

```text
ComputePreparation
LabPreparation
```

二者共享：
- contract version tracking
- input provenance
- file hashing
- manifest generation
- validation
- immutable prepared package
- reproducibility metadata

## 5.3 Compute Preparation

职责：

```text
Master 已决定的 computation plan
        ↓
Execution Contract
        ↓
ComputePreparation
        ↓
real compute workspace
```

### Execution Contract coverage

至少检查并补齐：
- inputs
- procedure
- parameter_targets
- allowed_ranges
- resource_limits
- required_outputs
- stop_conditions
- software / execution requirements

Master 的 DAG writing path 必须能够真正写入这些字段。

当前 `ExecutionContract` model 已有部分字段，但 Master 的 node-creation path 未完整暴露；Phase 11 必须补齐。

### Software-specific materializer

第一阶段只完整实现一个真实软件栈，例如：

```text
RaspaInputMaterializer
```

或：

```text
VaspInputMaterializer
```

RASPA 示例输出：

```text
simulation.input
force_field_mixing_rules.def
pseudo_atoms.def
molecule definitions
structure files
job.slurm
```

VASP 示例输出：

```text
INCAR
POSCAR
KPOINTS
POTCAR references
job.slurm
```

### Scientific boundary

ComputePreparation 可以：
- 把 Master 已确定的 method/parameter 写成软件输入格式。
- 检查字段完整性。
- 验证输入文件语法或静态约束。
- 生成 Slurm script。
- 生成可复现 manifest。

不能：
- 自己选择 GCMC / MD / DFT。
- 自己选择 functional。
- 自己选择 force field。
- 自己决定 temperature / pressure。
- 自己从模型记忆补参数。

缺失科学参数时：

```text
ComputePreparation refuses
→ execution cannot start
→ Master
→ optionally create RESEARCH node
```

### Compute manifest

至少：

```text
calculation_manifest.json
```

字段：
- project_id
- node_id
- contract id/version
- software
- method
- input source refs
- parameters
- generated files
- file hashes
- resource request
- expected outputs
- materializer version
- preparation timestamp

## 5.4 Lab Preparation

LabPreparation 负责将 Master 已冻结的实验方案变成**实验人员可以实际执行的实验包**。

链路：

```text
Master
→ EXPERIMENT node
→ Execution Contract
→ LabPreparation
→ prepared lab package
→ Experimental Worker
→ HumanLabBackend
→ LAB_USER
```

### Lab package

根据具体实验，至少能够生成或组织：

```text
experimental_protocol.md
sample_manifest.csv
reagent_list.csv
equipment_requirements.json
measurement_plan.json
data_collection_template.csv
acceptance_checklist.json
```

需要时还可包括：

```text
safety_notes.md
instrument_settings.json
sample_label_template.csv
```

是否生成 `safety_notes.md` 取决于 Execution Contract 和已有安全信息；Preparation 不得自行发明危险性或操作要求。

### LabPreparation 应物化的信息

例如：
- 样品 identity / sample ID。
- 样品数量。
- reagent / material requirements。
- 已确定的用量、浓度。
- equipment requirements。
- temperature / pressure / time / rate。
- repetition count。
- control groups。
- measurement points。
- required raw data。
- data recording format。
- required outputs。
- stop conditions。
- allowed ranges。
- deviation reporting conditions。

### Scientific boundary

LabPreparation 可以：
- 将 Master 已确定的实验 procedure 转成标准化 protocol。
- 生成样品和数据记录模板。
- 生成 checklist。
- 检查是否缺少必要执行信息。
- 将 required outputs 映射到 Lab User 上传要求。

不能：
- 改实验温度。
- 改 reagent。
- 改浓度。
- 改设备方法。
- 自己增加/删除 control group。
- 自己改变重复次数。
- 自己把 contract 外偏差判定为可接受。

如果实验包无法物化：

```text
LabPreparation refuses
→ Experimental execution not started
→ Master
→ optionally commission more Research / revise plan
```

### Lab manifest

建议：

```text
lab_manifest.json
```

至少：
- project_id
- node_id
- contract id/version
- sample identifiers
- protocol files
- reagent/equipment requirements
- measurement requirements
- expected outputs
- file hashes
- preparation timestamp
- preparation version
- source/evidence references where applicable

## 5.5 Preparation provenance

Compute 与 Lab 必须统一：

```text
Execution Contract version
        ↓
PreparedExecution manifest
        ↓
Worker execution
        ↓
ExecutionRecord
        ↓
Review
```

Review 必须能够判断：

> 实际执行的包是不是由被批准的 frozen contract 物化出来的。

不得允许：

```text
contract v1
→ preparation
→ contract v2
→ 继续执行旧 package
```

而不显式记录版本差异。

## 5.6 Acceptance

### Compute Preparation

- Master 写完整 computation contract。
- Materializer 生成真实软件输入文件。
- required files 存在。
- file hashes 被记录。
- 缺关键科学参数时明确失败。
- 不允许 materializer 自己猜 scientific parameter。
- manifest 可进入 Review。

### Lab Preparation

- Master 写完整 experiment contract。
- LabPreparation 生成结构化实验包。
- Lab User 能直接理解需要执行什么、记录什么、上传什么。
- 缺关键实验条件时明确失败。
- Preparation 不自行补 scientific parameter。
- protocol / templates / manifest 都有 hash。
- prepared package 与 contract version 绑定。
- Experimental Worker 只能执行对应 prepared package。

# 6. P11-05：SlurmComputeBackend

**优先级：P1。**

Slurm 负责真实执行，不负责决定 scientific method。

## 6.1 连接方式

第一版：

```text
RAVEL Server
    ↓ SSH
Slurm submission host
    ↓
sbatch
```

配置来自环境变量：

```text
RAVEL_SLURM_HOST
RAVEL_SLURM_PORT
RAVEL_SLURM_USERNAME
RAVEL_SLURM_PASSWORD
```

密码绝不能暴露给 LLM。

## 6.2 Backend operations

至少：

```text
submit()
status()
collect()
cancel()
```

映射：

```text
submit  → sbatch
status  → squeue + sacct
cancel  → scancel
collect → remote workspace / logs / outputs
```

## 6.3 Idempotency

强制：

```text
(project_id, node_id, attempt)
```

必须是 submission idempotency key。

Temporal retry 不得产生重复 Slurm jobs。

## 6.4 Remote workspace

建议：

```text
/ravel/jobs/<project_id>/<node_id>/<attempt>/
```

至少：

```text
input/
job.slurm
stdout
stderr
outputs/
calculation_manifest.json
```

## 6.5 Slurm state mapping

至少覆盖：

```text
PENDING
RUNNING
COMPLETED
FAILED
CANCELLED
TIMEOUT
OUT_OF_MEMORY
NODE_FAIL
PREEMPTED
```

并映射到 RAVEL JobState / FailureClass。

## 6.6 Credential boundary

LLM/Agent 不得：

```text
read password
print password
retrieve password
write password into artifact
```

只有 backend process 读取 credential。

## 6.7 Acceptance

- 上传 prepared workspace。
- 提交真实 job。
- 获取真实 job id。
- squeue/sacct 同步到 RAVEL。
- 完成后拉回 logs/output。
- retry 不重复提交。
- cancel 正常工作。
- Slurm 不可达 → infrastructure failure。
- real artifact 不标 simulated。

---

# 7. P11-06：HumanLabBackend

**优先级：P1。**

第一版只实现真实人工实验闭环，不做机器人实验室。

## 7.1 Flow

```text
Master
→ EXPERIMENT node
→ Frozen Execution Contract
→ LabPreparation
→ Prepared Lab Package
→ Experimental Worker
→ HumanLabBackend
→ LAB_USER
→ real experiment
→ upload result
→ Review
→ Master
```

## 7.2 Node status

人工等待应该是：

```text
WAITING_EXTERNAL
```

不要伪装成长期 RUNNING。

## 7.3 Lab task content

Lab User 至少看到：

- objective
- prepared experimental protocol
- sample manifest
- reagent / equipment requirements
- measurement plan
- allowed parameter ranges
- required outputs
- stop conditions
- contract version
- prepared package version / manifest
- status
- deviations
- messages

## 7.4 Upload provenance

结果绑定：

```text
project_id
node_id
contract version
uploaded_by
timestamp
media_type
hash
```

## 7.5 Required outputs

不能因为上传任意一个文件就自动完成。

必须机械检查 `required_outputs`。

## 7.6 Deviation

继续保持：

```text
LAB_USER reports
Experimental Worker records
Master decides
```

Lab User / Experimental Worker 不得自行修改实验方案。

## 7.7 Acceptance

- Human task created。
- LabScreen 可见。
- 上传缺失输出 → 继续等待。
- outputs 齐全 → Review。
- deviation 不绕过 Master。
- 真实实验 artifact 有 provenance。

---

# 8. P11-07：Release CI / Certification Gate

**优先级：P1。**

普通 CI：

```text
lint
unit
integration
e2e-mock
```

新增 release certification：

```text
phase11-acceptance
├── DSH live
├── live research
├── Research → Master readback
├── compute preparation
├── lab preparation
├── Slurm integration
├── HumanLab integration
├── Temporal reconciliation
└── full five-agent E2E
```

明确输出：

```text
CERTIFIED
PARTIALLY_CERTIFIED
NOT_CERTIFIED
```

live secrets 缺失时不得显示“完整认证成功”。

---

# 9. P11-08：User / Project / Membership 生命周期

**优先级：P1。**

当前：
- Account 由 Server Operator 创建。
- Gateway 无开放注册。
- ADMIN 是 project-scoped runtime admin。
- ADMIN 不是平台超级管理员。

Phase 11 继续保持。

## 9.1 User creation

继续：

```text
Server Operator
→ scripts/create_account.py
→ User
```

不增加 `/register`。

## 9.2 Project creation

允许 PROJECT_OWNER 创建 Project。

创建后：

```text
creator
→ membership PROJECT_OWNER
```

## 9.3 Member management

PROJECT_OWNER 可将已有 User 加入自己的 Project：

```text
LAB_USER
ADMIN
```

第一版不做 email invite。

## 9.4 Constraints

- LAB_USER 不管理成员。
- ADMIN 不管理成员。
- ADMIN 不自动获取 scientific authority。
- 不能删除最后一个 PROJECT_OWNER。
- membership mutation 必须有 audit event。
- role 不缓存在 token 中。

---

# 10. P11-09：Role-specific TUI

**优先级：P1。**

保持当前：

```text
PROJECT_OWNER → OwnerScreen
LAB_USER      → LabScreen
ADMIN         → AdminScreen
```

不是统一 dashboard 隐藏按钮。

## 10.1 OwnerScreen

至少：

- Master conversation
- project state
- DAG
- execution
- Research results
- Evidence
- Reviews
- approvals
- Pause / Resume
- member management
- project switch/create

Owner 仍然不能直接修改 DAG。

## 10.2 LabScreen

只强调：

- experiment tasks
- frozen procedure
- current status
- upload
- deviation
- responses

不要放：
- DAG mutation
- system runtime control
- unrestricted Master planning

## 10.3 AdminScreen

显示：

- DSH
- Temporal
- Supervisor
- backend health
- Slurm health
- running/failed jobs
- retry/reconciliation
- logs

不提供：
- scientific approval
- DAG mutation
- experiment modification
- Master scientific control

---

# 11. P11-10：Supervisor Managed Service

**优先级：P1/P2。**

推荐 systemd：

```text
ravel-gateway.service
ravel-supervisor.service
ravel-temporal-worker.service
```

要求：

- auto restart
- graceful shutdown
- structured logs
- active project recovery
- 不重复 backend submission
- health indication

验收：

```text
kill supervisor
→ systemd restarts
→ project recovers
→ no duplicate Slurm job
```

---

# 12. P11-11：真实科研 End-to-End Certification

**优先级：最终 P0。**

必须跑至少一个真实非纯 mock 任务。

建议材料/MOF 场景。

完整链路：

```text
Owner natural-language objective
    ↓
Master creates research contract / success contract / roadmap
    ↓
Research node
    ↓
Research Agent real search
    ↓
Research deep read
    ↓
Evidence / ResearchRecord
    ↓
Review
    ↓
Master reads Research result
    ↓
Master creates COMPUTATION node
    ↓
Compute Preparation
    ↓
real input files
    ↓
Compute Worker starts execution
    ↓
Temporal
    ↓
real Slurm
    ↓
real calculation artifact
    ↓
Review
    ↓
Master
    ↓
Experiment node
    ↓
Lab Preparation
    ↓
prepared experiment package
    ↓
Experimental Worker
    ↓
HumanLabBackend
    ↓
Lab User
    ↓
real experimental result
    ↓
Review
    ↓
Master
    ↓
SUCCESS / FAILED / INCONCLUSIVE
```

必须证明：

- Master 唯一修改 DAG。
- Research 真网。
- Research Evidence 可回流给 Master。
- Compute input 由 computation contract materialize。
- Lab protocol/package 由 experiment contract materialize。
- Compute Worker 不越权承担搜索/科研决策。
- Experimental Worker 不越权修改实验方案。
- 至少一个真实 Slurm job。
- HumanLabBackend 非 mock。
- simulated artifacts 不进入科学结论。
- supervisor kill/restart 可恢复。
- workflow/backend failure 可正确 reconciliation。
- 最终 conclusion 有 Decision / Review / Evidence provenance。

---

# 13. 推荐开发顺序

严格按：

```text
P11-01 Temporal reconciliation
        ↓
P11-02 Research deep read
        ↓
P11-03 Research → Master result readback
        ↓
P11-04 Execution Preparation / Materialization
        ├─ Compute Preparation
        └─ Lab Preparation
        ↓
P11-05 SlurmComputeBackend
        ↓
P11-06 HumanLabBackend
        ↓
P11-07 Release CI / Certification
        ↓
P11-08 User / Project / Membership
        ↓
P11-09 Role-specific TUI
        ↓
P11-10 Managed Supervisor
        ↓
P11-11 Real E2E Certification
```

原因：

1. 不修 Temporal recovery，真实 HPC 会放大重复提交和永久 RUNNING 风险。
2. 不修 Research deep read，参数来源不可靠。
3. 不打通 Research → Master，Research 结果无法稳定驱动 downstream computation。
4. 不做 Execution Preparation，计算和实验都缺少从 frozen contract 到可执行包的物化层。
5. Compute Preparation 为 Slurm 生成真实可执行计算输入；Lab Preparation 为 Human Lab 生成真实实验包。
6. Slurm 只负责执行已经准备好的计算；HumanLabBackend 只负责传递和接收已经准备好的实验任务。
7. 最后再收口 UI、服务化、release certification。

---

# 14. Definition of Done

每个任务完成必须同时满足：

1. code
2. migration（若涉及 schema）
3. unit tests
4. integration tests
5. necessary live tests
6. documentation
7. `KNOWN_LIMITATIONS.md`
8. `TEST_REPORT.md`
9. existing V0 / Phase10 tests 不回退
10. clean git commit
11. authority boundary tests
12. project isolation tests

---

# 15. Git discipline

开始前：

```bash
git status
git log --oneline -10
git branch --show-current
```

建议 commits：

```text
phase11: reconcile dead Temporal workflows
phase11: add bounded research source reading
phase11: expose research results to Master
phase11: add execution preparation framework
phase11: materialize computation workspaces
phase11: materialize laboratory execution packages
phase11: add real Slurm backend
phase11: add human lab backend
phase11: add release certification
phase11: add project membership management
phase11: complete role-aware TUI
phase11: manage supervisor as service
phase11: certify real scientific loop
```

禁止：

- force push
- 删除失败 tests
- 将失败 test 改 skip 来伪造通过
- mock 假装 real
- fake web source
- 让 Compute Worker 绕过 Master 修改 scientific method
- 让 Master 绕过 Research 将 model memory 当 Evidence
- 让 Review 修改 DAG
- 暴露 Slurm password 给模型

---

# 16. Phase 11 明确不做

以下后移：

- PLATFORM_ADMIN API/TUI
- public registration
- OAuth/SSO
- email invitation
- full LIMS
- robot lab
- autonomous lab
- multi-tenant billing
- all VASP/LAMMPS/GROMACS/RASPA integrations
- multi Harness
- Web frontend
- mobile app
- complex RBAC
- automatic compute pricing

---

# 17. Phase 11 完成后的目标状态

完成后：

```text
真实 Master
真实 Research Agent
真实 Review Agent
真实 Compute Worker
真实 Experimental Worker

真实 Research
真实 Evidence
Research → Master readback

真实 Execution Preparation
├── Compute Preparation
└── Lab Preparation
真实 Slurm execution
真实 Human Lab execution

可靠 Temporal recovery
角色化 TUI
项目成员管理
真实 release certification
```

RAVEL 此时应具备：

> **从自然语言研究目标，到 Research Evidence，到 Master 科学决策，到真实计算和实验执行，再到独立 Review 和项目结论的完整科研闭环。**

---

# 18. Claude Code 启动指令

将本文件放入仓库根目录后，对 Claude Code 使用：

```text
Read PHASE11_IMPLEMENTATION_PLAN.md completely before changing any code.

This document preserves the repository's existing agent responsibility model.

Do not redesign the agent roles.

The authoritative responsibility boundaries are:

- Master owns scientific planning, DAG mutation, scientific decisions, and Execution Contracts.
- Research Agent owns real literature/web/database search and Evidence creation.
- Compute Worker is an execution-control agent. It does not perform research, choose scientific methods, or mutate the DAG.
- Experimental Worker executes/co-ordinates experiment tasks under a frozen contract.
- Review independently evaluates work against frozen criteria.

Important computation rule:
Research Agent may research computation methods and parameters.
Master consumes that evidence and decides the computation plan.
Execution Preparation is a deterministic materialization layer, not an Agent.
Compute Preparation materializes Master's frozen computation contract into executable software input files.
Lab Preparation materializes Master's frozen experiment contract into an executable laboratory package.
Compute Worker starts and monitors computation execution.
Experimental Worker starts and monitors laboratory execution.
SlurmComputeBackend performs the durable HPC submission/status/collection/cancellation.
HumanLabBackend transports prepared laboratory tasks/results between RAVEL and Lab Users.

Before implementing:
1. verify main branch and clean working tree;
2. inspect existing Temporal workflows and known L-24 failure;
3. inspect Research source reading;
4. inspect how ResearchRecord/Evidence can currently be read by Master;
5. inspect Master DAG writing and ExecutionContract field coverage;
6. inspect Compute Worker role/tool roster;
7. inspect backend abstraction and existing mocks;
8. produce a short gap report for P11-01 only.

Then implement P11-01 completely.
After it passes its tests, continue through this document in order.

Do not:
- give Compute Worker research tools;
- give Compute Worker DAG mutation tools;
- let Master treat model memory as evidence;
- let compute or lab materializers invent scientific parameters;
- let Lab Preparation silently modify experiment conditions;
- weaken provenance, idempotency, project isolation or authority boundaries;
- remove mock backends; they remain test backends.
```
