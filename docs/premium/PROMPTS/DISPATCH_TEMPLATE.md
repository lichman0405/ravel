# Supervisor派发模板（占位必须填写后发送）

task_id: Txx
title: 从任务卡读取
role: DEVELOPMENT_SUBAGENT 或 REVIEWER
repository: C:/Users/lishi/code/ravel
worktree: 实际绝对路径
branch: cc/Txx-任务slug
base_sha: 实际完整SHA
candidate_sha: 审查时必填，实现派发可为null
source_digest: 审查时必填
depends_on_done: 列任务ID及集成SHA
read_first: docs/premium/TASKS/本任务.md、SPEC、CONTRACTS、GATES及仓库规则
write_allowlist: 精确路径列表
shared_file_locks: 路径、owner、授予记录；没有则写NONE
test_environment: 真实OS/路径/服务引用，禁止秘密值
database_test_lock: 共享锁路径/持有者或申请方式
acceptance_ids: 完整AC列表
commands: 当前已有准确命令；待实现入口要标明，不能当已存在
required_deliverables: 从任务卡列出文件/证据
task_log: docs/premium/tasklogs/Txx.md
handoff_format: task_id/base_sha/head_sha/source_digest/files/AC-results/gates/issues/locks

在此附任务卡全文。禁止省略AC或只发送概括性一句话。
