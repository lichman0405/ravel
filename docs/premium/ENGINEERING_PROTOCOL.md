# 开发、审查与本地 Git 流程

## 1. 仓库和工作区

唯一目标：`C:/Users/lishi/code/ravel`。Supervisor记录当前分支和MAIN_BASE_SHA。集成分支固定 `cc/ravel-premium-v1`；任务分支 `cc/Txx-<任务卡slug>`；worktree放 `C:/Users/lishi/code/ravel-worktrees/integration`、`Txx`、`review`、`candidate-Txx`。Linux下映射实际同一提交图；路径写入BASELINE。已存在路径/分支先读取和恢复，不覆盖。

从当前已提交main建立集成分支：`git worktree add -b cc/ravel-premium-v1 <integration-path> <MAIN_BASE_SHA>`。每task由最新已集成SHA建立worktree。原工作区有用户改动时保持原样，禁止自动stash、reset --hard、git clean、强行checkout覆盖、强删分支或删除volume。工作区改动影响同一目标功能时记录冲突，不能自动推断“用户不需要了”。

T02允许fetch固定官方上游对象；不自动pull main，不跟随移动的远端HEAD。其后全部本地操作；本包没有授权push、远端tag、GitHub Release或公开部署。

## 2. 派发与文件锁

Supervisor最多派发3个实现任务，加1个独立Reviewer。每task必须持有自己的写路径白名单。共享文件包括 `src/ravel/config.py`、`domain/enums.py`、`state/tables.py`、`state/mapping.py`、`state/guards.py`、`state/migrations/**`、`gateway/app.py`、`mcp/registry.py`、`pyproject.toml`、Makefile、JS workspace manifest/lockfile及CONTRACTS/schema。

派发前将每task白名单展开成具体路径并登记独占拥有者；两个写路径模式相交（包括目录通配和尚未创建的同名文件）就不能同时写。若任务卡路径过宽，Supervisor须在派发记录细化不重叠子路径后才能并行。只有独立新模块、独立测试文件且无公共文件写冲突，才允许同时实现；最大3不意味着必须凑满3个。

共享文件采用独占批次：Subagent提交变更请求，Supervisor在FILE_LOCKS授予具体路径/任务/分支，其他任务暂停编辑这些路径；获得者完成并提交后由Supervisor集成/释放。锁不会因Agent沉默而过期；Supervisor确认Agent停止写入且工作区状态已保存才能回收。没有锁的任务可阅读、开发自身独立模块和测试，不得预写共享patch等待偷偷合并。

所有新migration由当前持锁任务按唯一Alembic revision追加；不改已应用migration。Supervisor在每次集成检查单head；冲突用单独merge migration或重写尚未发布的新revision，理由和测试入日志。SQLAlchemy表约束、migration及schema必须一致。

## 3. Commit

Subagent只在自身分支commit，格式 `feat(T09): enforce Slurm submission identity`、`fix(T09): reconcile unknown scheduler response`、`test(T09): cover lost submission response`、`docs(T09): record execution evidence`。一个commit一件可解释的事，不把格式化全仓与功能混在一起。

提交前运行 `git diff --check` 和任务规定的检查，确保没有.env、凭据、客户数据或大二进制。禁止把测试失败的中间commit描述成已完成；允许提交中间进度，但候选HEAD必须完整且干净。无变化就写ALREADY_SATISFIED证据，不制造空commit。

tasklogs/Txx由该Subagent拥有；全局状态文件只由Supervisor写。任务交接必须给base_sha、head_sha、changed_files、AC证据、准确测试命令/exit、known issues和共享文件锁情况。

## 4. 独立 Review

Reviewer在固定候选HEAD创建detached worktree，读取任务/契约/日志和完整相关diff；不与作者共用可变工作目录。Reviewer可以跑测试但数据库suite仍必须经过全局测试锁；不可修改实现以让测试通过。

输出使用TEMPLATES/review.json：candidate_sha、base_sha、source_digest、AC逐项结果、findings（文件/行/触发条件/影响/要求修复）、decision。任何未解决的需求违背、权限漏洞、数据丢失、科学证据混淆、未跑必需测试或空实现均CHANGES_REQUESTED。风格建议标advisory，不能扩张本包范围。

作者修复并commit后重新Review。三轮仍不通过时Supervisor先做根因分析、缩小变更或拆子任务；验收标准不降低。只在发现不可由本包决定的重大设计冲突时问用户。

## 5. 集成顺序（不可省略）

1. 确认任务依赖DONE、候选分支干净、作者已交接、第一轮Reviewer APPROVE。
2. 从最新集成SHA建立临时candidate分支/worktree，`git merge --no-ff <task-branch>`；冲突由Supervisor逐项修复并commit，禁止整目录ours/theirs。
3. 在这个集成候选运行任务门禁及受影响回归；schema/执行状态/权限/公用脚本变化还运行对应旧suite。数据库测试串行。
4. Reviewer复核集成候选（尤其冲突修复）及准确source digest；未批准不更新集成分支。
5. 集成分支 `git merge --ff-only <candidate-branch>`，更新PROGRESS/DEVLOG/ROADMAP并commit；只有这时任务DONE。
6. 若期间集成分支被推进，重新生成candidate并重做受影响检查；禁止强推以覆盖。

Supervisor自身写的集成修复没有自审例外。

## 6. SHA 与日志造成的自引用

Review和测试同时绑定实际commit SHA与source_digest。source_digest由T01实现：对Git已跟踪的路径、mode、blob id排序后计算SHA256；只排除明确的进度/执行记录文件：`docs/premium/PROGRESS.json`、`PROGRESS.md`、`DEVLOG.md`、`FILE_LOCKS.json`、`BLOCKERS.md`、`tasklogs/**`、`reviews/**`、`evidence/**`、BASELINE.md、ROADMAP_STATUS.md、INTERNAL_ACCEPTANCE.md、FINAL_REPORT.md、RELEASE_MANIFEST.json、PACKAGE_CHECK.json、PACKAGE_MANIFEST.json、完整任务书_汇总.md。所有这些文件名均相对于docs/premium；除上述列表无其他排除。SPEC、CONTRACTS、TASKS、GATES、实现、测试、依赖、schema、prompt、部署配置均不能排除。

追加纯记录commit不触发全量测试重跑；产品或规范digest变化就使旧批准失效。日志保存被测SHA，后续日志commit不伪装成被测SHA。人工结果manifest自身hash不放进自己的字节里，外层运行索引记录它的hash。

## 7. main 合并与后续修复

T23只有internal全PASS、T00–T22全部DONE、Reviewer最终APPROVE、main仍是记录的baseline且原工作区干净才可 `git merge --no-ff cc/ravel-premium-v1` 到本地main。若main已前进，先将新main合入临时候选并重新Review/受影响回归，重新记录baseline；禁止reset main。

main合并前后核对source_digest一致；main新增merge commit而源码完全相同不重跑昂贵live suite，只保存ancestor/tree核对和diff检查。推进integration为新main后，T24–T29的修复继续任务分支流程。最终gate绑定最终源码digest。无代码变化的外部证据commit只需Reviewer核验真实性。

回退使用独立修复分支或经Review的 `git revert -m 1 <merge-sha>`，不删历史。任何涉及数据库/外部动作的回退先核对兼容性，不能把Git回退当实验/发信/计算已撤销。

## 8. 恢复上下文

会话中断后Supervisor依次读PROGRESS.json、ROADMAP、DEVLOG末尾、FILE_LOCKS、最近Review、git status/worktree list。对RUNNING任务询问实际Agent/检查分支，不重建相同任务或清空进度。资源解锁后仅重跑受影响gate；source_digest或环境版本变化必须说明重跑范围。
