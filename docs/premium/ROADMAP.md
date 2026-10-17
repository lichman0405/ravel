# Roadmap 与派发顺序

状态：PLANNED，0/30任务完成。2026-10-17；本文件描述待执行工作，所有实现/外部门禁均未由本交接包执行。

| 波次 | 任务 | 可并行/完成条件 |
|---|---|---|
| W0 本地接管 | T00 → T01 → T02 | 串行。先保留本地工作，再建立环境与证据入口，最后审查引入已有Phase11。 |
| W1 接口冻结 | T03 | 串行。冻结共享接口、范围更新、AC和schema。 |
| W2 基础能力 | T04租约、T06审批预算、T07安全；随后T05重试、T08存储、T12证据、T14桌面壳、T22 gate骨架 | 最多3个实现任务，按共享文件锁排队；安全和权限缺口不让桌面绕过。 |
| W3 执行与API | T09 HPC、T13 API、T19 Docker；T10 RASPA和T11人类实验在其依赖DONE后派发 | HPC凭据和科学输入不进入模型自由参数。 |
| W4 工作台与通信 | T15科学工作台、T16 IMAP、T20 VM；之后T17 SMTP、T18审批/实验/邮箱UI | 界面必须连真实Gateway；邮箱内部只连隔离协议服务。 |
| W5 部署与集成 | T21部署包、T22 gate收口 → T23 internal集成与main合并 | 所有T00–T22 DONE且internal无skip才合本地main。 |
| W6 外部认证 | T24云/COS、T25 HPC、T26真人、T27邮箱、T28 VM | 资源齐全可并行，数据库/费用/实验权限锁分别独立；无资源明确BLOCKED_EXTERNAL。 |
| W7 最终闭环 | T29 | 同一Project全真实链路、最终Review和gate，才FULLY_CERTIFIED。 |

W2表中的“随后”不授权越依赖派发；唯一精确依赖为任务卡和PROGRESS.json。W2至W5是可滚动队列，不要求整波结束才开下一波；任务依赖全部DONE、文件锁空闲即可READY。

里程碑：M0=T00–T03；M1=T04–T13+T19；M2=T14–T23（同时满足M1）；M3=T24–T29。禁止设置虚构日期/人天承诺；每阶段完成后根据真实吞吐记录下一阶段估算。

资源准备清单：T01 Linux/Docker；live模型与Research key/contact；T24已授权CVM/COS和预算；T25 Slurm队列/known_hosts/软件许可；T26实验负责人/SOP/人员/设备；T27测试邮箱与允许收件人；T28专用VM。优先查既有安全配置，只记录是否存在和键名，不读出秘密。

Supervisor每个里程碑后追加“实际完成/新增风险/下一波”日志，并同步PROGRESS和生成的ROADMAP_STATUS.md。依赖重排要DECISION记录；新增范围追加任务ID及AC，不能删除本包任务伪装完成。
