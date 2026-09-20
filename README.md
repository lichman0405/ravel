# RAVEL V0 — Autonomous Scientific Research Runtime

**[English](README-en.md) | 中文**

**RAVEL = Research Autonomous Validation & Execution Loop**

RAVEL V0 是一个运行在单台 Ubuntu 24.04 CVM 上的自主科研执行 Runtime。它接收自然语言的研究/产业需求，将其形式化为科学问题，进行真实的网络/API/数据库研究，构建动态 Scientific DAG，调度计算与实验执行，独立 Review，并由 Master 在证据约束下持续重规划，直到 Project 进入 `SUCCESS`、`FAILED`、`INCONCLUSIVE` 或 `TERMINATED`。

**RAVEL 不是代码开发 Agent。** 代码、shell、HPC script 只是科研执行手段。

底层 Harness 唯一使用 **DeepSeek Harness (DSH)**，版本钉在 `dsh-v0.1.5-rc.1`（见 `vendor/DSH_PIN.json`）。

---

## 目录

- [项目现状](#项目现状)
- [系统要求](#系统要求)
- [安装与初始化](#安装与初始化)
- [启动与部署](#启动与部署)
- [日常使用](#日常使用)
- [运行测试](#运行测试)
- [CI / GitHub Actions](#ci--github-actions)
- [安全与已知限制](#安全与已知限制)
- [目录结构](#目录结构)
- [文档索引](#文档索引)
- [常见问题 / 故障排查](#常见问题--故障排查)

---

## 项目现状

**V0 实现完整，验收完整：27/27 项通过，0 skip。**

在填好 `.env` 里的 `DEEPSEEK_API_KEY` 与 `RAVEL_RESEARCH_CONTACT_EMAIL` 后，本机跑出的结果是：

| 验证内容 | 结果 |
|---|---|
| `make acceptance`（A01–A20 + 7 个额外 gates） | 43 通过，0 skip |
| 真实模型做 Master/Review 决策（`tests/dsh`） | 11 通过，0 skip |
| 真实文献 / 网页抓取（`tests/live_research`） | 13 通过，0 skip |
| 结构性验证（unit / integration / e2e） | 752 / 537 / 20 通过 |

让 RAVEL 区别于普通编排器的两件事 —— **模型真的做科研决策**、**真的读原始文献** —— 已经在这台机器上验证通过。详见 `TEST_REPORT.md`。

**但 V0 还不是可以公开部署的成品。** `SECURITY_NOTES.md` 与 `KNOWN_LIMITATIONS.md` 记录了尚未修补的缺口（如 L-08 DNS rebinding、L-14 浏览器路径）。请先读它们，再决定把 RAVEL 暴露到哪里。默认所有端口只绑 `127.0.0.1`。

> **唯一未完成的可选项：** DSH spike 已用真模型跑过，但“一个 Project 从头到尾每一轮 Master replan 和 Review verdict 都由真模型连续驱动直到结束”的长程自治 stress test 还没专门跑。这是 `KNOWN_LIMITATIONS.md` L-19 的剩余部分，不影响 V0 功能 completeness。

---

## 系统要求

RAVEL V0 的**唯一标准环境**是：

| 项 | 要求 |
|---|---|
| OS | Ubuntu 24.04 LTS x86_64 |
| Python | 3.12（需 `python3.12-venv`） |
| 容器 | Docker Engine + Docker Compose v2 |
| 网络 | 可访问 DeepSeek API、Crossref/OpenAlex/arXiv 等真实研究源 |
| 模型凭据 | `DEEPSEEK_API_KEY`（运行 Master/Review 必需） |

DSH runtime 是 `manylinux_2_28_x86_64` wheel，因此非 x86_64 平台在第一个 agent turn 就会失败。Windows/macOS 不是 V0 的标准开发和部署环境。

---

## 安装与初始化

### 1. 克隆仓库

```bash
git clone <this repository> ravel && cd ravel
```

### 2. 一键引导环境

```bash
scripts/bootstrap_ubuntu.sh
# 或：make bootstrap
```

`bootstrap_ubuntu.sh` 是幂等的，会：

- 检查 Ubuntu 24.04 x86_64；
- 安装/校验 apt 基础包；
- 创建 `.venv` 并安装项目依赖（优先 `uv`，回退 `pip`）；
- 校验钉住的 DSH 版本（`deepseek-harness-sdk==0.1.5rc1`、`deepseek-harness-runtime-bin==0.1.5rc1`）；
- 安装 Playwright Chromium（若系统库缺失会给出警告，见 `KNOWN_LIMITATIONS.md` L-06）；
- 检查 Docker daemon；
- 如 `.env` 不存在，从 `.env.example` 复制一份并设权限 `600`。

无 sudo 时可跳过系统包安装：

```bash
scripts/bootstrap_ubuntu.sh --no-apt
```

### 3. 配置 `.env`

```bash
cp .env.example .env
$EDITOR .env
```

`.env` 已被 Git 忽略，是唯一该放秘密的地方。关键变量：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 空 | **运行 Master/Review 必需**。从 https://platform.deepseek.com/ 获取。 |
| `RAVEL_POSTGRES_PASSWORD` | `ravel_dev_password` | 如有外网可达，请修改。 |
| `RAVEL_S3_ACCESS_KEY` / `RAVEL_S3_SECRET_KEY` | dev 值 | MinIO 凭据。 |
| `RAVEL_GATEWAY_JWT_SECRET` | `dev-only-change-me` | **必须修改**。已知签名密钥意味着可伪造会话。 |
| `RAVEL_RESEARCH_CONTACT_EMAIL` | 空 | 可选但建议填写。Crossref/OpenAlex/NCBI 用此识别客户端并导向更快池；未设置时 `tests/live_research` 与 A03/A04 会 skip。 |

`RAVEL_RESEARCH_CONTACT_EMAIL` 请填你真实拥有的邮箱；填占位符会 defeat 这些服务要求地址的本意。没有搜索 API key 也能做真实研究 —— RAVEL 直接访问公开源，且不会 fallback 到 mock 搜索。

---

## 启动与部署

### 单条命令启动 V0

```bash
make up
```

这等于 `scripts/run_v0.sh`，会依次：

1. 启动 PostgreSQL、Temporal、MinIO 并等待健康检查；
2. 应用数据库 migrations 到 head；
3. 启动 Gateway（HTTP/WebSocket）；
4. 启动执行 worker；
5. 启动 Project Supervisor（自动发现所有活跃 Project 并驱动）；
6. 打开本地 Textual TUI。

退出 TUI（`q` 或 `Ctrl-C`）会同时停止 Gateway、worker、supervisor 和本脚本启动的 loop，但容器会继续运行。停止容器用 `make dev-down`。

`DEEPSEEK_API_KEY` 为空时服务也会启动，但 Master 与 Review 无法完成任何 turn，Project 不会规划也不会 Review —— 这是设计行为，不是降级模式。

### 常用变体

```bash
# 无 TUI，作为后台服务运行
scripts/run_v0.sh --no-tui

# 启动 unattended Project Supervisor（不指定 project）
.venv/bin/python scripts/run_supervisor.py

# 启动时同时驱动某个 Project（不启动 supervisor）
scripts/run_v0.sh --project <project_id>

# 单独启动某个组件
make gateway     # Gateway + hot-reload
make worker      # Temporal 执行 worker
make supervisor  # 无人值守 Project Supervisor
make tui         # 纯 TUI 客户端（需 Gateway 已在运行）
```

### 服务与端口（全部绑在 127.0.0.1）

| 服务 | 端口 | 说明 |
|---|---|---|
| Gateway | 8000 | HTTP + WebSocket，用户唯一入口 |
| PostgreSQL | 55432 | 权威 Project State |
| Temporal frontend | 7233 | durable execution |
| Temporal UI | 8088 | 观察 workflow 运行 |
| MinIO S3 API | 9100 | artifact 字节存储 |
| MinIO console | 9101 | 对象存储管理界面 |

**这些端口故意只绑 loopback。** Temporal 和 MinIO 的 API 在传输层无认证，且开发凭据在仓库里，直接暴露会把项目数据库交给任何能连到主机的对象。如需从另一台机器访问，请用 SSH 隧道：

```bash
ssh -N -L 8088:127.0.0.1:8088 -L 9101:127.0.0.1:9101 user@cvm
```

### 停止

```bash
make dev-down                 # 停容器，保留数据
scripts/dev_down.sh -v        # 停容器并删除数据（会确认）
```

`-v` 会同时删除 `runtime/dsh_home`、`runtime/workspaces`、`runtime/snapshots`，避免 harness 状态残留指向已不存在的 project。

---

## 日常使用

### 创建第一个账号与 Project

Gateway 能认证用户，但不能创建用户。Membership 是制造 authority 的唯一入口，因此必须在本机终端执行：

```bash
# 创建一个 Owner，并同时开一个新的 Project
make account ARGS="--username ada --new-project \
  --title 'Catalyst screen' \
  --objective 'Find a dopant that raises conductivity by 15%.'"

# 或直接用脚本
.venv/bin/python scripts/create_account.py --username ada \
  --new-project --title "Catalyst screen" \
  --objective "Find a dopant that raises conductivity by 15%."
```

密码会在终端提示输入并二次确认，用 Argon2id 哈希，不会打印、不会存储。`--password`  flag 仅用于脚本化，会进 shell history。

### 给同一 Project 添加其他成员

```bash
make account ARGS="--username bench --project <project_id> \
  --role LAB_USER --granted-by ada"
```

只有已持有不低于目标权限的成员才能 grant；project 的**第一个** membership 可省略 `--granted-by`。

### 驱动 Project 运行

V0 现在默认通过无人值守的 **Project Supervisor** 驱动所有活跃 Project：

```bash
make supervisor
# 或
.venv/bin/python scripts/run_supervisor.py
```

Supervisor 会：

- 定期轮询 PostgreSQL，发现所有未结束且未暂停的 Project；
- 为每个 Project 创建 Master、Review、Compute Worker、Experimental Worker 的 DSH session；
- 进入 loop：启动运行、等待结果、在需要决策/评审/Worker 沟通时调用对应 Agent；
- 直到 Project 结束或连续多轮无进展。

如需只驱动单个 Project（例如调试或 CI）：

```bash
make project PROJECT=<project_id>
# 或
.venv/bin/python scripts/run_project.py --project <project_id>
```

常用参数：

```bash
.venv/bin/python scripts/run_project.py --project <project_id> \
  --max-rounds 200 --poll-seconds 0.5
```

### 使用 TUI

`make up` 默认会打开 TUI。若单独启动：

```bash
make tui
```

登录后，Project Owner / Research User 可：

- 选择/创建 project；
- 与 Master 自然语言对话；
- 查看当前 focus、DAG、decisions、reviews、evidence；
- 处理 approval / pause / resume；
- 调整 Authority Envelope；
- 上传用户 artifact。

Lab User 只能看到分配的实验任务；Admin 看到 DSH/Temporal/Runtime 健康状态，不做科学决策。

### Worker 的后端场景

V0 的 compute 和 lab 都是 mock，通过 scenario 决定行为：

```bash
make worker ARGS="--compute-scenario COMPUTE_SCIENTIFIC_FAILURE --lab-scenario LAB_DEVIATION_PRESSURE"
```

可用 scenario 见 `scripts/run_temporal_worker.py` 顶部，或运行：

```bash
.venv/bin/python scripts/run_temporal_worker.py --help
```

默认是 `COMPUTE_SUCCESS` / `LAB_SUCCESS`。mock 产生的所有 artifact 都会被标记为 simulated，不能作为 Evidence Ledger 的真实证据。

---

## 运行测试

### 环境检查

```bash
make env-check
```

Phase -1 的门禁：检查平台、设置、PostgreSQL、MinIO、Temporal、钉住的 DSH 发行版。

### 常用测试命令

```bash
make lint             # ruff check + pyright（lint 是代码门禁）
make typecheck        # 仅 pyright
make test-unit        # 无需服务
make test-integration # 需要先 make dev-up
make test-e2e         # headless loop + TUI
make test-dsh         # Phase 0 harness gate，需要 DEEPSEEK_API_KEY
make test-live        # 真实网络研究，需要 RAVEL_RESEARCH_CONTACT_EMAIL
make acceptance       # A01–A20 + 7 个额外 gate，打印 pass/fail 矩阵
```

`make lint` 里的 `pyright` 是 Node 工具，不在 venv 里。如未安装：

```bash
npm install -g pyright
```

`make acceptance` 是 V0 最终门禁：它不仅报告“多少测试通过”，还会按 A01–A20 每个 item 打印矩阵，没有对应测试的 item 会被标为失败而不是空白。

### 完整默认测试链

```bash
make test
# 等价于 scripts/test_all.sh：lint → unit → integration → DSH gate
```

**注意：**

- **同一台机器上不要并发跑两个 pytest 进程**，它们共用 `ravel_test` 数据库，进入时都会 TRUNCATE。
- **`make test` 在没有 `DEEPSEEK_API_KEY` 时会失败**（停在 DSH gate），这是设计：该 gate 不能通过“不运行”来通过。其他所有 suite 都不需要 API key。

---

## CI / GitHub Actions

仓库已配置 `.github/workflows/ci.yml`。

- 每次 push/PR 到 `main`：跑 `lint`、`unit`、`integration`、`e2e`、`acceptance`。
- 仅在 `main` push 且仓库存在对应 secret 时：跑 `make test-dsh` 和 `make test-live`。

这样 PR 不会消耗 API 额度，合并到 main 后才做真实模型/真实网络验证。

如需在 CI 中跑满，请在仓库 **Settings → Secrets and variables → Actions** 添加：

- `DEEPSEEK_API_KEY`
- `RAVEL_RESEARCH_CONTACT_EMAIL`

---

## 安全与已知限制

在把 RAVEL 放到任何可被他人访问的环境前，请完整阅读：

- `SECURITY_NOTES.md` — 安全姿态与未关闭风险。
- `KNOWN_LIMITATIONS.md` — 逐条列出已知限制，并说明“不要由此推断什么”。

关键提醒：

- 默认所有服务端口只绑 `127.0.0.1`；不要改成 `0.0.0.0`。
- `.env` 是 git-ignored，永远不要提交。
- V0 的 compute/lab 是 mock，research 是真实的，绝不允许 mock web evidence。
- 没有用户级撤销（deactivation/revocation）路径；authority 一旦 grant 就只能等 project 结束。
- V0 没有 scheduler，loop 是进程不是服务。

---

## 目录结构

```text
src/ravel/                 # 运行时本体
  domain/                  # 领域记录、闭枚举、状态机
  state/                   # PostgreSQL 表、约束、仓储、migration
  dag/                     # Scientific DAG 服务与 Master's mutation 工具
  research/                # 真实来源连接器、抓取、证据、sufficiency
  execution/               # loop、node run、Temporal workflow/activity
  backends/                # mock compute / mock lab
  master/                  # Master 决策与 ending
  review/                  # Review checkpoint 与 verdict 应用
  mcp/                     # 工具注册表与 5 个 per-role MCP server
  dsh/                     # harness pool、composition、角色绑定
  gateway/                 # 认证、权限、REST + WebSocket
  tui/                     # Textual 控制台客户端

tests/
  unit/                    # 纯内存测试
  integration/             # 需要 PostgreSQL/Temporal/MinIO
  dsh/                     # 真实 DSH runtime + 真实模型 turn
  e2e/                     # headless loop + TUI
  live_research/           # 真实网络研究，永不 mock
  acceptance/              # A01–A20 与 7 个额外 gate

scripts/                   # 引导、起停、账号、验收矩阵、worker/loop 入口
infra/postgres/init/       # Temporal 需要的初始库
docker-compose.yml         # PostgreSQL + Temporal + MinIO
prompts/                   # 5 类 Agent 角色预设（运行时代码读取）
schemas/                   # 机器可读 schema / contract（运行时代码读取）
acceptance/MOCK_SCENARIOS.yaml  # mock 场景表（运行时代码读取）
docs/                      # 产品与技术规格
```

---

## 文档索引

| 文件 | 内容 |
|---|---|
| `START_PROMPT.md` | 建仓时的启动提示词（档案） |
| `CLAUDE.md` | 开发 Agent 强制规则 |
| `IMPLEMENTATION_REPORT.md` | 实现报告，逐阶段与门禁 |
| `TEST_REPORT.md` | 测试报告与真实数字 |
| `KNOWN_LIMITATIONS.md` | 已知限制 |
| `SECURITY_NOTES.md` | 安全姿态与开放风险 |
| `DSH_INTEGRATION_REPORT.md` | DSH 集成与 pin 验证记录 |
| `DEPLOYMENT.md` | 部署、配置、运行、测试完整说明 |
| `docs/IMPLEMENTATION_DEVIATIONS.md` | 实现偏离规格的记录 |
| `docs/00_PRODUCT_AND_SCOPE.md` | Source of Truth 优先级最高 |
| `docs/01_ARCHITECTURE.md` | 架构 |
| `docs/02_AGENT_MODEL.md` | Agent 模型与 5 类角色 |
| `docs/03_SCIENTIFIC_DAG.md` | Scientific DAG |
| `docs/04_STATE_AND_DATA.md` | State 与数据 |
| `docs/05_RESEARCH_AND_EVIDENCE.md` | 研究与证据 |
| `docs/06_EXECUTION_AND_REVIEW.md` | 执行与 Review |
| `docs/07_DSH_INTEGRATION.md` | DSH 集成 |
| `docs/08_GATEWAY_AND_TUI.md` | Gateway 与 TUI |
| `docs/09_SECURITY_AND_IDENTITY.md` | 安全与身份 |
| `docs/10_IMPLEMENTATION_PLAN.md` | 实现计划 |
| `docs/14_DEVELOPMENT_ENVIRONMENT.md` | 标准开发环境 |
| `docs/15_AUTONOMOUS_DEVELOPMENT_CONTRACT.md` | 自主开发合约 |

---

## 常见问题 / 故障排查

**Q: `make up` 提示 `.env missing`**  
A: 运行 `cp .env.example .env` 并至少填写 `DEEPSEEK_API_KEY` 和 `RAVEL_GATEWAY_JWT_SECRET`。

**Q: `make lint` 报 `pyright: command not found`**  
A: `npm install -g pyright`。bootstrap 不会安装 Node/pyright。

**Q: `make test-dsh` 全 skip 或失败**  
A: 检查 `.env` 里 `DEEPSEEK_API_KEY` 是否设置且不为空。`make test` 会因此失败，这是设计。

**Q: `make test-live` 全 skip**  
A: 设置 `RAVEL_RESEARCH_CONTACT_EMAIL` 为一个你真实拥有的邮箱。

**Q: 两个测试进程同时跑，结果不稳定**  
A: 不要并发跑 pytest。它们共享 `ravel_test` 并会在进入时 TRUNCATE。

**Q: 浏览器研究测试失败 / Chromium 起不来**  
A: 见 `KNOWN_LIMITATIONS.md` L-06。在缺少系统库的机器上需要运行：

```bash
sudo .venv/bin/python -m playwright install-deps chromium
```

**Q: 我想从另一台机器访问 Temporal UI / MinIO console**  
A: 用 SSH 隧道，不要改 `docker-compose.yml` 的端口绑定。

---

License: MIT — see `LICENSE`.
