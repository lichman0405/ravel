# 源码与执行基线

## 交接包观察（2026-10-17，只读）

- repo: C:/Users/lishi/code/ravel
- branch: main
- HEAD: 79db86e07d8c2e541fd366d6667b54ee76f671fb
- subject: docs: keep README a README, not a test dashboard
- git status --short: 交接时无输出，工作区干净。
- 阶段：Phase10；本地没有Phase11验收文档。
- 引入候选：官方同仓库 c9815f13282c6df85e76e616679473511e94f812。
- 祖先检查：已在只读研究副本确认 79db86e 是 c9815f 祖先；差异174个文件、29个后续提交。

本文件不是执行时的强制回退点。T00须重新读取actual HEAD/dirty/remote/worktree并追加接管快照；T02在任务分支引入固定候选，保持现有本地历史。

## T00 实际接管快照

- scan_time: 2026-10-17 13:06:15 +0800
- target_repo: C:/Users/lishi/code/ravel
- original_branch: main
- actual_head: 79db86e07d8c2e541fd366d6667b54ee76f671fb
- actual_status_porcelain_v1: `?? docs/premium/`
- dirty_note: 唯一未跟踪项是交接包 `docs/premium/`。原工作区保留原样；未执行 stash、reset --hard、git clean、强制 checkout 或代为 commit。
- remote_origin_fetch: https://github.com/lichman0405/ravel.git
- remote_origin_push: https://github.com/lichman0405/ravel.git
- worktrees_at_scan:
  - `C:/Users/lishi/code/ravel` — main — 79db86e
- MAIN_BASE_SHA: 79db86e07d8c2e541fd366d6667b54ee76f671fb
- ORIGINAL_BRANCH: main
- INTEGRATION_BRANCH: cc/ravel-premium-v1
- INTEGRATION_WORKTREE: C:/Users/lishi/code/ravel-worktrees/integration
- T00_WORKTREE: C:/Users/lishi/code/ravel-worktrees/T00
- T00_BRANCH: cc/T00-takeover
- PHASE11_SOURCE_SHA: c9815f13282c6df85e76e616679473511e94f812（尚未 fetch；T02 处理）
- WINDOWS_PATH_MAPPING: `C:/Users/lishi/code/ravel` ↔ Git Bash `/c/Users/lishi/code/ravel` ↔ WSL `/mnt/c/Users/lishi/code/ravel`
- BACKEND_LINUX_PATH: WSL2 distro `Ubuntu`，`/mnt/c/Users/lishi/code/ravel` 可达
- BACKEND_ENV_OBSERVED:
  - Ubuntu 26.04 LTS x86_64（不是任务书要求的 24.04）
  - Python 3.14.4（不是任务书要求的 3.12；`uv` 可用，可创建 3.12 用户级环境）
  - Docker Engine / Compose: 未安装（Windows 与 WSL 均未找到）
- DATABASE_TEST_LOCK: 待 T01 实现；锁路径由数据库/端口组合哈希生成
- ENV_GAP: 内部 gate 需要的 Ubuntu 24.04 + Python 3.12 + Docker/Compose 未满足。该缺口按 GATES 记录为 internal gate 不通过，不能用 Windows 或 26.04 冒充 canonical 环境。
- SECRETS: 本次扫描未读取任何 `.env` 值或凭据。

## 当前 worktree

- integration worktree 由 `git worktree add -b cc/ravel-premium-v1 <path> 79db86e...` 创建。
- T00 worktree 由 integration 分支创建，交接包复制到 `docs/premium/`。
- 原始工作区中的未跟踪 `docs/premium/` 保留不动。
