# 外部资源与阻塞

更新时间：2026-10-17 18:05 Asia/Shanghai。以下只记录实际观察到的事实；没有外部资源的任务不阻止无关代码开发，但相关门禁不能标 PASS。

## B01 — 真实 MinIO 不可用

- blocker_id: B01
- opened_at: 2026-10-17
- task_ids: [T01, T02]
- status: OPEN
- category: RESOURCE
- observed_fact: 尝试从 Docker Hub/Quay/GHCR 获取 MinIO 镜像及官方二进制均返回 401/403/410；T02 integration 使用 `localstack/localstack:3.8`，证据标记为 `SIMULATED_S3`。
- missing_input: 可用的真实 MinIO 镜像/二进制，或获授权的 S3 兼容端点。
- unblock_condition: 真实 MinIO 启动后，用同一套 integration S3 测试和真实 endpoint 重跑；结果不能再标 `SIMULATED_S3`。
- owner: 环境/基础设施提供者。
- independent_work: T03、T04、T05、T06 等不依赖真实对象存储的任务。
- next_command: `docker pull minio/minio:latest`（凭据/镜像源可用后）。
- resolved_at/evidence: 未解决。LocalStack 结果只证明协议路径，**不能**通过原定真实 MinIO 门禁。

## B02 — 容器内无 systemd

- blocker_id: B02
- opened_at: 2026-10-17
- task_ids: [T01, T02]
- status: OPEN
- category: ENVIRONMENT
- observed_fact: canonical Ubuntu 24.04 容器没有 systemd/systemctl/systemd-analyze；`tests/unit/test_service_units.py` 有 2 个 `systemd-analyze is not installed on this host` skip，T02 的服务清理也以 `user_manager_is_running()` 保护。
- missing_input: 带 systemd 的 Ubuntu 24.04 主机/容器（或独立 VM）。
- unblock_condition: 在带 systemd 的环境运行 `tests/unit/test_service_units.py` 与 `systemd-analyze verify`，并验证 P11-10 的服务安装/启动/停止。
- owner: 环境提供者。
- independent_work: 其余代码任务。
- next_command: `.venv/bin/python -m pytest tests/unit/test_service_units.py -q`（在 systemd 主机上）。
- resolved_at/evidence: 未解决；相关 skip 不能计 PASS。

## B03 — 完整 release-gate 尚未完成

- blocker_id: B03
- opened_at: 2026-10-17
- task_ids: [T02, T29]
- status: OPEN
- category: ENVIRONMENT / VERIFICATION_GAP
- observed_fact: 第一次 release-gate 因 PATH 无 `ruff` 失败；第二次因数据库 deadlock 停止（该次结果作废）；共享锁修复后 `--only unit` 已取得锁并运行，但因 `scripts/install_services.sh` 在容器 checkout 中为 CRLF 而失败；`--only research-readback` 通过（10 passed, CERTIFIED），完整 gate 仍未跑完。
- missing_input: 规范化 LF checkout、systemd 环境、真实 MinIO/DSH/外部资源，以及串行执行窗口。
- unblock_condition: 在无并发 DB 测试的条件下跑完整 `release_gate.py`，并保存每行结果；任何 skip/fail 均按 `NOT_CERTIFIED`/`PARTIALLY_CERTIFIED` 记录。
- owner: Supervisor/环境提供者。
- independent_work: T05、T06 等不依赖完整 gate 的任务。
- next_command: `make release-gate`（先修正 CRLF，确保无其他 DB 测试并发）。
- resolved_at/evidence: 未解决。`--only unit` 和 `--only research-readback` 只是单行证据，不是完整 gate。

## B04 — T04 重开，等待独立复审

- blocker_id: B04
- opened_at: 2026-10-17
- task_ids: [T04]
- status: OPEN
- category: DESIGN_CORRECTION
- observed_fact: 原 T04 只在 turn/轮次边界检查租约；in-flight turn 在租约失效后仍可写 DAG/启动执行。用户明确要求重开为 `FIX_REQUIRED`。修复分支 `cc/T04-binding-fence`（d2c8741）已冻结启动 binding、隔离 brief 文件，并把 fence 扩展到所有科研/执行副作用入口；待新的独立 Review。
- missing_input: 独立 Reviewer 对 AC1–AC4 的复审结论；集成候选验证。
- unblock_condition: Reviewer APPROVE，`make premium-task TASK=T04` PASS，候选验证后 ff-only 合入 integration。
- owner: 独立 Reviewer / Supervisor。
- independent_work: T05、T06 的接口准备可以继续，但在 T04 DONE 前不作为 READY 派发。
- next_command: `make premium-task TASK=T04`（修复分支最终 HEAD）。
- resolved_at/evidence: 未解决。历史 `reviews/T04/1.json` 的 P3 延期已被用户否决，不能作为当前批准。

## B05 — T24–T29 的真实外部资源

- blocker_id: B05
- opened_at: 2026-10-17
- task_ids: [T24, T25, T26, T27, T28, T29]
- status: OPEN
- category: RESOURCE / AUTHORIZATION
- observed_fact: 尚无已授权的 CVM/COS、Slurm 队列/allocation、真人实验负责人/SOP/设备、专用 IMAP/SMTP 邮箱、专用 VM；真实 DSH 模型凭据也未进入本轮认证。
- missing_input: 对应资源、授权和联系人；秘密不写入本文件。
- unblock_condition: 各任务解锁条件按其任务卡；`make premium-live TASK=Txx` 必须真实运行并保存证据，不能用 mock 代替。
- owner: 用户/基础设施与实验室负责人。
- independent_work: T05、T06 等内部实现任务。
- next_command: `make premium-live TASK=T24`（资源与授权到位后）。
- resolved_at/evidence: 未解决；T24–T29 一律不得标 PASS。
