# RAVEL V0 — Autonomous Scientific Research Runtime

**RAVEL = Research Autonomous Validation & Execution Loop**

本仓库同时装着 RAVEL V0 的**实现**和它所依据的**规格包**。两者不是同一类东西，读之前先分清：

- **实现**：`src/`、`tests/`、`scripts/`、`Makefile`、`docker-compose.yml`、`alembic.ini`
- **规格**：`docs/`、`schemas/`、`prompts/`、`acceptance/`
- **交付记录**：`IMPLEMENTATION_REPORT.md`、`TEST_REPORT.md`、`KNOWN_LIMITATIONS.md`、`SECURITY_NOTES.md`、`DSH_INTEGRATION_REPORT.md`、`DEPLOYMENT.md`

其中 `schemas/*.yaml`、`prompts/*.md`、`acceptance/MOCK_SCENARIOS.yaml` **由运行时代码直接读取**，不是文档 —— `src/ravel/dsh/roles.py` 读那 5 个角色预设，`src/ravel/backends/scenarios.py` 读 mock 场景表。删掉它们，功能就坏。

## 一句话定位

RAVEL 是运行在云端 CVM 上、以 **DeepSeek Harness (DSH)** 为唯一底层 Harness 的自主科研执行 Runtime。它接收自然语言研究/产业需求，将其形式化为科学问题，进行真实网络/数据库研究，构建动态 Scientific DAG，调度计算与实验执行，独立 Review，并由 Master 在证据约束下持续重规划，直到 Project 进入 SUCCESS / FAILED / INCONCLUSIVE / TERMINATED。

RAVEL **不是代码开发 Agent**。代码、shell、HPC script 仅是科研执行手段。

## V0 现状

**实现完整，验收完整：27/27 项通过，0 skip。**

在填好 `.env` 里的 `DEEPSEEK_API_KEY` 与 `RAVEL_RESEARCH_CONTACT_EMAIL` 后，本机跑出了以下结果：

| 验证内容 | 结果 |
|---|---|
| `make acceptance`（A01–A20 + 7 gates） | 43 通过，0 skip |
| 真实模型做 Master/Review 决策（`tests/dsh`） | 11 通过，0 skip |
| 真实文献 / 网页抓取（`tests/live_research`） | 13 通过，0 skip |
| 结构性验证（unit / integration / e2e） | 752 / 537 / 20 通过 |

也就是说，让 RAVEL 区别于普通编排器的两件事 —— 模型真的做科研决策、真的读原始文献 —— **已经在这台机器上验证通过**。详见 `TEST_REPORT.md`。

**但 V0 仍不是可以公开部署的成品。** `SECURITY_NOTES.md` 与 `KNOWN_LIMITATIONS.md` 记录了尚未修补的缺口（L-08 DNS rebinding、L-14 浏览器路径），请先读它们再决定把它暴露到哪里。所有端口默认只绑 `127.0.0.1`。

**还有一点未 stress-test：** DSH spike 已用真模型跑过，但一个 Project 从头到尾每一轮 Master replan 和 Review verdict 都由真模型连续驱动、直到项目结束，这种长时间自治运行还没有被专门压测。这是 L-19 的剩余部分。

## Canonical 开发环境

RAVEL V0 的唯一标准开发、验收和生产基准环境是 **Ubuntu 24.04 LTS x86_64 + Python 3.12**。

开发 Agent 必须先阅读：
- `docs/14_DEVELOPMENT_ENVIRONMENT.md`
- `docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md`

## V0 核心边界

- Harness：**DeepSeek Harness only**
- Agent types：Master / Research / Review / Compute Worker / Experimental Worker
- Master：Project 级长期角色，默认 persistent session，可 checkpoint/recover
- Scientific DAG：RAVEL 自研、强 schema、滚动展开、支持 parallel/join
- Durable execution：Temporal
- Authoritative state：PostgreSQL
- Artifact：S3-compatible object storage（V0 推荐 MinIO）
- Research：**必须真实 Web / API / Database；严禁 mock 研究证据**
- Compute：V0 使用 MockComputeBackend
- Experiment：V0 使用 MockLabBackend
- 用户交互：本地 Textual TUI → Research Gateway → Cloud RAVEL/DSH
- Web Dashboard：V0 不做
- POST：完全独立项目；V0 不依赖，不实现 POST integration
- 用户不能直接修改 DAG
- 只有 Master 能修改 DAG / Execution Contract / research route

## 部署与运行

在单台 Ubuntu 24.04 上实现并验证（见上面的「V0 现状」——结构性验证完整，真模型与真抓取未跑）。完整说明见 `DEPLOYMENT.md`；下面是实际命令。

```bash
# 1. 环境（幂等；校验 DSH 是否为 vendor/DSH_PIN.json 钉住的版本）
scripts/bootstrap_ubuntu.sh

# 2. 配置：只有一个文件，且不进 Git
cp .env.example .env && $EDITOR .env

# 3. 启动：基础设施 + Gateway + 执行 worker + TUI
make up

# 4. 第一个账号与第一个 Project（账号只能在主机上开，密码走终端提示）
make account ARGS="--username ada \
  --new-project --title '…' --objective '…'"

# 5. 让某个 Project 跑起来
make project PROJECT=<project_id>
```

`make up` 是 START_PROMPT 要求的「一条命令启动 V0」：它起 PostgreSQL / Temporal / MinIO、跑 migration、起 Gateway 和 worker，然后进入本地 Textual TUI。不加 `--project` 就不启动任何 Project —— **V0 没有调度器，哪个 Project 运行是人的决定**。所有端口只绑 `127.0.0.1`。

`DEEPSEEK_API_KEY` 为空时服务照常启动，但 Master 与 Review 无法完成任何 turn，Project 不会规划也不会 Review：这是设计行为，不是降级模式。没有 `RAVEL_RESEARCH_CONTACT_EMAIL` 时 `tests/live_research` 与 A03/A04 会 skip。

### 测试

```bash
make lint          # ruff check src tests + pyright src tests（唯一门禁）
make acceptance    # 跑 A01–A20 与 7 个额外 gate，打印 pass/fail 矩阵
scripts/test_all.sh  # lint → unit → integration → DSH gate（需要 DEEPSEEK_API_KEY）
```

**同一台机器上绝不并发跑两个 pytest 进程**：它们共用 `ravel_test`，进入时都会 TRUNCATE。

### 交付文档

- `IMPLEMENTATION_REPORT.md`：实现报告，逐阶段与门禁
- `TEST_REPORT.md`：测试报告与真实数字
- `KNOWN_LIMITATIONS.md`：已知限制，逐条说明「不要由此推断什么」
- `DSH_INTEGRATION_REPORT.md`：DSH 集成与 pin 的验证记录
- `SECURITY_NOTES.md`：安全姿态与未关闭风险
- `DEPLOYMENT.md`：部署、配置、运行、测试的完整说明

## 目录

### 实现

- `src/ravel/`：运行时本体 —— `dsh/` 角色绑定与 pool、`domain/` 领域模型与状态机、`state/` Postgres 仓储与 migration、`execution/` Temporal workflow、`research/` 真实来源连接器、`master/`、`review/`、`gateway/` HTTP + WebSocket、`tui/` Textual 客户端、`backends/` mock compute/lab、`mcp/`
- `tests/`：`unit/`、`integration/`、`dsh/`、`e2e/`、`live_research/`、`acceptance/`、`support/`
- `scripts/`：环境引导、起停、账号、验收矩阵
- `infra/postgres/init/`：Temporal 需要的库

### 规格（运行时部分被代码读取）

- `prompts/`：5 类 Agent 的行为规范 —— **运行时代码读取，即角色预设本体**
- `schemas/`：机器可读领域 schema / event / backend contract —— **运行时代码读取**
- `acceptance/`：V0 端到端验收矩阵与 mock 场景 —— `MOCK_SCENARIOS.yaml` 被运行时读取
- `docs/`：完整产品和技术规格
- `references/`：截至 2026-09-17 的 DSH 现状与官方资料指针

### 规格入口

- `START_PROMPT.md`：唯一启动提示词（建仓时的 agent 指令，保留为档案）
- `CLAUDE.md`：开发 agent 强制规则（原 `AGENTS.md` 与之内容一致，已合并删除）
- `START_PROMPT.md`：唯一启动提示词（建仓时的 agent 指令，保留为档案）

## Source of Truth 优先级

发生冲突时按以下优先级：

1. `docs/00_PRODUCT_AND_SCOPE.md`
2. `docs/01_ARCHITECTURE.md`
3. `docs/02_AGENT_MODEL.md`
4. `docs/03_SCIENTIFIC_DAG.md`
5. `docs/04_STATE_AND_DATA.md`
6. `docs/05_RESEARCH_AND_EVIDENCE.md`
7. `docs/06_EXECUTION_AND_REVIEW.md`
8. `docs/07_DSH_INTEGRATION.md`
9. `docs/08_GATEWAY_AND_TUI.md`
10. `docs/09_SECURITY_AND_IDENTITY.md`
11. `docs/10_IMPLEMENTATION_PLAN.md`
12. `acceptance/V0_ACCEPTANCE.md`
13. 其他文件

如果仍不明确：选择**最小实现、最强审计、最少隐式判断、最少耦合**的方案，并在 `docs/IMPLEMENTATION_DEVIATIONS.md` 记录。
