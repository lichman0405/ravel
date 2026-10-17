# 验收门禁与证据规则

## G0 接管

T00保存实际目录/HEAD/dirty/worktree/remote，完整任务与日志存在，Reviewer批准。没有授权重置原目录或覆盖未提交变更。当前观察值是记录，不是执行时强制回退目标。

## G1 单任务

除T00/T03文档与T02引入门禁外，每代码任务在 `tests/premium/Txx/` 提供必要行为验证；各AC必须映射到具体测试nodeid或真实操作证据。`make premium-task TASK=Txx`运行映射命令、diff检查、该任务静态检查与受影响旧suite。代码必须已commit且干净；默认无SKIP/XFAIL/MISSING。不能为了满足数量复制实现逻辑写空测试。

T01创建：premium-preflight、premium-task、premium-source-digest；T22创建：premium-gate、premium-gate-plan、premium-live。文档里的命令是待实现接口，runner输出与Makefile必须一致。源代码digest算法见ENGINEERING_PROTOCOL。

## G2 基线与受影响回归

既有入口仍有效：make lint、make test-unit、完整tests/integration目录、tests/e2e、make acceptance、make phase10-acceptance，以及T02后make phase11-acceptance、make release-gate。全量内部测试由inventory按具体nodeid执行，不能简单使用 `-m not live` 假设标记完备。

legacy-inventory必须包含收集到的每个nodeid；每个外部分类说明为何需要真实模型/Internet/HPC/真人及原测试ID。Reviewer核对分类，防止把代码故障重分类成外部阻塞。旧 suite 包含live项时保留原命令的非零/skip日志，并由inventory给逐项internal结果，禁止把整套失败描述成PASS。

改domain/state/migration/权限/执行合同/共享脚本必须运行所有相关旧阶段与新task suite；其他独立UI改动可仅跑UI相关回归。完整internal gate在T23统一跑一次；失败修复后重复受影响行，最终manifest不得混用不同source digest的结果。旧release-gate要求真实模型等资源时记录BLOCKED_EXTERNAL，不篡改脚本为成功。

## G3 集成候选

Task branch APPROVE后先合临时候选，跑门禁并由Reviewer审合并后的真实tree，再推进integration。共享测试库串行。产品源码改变重新绑定Review与测试；只追加进度/测试记录不做无意义全量重跑。任何未解释回归都阻塞集成。

## G4 Internal

`make premium-gate PROFILE=internal` 必须覆盖：全部已分类legacy deterministic项目；所有T04–T22内部AC；迁移空库/升级/单head；Python静态类型；生成客户端一致性；TypeScript类型和构建；真实Electron安全及Windows打包启动；真实PG/Temporal/MinIO；协议级IMAP/SMTP隔离服务；真实Docker隔离；跨项目权限/租约竞争/Action版本/资源竞争/重启及不确定外部结果。

模拟Slurm和模拟实验人员可证明内部软件链，但输出显著SIMULATED；它们不会通过任何真实HPC/真人测量行。缺Docker/Linux不能因为属于环境问题就让internal变绿。

T23代码完成并本地main合并的条件是G4全部PASS且Reviewer最终批准。此时外部项可BLOCKED_EXTERNAL，整体只叫内部就绪。

## G5 Certified

`make premium-gate PROFILE=certified` = G4 + 真实DSH五角色/真实Research + T24腾讯云/COS + T25真实HPC/RASPA + T26真人实验 + T27真实邮箱 + T28真实VM + T29同Project科研闭环。并且原make release-gate无skip/缺资源行、真正CERTIFIED。

live入口 `make premium-live TASK=T24` 等必须先检查operator授权、测试资源allowlist、预算/有效时间和必需配置；缺少条件在任何副作用前退出非零。真人实验行的自动程序只核验真实人员上传的证据，不能自动执行实验或替真人签名。真实Research不能用fixture，允许unit parser用fixture。

若external条件不满足：内部PASS且外部blocked→PARTIALLY_CERTIFIED；任一实际错误→未通过。没有FULLY_CERTIFIED例外。

## 证据manifest

每次运行 `evidence/Txx/<run-id>/manifest.json` 至少：run_id、task_id、AC映射、北京时间与UTC、source_commit、source_digest、platform、依赖/软件版本、命令、cwd、exit_code、junit_path、log_path、artifacts[{ref,sha256,size}]、data_mode(REAL/SIMULATED/MIXED)、external_resources（非秘密ID）、operator_authorization_ref、result、blocked_reason。

不伪造签名：operator确认保存真实确认文本/签名文件及hash，自动化只能核验存在性、关联与文件完整性；物理测量正确性由产品Review/实验负责人评估。邮件证据要包含对端接收确认，SMTP250本身只证明服务器接受。HPC证据需要真实JobID/调度输出，不能仅manifest里一个自填数字。

大二进制留受控Artifact Store/本地evidence volume，Git保存索引与hash。必须在报告说明哪些文件受控保存、怎样由授权人员复核。所有秘密必须脱敏，原始敏感输出不打包进公开构建。
