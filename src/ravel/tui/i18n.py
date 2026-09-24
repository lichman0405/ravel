"""Every word the console shows, in the two languages it shows them in.

This is the only module under `ravel.tui` that a sentence is allowed to live
in. Everything else names a message by key and calls `t`, and
`tests/unit/test_tui_i18n.py` walks the other modules' syntax trees to prove it:
a string that reads like English prose and is not in this table fails a test.

**Both languages are on one line.** `MESSAGES` maps a key to an `(english,
chinese)` pair rather than two dictionaries that have to be kept in step, so a
message cannot exist in one language and be missing from the other — the shape
of the data is the invariant, and there is no parity test to forget to write.
A translator reads the pair together, which is also how a person who speaks
both checks a translation.

**Numbered lines carry a `.one` sibling.** `t("fmt.ago.minute", count=3)` reads
`fmt.ago.minute`, and the same call with `count=1` reads `fmt.ago.minute.one`
when that key exists. English needs the distinction and Chinese does not, so the
Chinese half of a `.one` pair repeats the plural text rather than leaving a hole
— a key that exists in one language only is exactly what the table's shape is
there to prevent. Counts that are not sentences use the `(s)` convention the
console already had (`12 job(s)`), which needs no second key at all.

**The language is process state**, like a locale: `set_language` is the only
thing that changes it and the console is one process serving one person at one
terminal. It starts at `RAVEL_TUI_LANGUAGE` — `en` or `zh` — and an unrecognised
value raises rather than falling back, because a deployment that asked for a
language this program does not have should be told so at startup rather than
discovered later by whoever cannot read the screen. `F2` changes it for the
session; nothing is written to disk, because RAVEL has no user configuration
file and inventing one to remember a keystroke would be a new kind of state.

**What is deliberately not translated.** The vocabulary the rest of the system
uses to name things: node and project statuses (`RUNNING`, `WAITING_DECISION`),
review outcomes, the five agent roles, backend and service names, identifiers,
timestamps and exception text. Those words are the same words in the API, the
database and the logs, and a console that renamed them would be a console whose
reader cannot match what they see to what an operator is telling them. A
translated `RUNNING` would be worse than an English one.

**What the console sends is a term; what it says is a sentence.** A screen with
a picker on it translates the label beside each choice and sends the value
unchanged — a bench reads `契约不允许的操作` and the record keeps `action`, a
person reads `项目负责人` and the Gateway is sent `PROJECT_OWNER`. A message key
that a call site passes to a *route* rather than to a label is the one shape of
mistake this rule is here to prevent, and it is why the label and the value are
written as a pair at every such site.
"""

from __future__ import annotations

import os
from string import Formatter
from typing import Final

#: The environment variable a deployment sets to start the console in a
#: language. Read once, at import, so that `--help` is rendered in the language
#: the reader asked for as well as the screen.
LANGUAGE_ENV: Final[str] = "RAVEL_TUI_LANGUAGE"

#: What the console speaks, in the order the menu would show them.
LANGUAGES: Final[tuple[str, ...]] = ("en", "zh")

#: What it speaks when nobody said.
DEFAULT_LANGUAGE: Final[str] = "en"

#: Key → (english, chinese). See the module docstring for the rules.
MESSAGES: Final[dict[str, tuple[str, str]]] = {
    # ── The window ──────────────────────────────────────────────────────────
    "widget.panel.empty": ("Nothing to show.", "暂无内容。"),
    "widget.notice.ready": ("Ready.", "就绪。"),
    # ── Signing in ──────────────────────────────────────────────────────────
    "signin.username": ("username", "用户名"),
    "signin.password": ("password", "密码"),
    "signin.signing_in": ("Signing in…", "正在登录…"),
    # ── Keys, as the footer names them ──────────────────────────────────────
    "binding.quit": ("Quit", "退出"),
    "binding.quit_app": ("Quit", "退出"),
    "binding.next_project": ("Next project", "下一个项目"),
    # The key that switches language is named in the language it switches *to*.
    "binding.toggle_language": ("中文", "English"),
    "binding.pause": ("Pause", "暂停"),
    "binding.resume": ("Resume", "继续"),
    "binding.focus_composer": ("Message Master", "给 Master 留言"),
    "binding.membership_tab": ("Membership", "成员"),
    "binding.add_member": ("Add member", "添加成员"),
    "binding.withdraw_member": ("Withdraw member", "撤销成员"),
    "binding.new_project": ("New project", "新建项目"),
    "binding.reload": ("Refresh", "刷新"),
    "binding.upload": ("Upload", "上传"),
    "binding.report_deviation": ("Report deviation", "报告偏差"),
    # ── Table columns ───────────────────────────────────────────────────────
    "column.status": ("status", "状态"),
    "column.node": ("node", "节点"),
    "column.objective": ("objective", "目标"),
    "column.role": ("role", "角色"),
    "column.username": ("username", "用户名"),
    "column.added_by": ("added by", "添加人"),
    "column.task": ("task", "任务"),
    # ── The application ─────────────────────────────────────────────────────
    "app.no_membership": (
        "You are not a member of any project. Nothing here is yours to see.",
        "你还不是任何项目的成员，这里没有你能查看的内容。",
    ),
    "app.several_projects": (
        "Several projects. Press ctrl+n to move between them: {named}",
        "有多个项目。按 ctrl+n 在它们之间切换：{named}",
    ),
    "app.unknown_role": (
        "This program has no screen for the role {role}. "
        "That is a gap here, not a problem with your account.",
        "本程序没有为角色 {role} 准备界面。这是程序的缺口，不是你的账号有问题。",
    ),
    # ── format.py: times ────────────────────────────────────────────────────
    "fmt.ago.now": ("just now", "刚刚"),
    "fmt.ago.second": ("{count} seconds ago", "{count} 秒前"),
    "fmt.ago.second.one": ("{count} second ago", "{count} 秒前"),
    "fmt.ago.minute": ("{count} minutes ago", "{count} 分钟前"),
    "fmt.ago.minute.one": ("{count} minute ago", "{count} 分钟前"),
    "fmt.ago.hour": ("{count} hours ago", "{count} 小时前"),
    "fmt.ago.hour.one": ("{count} hour ago", "{count} 小时前"),
    "fmt.ago.day": ("{count} days ago", "{count} 天前"),
    "fmt.ago.day.one": ("{count} day ago", "{count} 天前"),
    # ── format.py: the graph ────────────────────────────────────────────────
    "fmt.nodes.empty": (
        "No nodes yet. Master has not planned this project.",
        "还没有节点。Master 尚未规划这个项目。",
    ),
    # ── format.py: attention ────────────────────────────────────────────────
    "fmt.attention.deviation": (
        "deviation on {node}: {description}",
        "节点 {node} 的偏差：{description}",
    ),
    "fmt.attention.concludable": (
        "nothing left to run — Master can conclude this project",
        "没有待运行的工作 —— Master 可以结项",
    ),
    "fmt.attention.finished": ("this project has ended", "这个项目已经结束"),
    "fmt.attention.empty": (
        "Nothing needs you. Master is working.",
        "暂时不需要你。Master 正在工作。",
    ),
    # ── format.py: execution ────────────────────────────────────────────────
    "fmt.execution.empty": (
        "Nothing has been submitted to a backend yet.",
        "还没有提交给任何后端的作业。",
    ),
    "fmt.execution.attempt": ("attempt {count}", "第 {count} 次尝试"),
    # ── format.py: the record ───────────────────────────────────────────────
    "fmt.decisions.empty": ("No decisions yet.", "还没有决策。"),
    "fmt.reviews.empty": ("No reviews yet.", "还没有评审。"),
    "fmt.reviews.line": (
        "{checkpoint} on {node} — {outcome}",
        "{node} 上的 {checkpoint} —— {outcome}",
    ),
    "fmt.research.empty": (
        "No research results yet. Nothing has been recorded as evidence.",
        "还没有研究结果，尚未记录任何证据。",
    ),
    "fmt.research.claims": ("{count} claim(s) recorded", "已记录 {count} 条主张"),
    "fmt.research.by_tier": ("by source tier:  {tiers}", "按来源层级： {tiers}"),
    "fmt.research.unsourced": (
        "    {count} of them cite no source at all",
        "    其中 {count} 条没有引用任何来源",
    ),
    "fmt.approvals.empty": ("Nothing has needed a human decision.", "尚未出现需要人来决定的事项。"),
    "fmt.approvals.needs": ("needs {role}   raised {ago}", "需要 {role}   提出于 {ago}"),
    "fmt.approvals.approved": ("approved", "已批准"),
    "fmt.approvals.refused": ("refused", "已拒绝"),
    "fmt.members.empty": ("Nobody is a member of this project.", "这个项目还没有成员。"),
    "fmt.members.creator": ("nobody — the project's first member", "无 —— 项目的首位成员"),
    "fmt.members.line": (
        "{role}  {username}   added by {by}   {ago}",
        "{role}  {username}   由 {by} 添加   {ago}",
    ),
    "fmt.members.nobody": ("nobody — the first member", "无 —— 首位成员"),
    "fmt.members.added_by": ("added by {by}", "由 {by} 添加"),
    "fmt.evidence.empty": ("No evidence recorded yet.", "还没有记录证据。"),
    "fmt.evidence.no_source": ("    no source cited", "    未引用来源"),
    # ── format.py: the bench's contract ─────────────────────────────────────
    "fmt.instruction.empty": (
        "No execution contract yet. Do not start this task.",
        "还没有执行契约。请不要开始这项任务。",
    ),
    "fmt.instruction.actions": ("allowed actions: {names}", "允许的操作：{names}"),
    "fmt.instruction.ranges": ("allowed ranges:  {names}", "允许的范围： {names}"),
    "fmt.instruction.outputs": ("required outputs: {names}", "必需产出：{names}"),
    "fmt.instruction.frozen": ("frozen {moment}", "冻结于 {moment}"),
    "fmt.none": ("none", "无"),
    "fmt.preparation.none": (
        "Nothing has been handed to a bench for this task yet.",
        "这项任务还没有交给实验台。",
    ),
    "fmt.preparation.why": (
        "The prepared package is built when the work is handed over; until "
        "then there is nothing to work from.",
        "工作包在交接时才生成；在那之前没有可依据的材料。",
    ),
    "fmt.preparation.lost": (
        "a package was handed over and is no longer readable here; what "
        "survives is the manifest, which records each file's name and hash",
        "曾交接过一个工作包，但已无法在此读取；留存下来的是清单，记录着每个文件的名字与哈希",
    ),
    "fmt.preparation.prepared_by": ("prepared by {by} {version}", "由 {by} {version} 准备"),
    "fmt.preparation.no_files": ("    the package records no files", "    工作包没有记录任何文件"),
    "fmt.preparation.sent": ("{name} — sent", "{name} —— 已提交"),
    "fmt.preparation.owed": ("{name} — still owed", "{name} —— 仍待提交"),
    "fmt.preparation.handover": (
        "handover {state} — attempt {attempt}",
        "交接 {state} —— 第 {attempt} 次尝试",
    ),
    # ── format.py: the machine ──────────────────────────────────────────────
    "fmt.harness.provider": (
        "provider {provider}   model {model}",
        "提供方 {provider}   模型 {model}",
    ),
    "fmt.harness.patched": (
        "this deployment carries patches; the pin does not",
        "这个部署带有补丁；锁定版本并没有",
    ),
    "fmt.harness.no_home": ("harness home has not been created", "harness home 尚未创建"),
    "fmt.harness.no_runtime": (
        "No runtime has been started in this process.",
        "本进程还没有启动任何 runtime。",
    ),
    "fmt.harness.pool": (
        "{runtimes} runtime(s), {sessions} session(s), {turns} turn(s)",
        "{runtimes} 个 runtime、{sessions} 个会话、{turns} 轮对话",
    ),
    "fmt.harness.scope": ("    {role} for {project}", "    {role} 用于 {project}"),
    "fmt.temporal.line": ("{host} namespace {namespace}", "{host} 命名空间 {namespace}"),
    "fmt.temporal.queue": ("task queue {queue} — {detail}", "任务队列 {queue} —— {detail}"),
    "fmt.services.empty": (
        "This deployment names no long-running services.",
        "这个部署没有配置常驻服务。",
    ),
    "fmt.services.never": ("{name} — has never reported here", "{name} —— 从未在此上报"),
    "fmt.services.stopped": (
        "{name} — stopped {when}, and not by a fault",
        "{name} —— 已于 {when} 停止，并非故障",
    ),
    "fmt.services.stale": ("STALE", "已过期"),
    "fmt.services.alive": ("alive", "在线"),
    "fmt.services.line": (
        "{name} — {note}, last spoke {seconds}s ago (budget {budget}s)",
        "{name} —— {note}，上次上报在 {seconds} 秒前（预算 {budget} 秒）",
    ),
    "fmt.services.instance": ("    {instance} since {when}", "    {instance} 自 {when} 起"),
    "fmt.services.holding": (
        "    holding {count} project(s), {mine}",
        "    持有 {count} 个项目，{mine}",
    ),
    "fmt.services.this_one": ("including this one", "包含本项目"),
    "fmt.services.not_this_one": ("not this one", "不含本项目"),
    "fmt.backends.empty": (
        "No backend has been handed any of this project's work yet.",
        "本项目的任何工作都还没有交给后端执行。",
    ),
    "fmt.backends.unknown_kind": ("unknown kind", "类型未知"),
    "fmt.backends.line": (
        "{backend} — {jobs} job(s) for {kinds}",
        "{backend} —— {jobs} 个作业，类型 {kinds}",
    ),
    "fmt.backends.slurm": ("slurm: {username}@{host}:{port}", "slurm：{username}@{host}:{port}"),
    "fmt.backends.no_slurm": ("no Slurm cluster is configured", "没有配置 Slurm 集群"),
    "fmt.backends.authentication": (
        "    authentication {authentication}",
        "    认证方式 {authentication}",
    ),
    "fmt.backends.jobs_root": (
        "    jobs root {root}   unknown host keys {policy}",
        "    作业根目录 {root}   未知主机密钥 {policy}",
    ),
    "fmt.backends.accepted": ("accepted", "接受"),
    "fmt.backends.refused": ("refused", "拒绝"),
    "fmt.backends.reachability": (
        "    reachability: {reachability} — {why}",
        "    可达性：{reachability} —— {why}",
    ),
    "fmt.jobs.empty": (
        "No backend job has ever been submitted for this project.",
        "这个项目从未提交过后端作业。",
    ),
    "fmt.jobs.open": (
        "{node}  {state} on {backend} attempt {attempt}",
        "{node}  {state} 于 {backend}，第 {attempt} 次尝试",
    ),
    "fmt.jobs.backend_says": ("    the backend says: {state}", "    后端给出的状态：{state}"),
    # `{failure}` is either empty or a bracketed class, because a language is
    # free to put the bracket somewhere the English does not.
    "fmt.jobs.ended": (
        "{node}  {state} on {backend}{failure}  ended {when}",
        "{node}  {state} 于 {backend}{failure}  结束于 {when}",
    ),
    "fmt.jobs.by_class": ("failures by class:  {classes}", "按类别统计的失败： {classes}"),
    "fmt.recon.empty": (
        "No run has had to be recovered in this project.",
        "这个项目还没有需要恢复的运行。",
    ),
    "fmt.recon.total": ("{count} run(s) recovered", "已恢复 {count} 次运行"),
    "fmt.recon.by_class": ("by class:  {classes}", "按类别： {classes}"),
    "fmt.recon.by_probe": ("the probe said:  {observations}", "探测结果： {observations}"),
    "fmt.recon.record": (
        "{observed}  {node}  {before} → {after}",
        "{observed}  {node}  {before} → {after}",
    ),
    "fmt.recon.workflow": ("    workflow {workflow}  {when}", "    工作流 {workflow}  {when}"),
    "fmt.runtime.nodes": ("nodes {total}   {counted}", "节点 {total}   {counted}"),
    "fmt.runtime.jobs": (
        "backend jobs {total}, of which {unfinished} unfinished",
        "后端作业 {total} 个，其中 {unfinished} 个未完成",
    ),
    # ── The owner's screen ──────────────────────────────────────────────────
    "owner.panel.master": ("Master", "Master"),
    "owner.panel.approvals": ("Approvals", "审批事项"),
    "owner.panel.attention": ("Attention required", "需要你处理"),
    "owner.panel.execution": ("Execution", "执行"),
    "owner.panel.dag": ("Scientific DAG", "科学 DAG"),
    "owner.panel.decisions": ("Decisions", "决策"),
    "owner.panel.reviews": ("Reviews", "评审"),
    "owner.panel.research": ("Research results", "研究结果"),
    "owner.panel.evidence": ("Evidence", "证据"),
    "owner.panel.members": ("Who is in this project", "项目成员"),
    "owner.tab.decisions": ("Decisions", "决策"),
    "owner.tab.reviews": ("Reviews", "评审"),
    "owner.tab.research": ("Research", "研究"),
    "owner.tab.evidence": ("Evidence", "证据"),
    "owner.tab.membership": ("Membership", "成员"),
    "owner.placeholder.member": ("username to add", "要添加的用户名"),
    "owner.select.as": ("as", "角色"),
    "owner.placeholder.new_project": ("title for a new project", "新项目的名称"),
    "owner.placeholder.composer": (
        "Ask Master, or tell it something.",
        "向 Master 提问，或告知它一些事情。",
    ),
    "owner.role.lab_user": ("Lab user — works the bench", "实验台用户 —— 在实验台操作"),
    "owner.role.project_owner": (
        "Project owner — may direct this project",
        "项目负责人 —— 可以指挥本项目",
    ),
    "owner.role.admin": (
        "Administrator — runtime only, no science",
        "管理员 —— 只管运行时，不管科研",
    ),
    "owner.notice.stream_lost": (
        "Lost the live stream ({error}); retrying.",
        "与实时连接断开（{error}），正在重试。",
    ),
    "owner.conversation.empty": (
        "Nothing said yet. Say something below.",
        "还没有对话。在下面说点什么。",
    ),
    "owner.conversation.master": ("Master", "Master"),
    "owner.conversation.you": ("you", "你"),
    "owner.notice.asking": ("Asking Master…", "正在询问 Master…"),
    "owner.notice.answered": ("Master answered.", "Master 已回复。"),
    "owner.notice.no_answer": ("Master did not answer: {error}", "Master 没有回应：{error}"),
    "owner.notice.paused": ("Paused.", "已暂停。"),
    "owner.notice.resumed": ("Resumed.", "已继续。"),
    "owner.notice.name_somebody": ("Name somebody to add.", "请填写要添加的用户名。"),
    "owner.notice.choose_role": ("Choose the role to give them.", "请选择要授予的角色。"),
    "owner.notice.adding": ("Adding {username} as {role}…", "正在将 {username} 添加为 {role}…"),
    "owner.notice.not_added": ("{username} was not added: {error}", "{username} 未能添加：{error}"),
    "owner.notice.now_role": (
        "{username} is now {role} here.",
        "{username} 现在是本项目的 {role}。",
    ),
    "owner.notice.select_member": (
        "Select the member to withdraw in the table first.",
        "请先在表格中选中要撤销的成员。",
    ),
    "owner.notice.withdrawing": ("Withdrawing {username}…", "正在撤销 {username}…"),
    "owner.notice.not_withdrawn": (
        "{username} was not withdrawn: {error}",
        "{username} 未能撤销：{error}",
    ),
    "owner.notice.withdrawn": (
        "{username} no longer holds a role here; the record of the withdrawal stays.",
        "{username} 在本项目中的角色已撤销；撤销记录会保留。",
    ),
    "owner.notice.name_project": (
        "Name the new project in the box, then press n again.",
        "请先在输入框中填写新项目名称，再按一次 n。",
    ),
    "owner.notice.opening": ("Opening {title}…", "正在创建 {title}…"),
    "owner.notice.not_opened": ("{title} was not opened: {error}", "{title} 未能创建：{error}"),
    "owner.notice.opened": (
        "Opened {display_id} ({title}). Press ctrl+n to move to it.",
        "已创建 {display_id}（{title}）。按 ctrl+n 切换过去。",
    ),
    "owner.notice.membership_tab": (
        "Membership is on its own tab — press m to open it.",
        "成员管理在单独的标签页 —— 按 m 打开。",
    ),
    "owner.notice.refused": ("{command} was refused: {error}", "{command} 被拒绝：{error}"),
    "owner.dag.role": ("your role: {role}", "你的角色：{role}"),
    # The objective a project opened from the console is given. It is *authored*
    # text that lands in the database as the project's own objective, so it is
    # written in the language of the person who opened it.
    "owner.new_project.objective": (
        "Opened from the console; no objective has been stated yet.",
        "从控制台创建；尚未声明目标。",
    ),
    # ── The bench's screen ──────────────────────────────────────────────────
    "lab.heading.tasks": ("Your tasks", "你的任务"),
    "lab.tab.instruction": ("Instruction", "作业说明"),
    "lab.tab.prepared": ("Prepared", "交接内容"),
    "lab.tab.status": ("Status", "状态"),
    "lab.tab.messages": ("Messages", "消息"),
    "lab.tab.reported": ("Reported", "已上报"),
    "lab.panel.contract": ("Contract", "契约"),
    "lab.panel.prepared": ("What was handed over", "交接了什么"),
    "lab.panel.status": ("Status", "状态"),
    "lab.panel.messages": ("Messages", "消息"),
    "lab.panel.reported": ("What was reported", "已上报的内容"),
    "lab.panel.upload": ("Send a result", "提交结果"),
    "lab.panel.deviation": ("Report a deviation", "报告偏差"),
    "lab.placeholder.path": ("path to the file to send", "要提交的文件路径"),
    "lab.placeholder.output": ("answers which output", "对应哪项产出"),
    "lab.placeholder.kind": ("what happened", "发生了什么"),
    "lab.placeholder.action": ("the action that was not permitted", "不被允许的操作"),
    "lab.placeholder.description": ("what the bench actually needs", "实验台实际需要什么"),
    "lab.deviation.action": ("An action the contract does not allow", "契约不允许的操作"),
    "lab.deviation.parameter": ("A parameter outside the allowed range", "超出允许范围的参数"),
    "lab.deviation.substitution": (
        "A substitution the contract does not permit",
        "契约不允许的替换",
    ),
    "lab.deviation.stop": ("A stop condition the contract does not name", "契约未列出的停止条件"),
    "lab.deviation.other": ("Something else", "其他情况"),
    "lab.notice.no_tasks": (
        "No experiment tasks in this project yet. Master has not planned any.",
        "这个项目还没有实验任务。Master 尚未规划。",
    ),
    "lab.notice.reading": ("Reading again…", "正在重新读取…"),
    "lab.notice.uptodate": ("Up to date.", "已是最新。"),
    "lab.notice.no_task": (
        "No task is selected, so there is nothing to attach this to.",
        "没有选中任务，无法关联这次提交。",
    ),
    "lab.notice.name_file": ("Name a file to send.", "请填写要提交的文件。"),
    "lab.notice.choose_output": (
        "Say which required output this file answers.",
        "请选择这个文件对应哪项必需产出。",
    ),
    "lab.notice.not_a_file": (
        "{named} is not a file this machine can read.",
        "{named} 不是本机可读的文件。",
    ),
    "lab.notice.unreadable": ("could not read {named}: {error}", "无法读取 {named}：{error}"),
    "lab.notice.sending": (
        "Sending {file} as {output} ({bytes} bytes)…",
        "正在提交 {file} 作为 {output}（{bytes} 字节）…",
    ),
    "lab.notice.upload_refused": (
        "the Gateway refused the upload: {error}",
        "Gateway 拒绝了这次上传：{error}",
    ),
    "lab.notice.sent": ("Sent {file} as {output}.", "已提交 {file} 作为 {output}。"),
    "lab.notice.still_owed": (" Still owed: {missing}.", " 仍待提交：{missing}。"),
    "lab.notice.everything_owed": (" That was everything owed.", " 应交的产出都已提交。"),
    "lab.notice.run_not_told": (
        " The run has not been told, so its wait is still open.",
        " 运行尚未收到通知，它的等待仍然开放。",
    ),
    "lab.notice.no_task_selected": ("No task is selected.", "没有选中任务。"),
    "lab.notice.deviation_needs": (
        "A deviation needs both the action and what the bench needs.",
        "报告偏差需要同时填写操作和实验台的实际需要。",
    ),
    "lab.notice.reporting": ("Reporting…", "正在上报…"),
    "lab.notice.report_refused": (
        "the Gateway refused the report: {error}",
        "Gateway 拒绝了这次上报：{error}",
    ),
    "lab.notice.reported": (
        "Reported. Master decides what happens next.",
        "已上报。接下来由 Master 决定。",
    ),
    "lab.instruction.contract": (
        "working under contract {contract} v{version}",
        "依据契约 {contract} v{version} 工作",
    ),
    "lab.instruction.no_contract": ("no contract is bound to this task", "这项任务还没有绑定契约"),
    "lab.instruction.no_job": (
        "no backend job has been submitted for this task",
        "这项任务还没有提交后端作业",
    ),
    "lab.instruction.backend": (
        "backend {backend} attempt {attempt} — {state}",
        "后端 {backend} 第 {attempt} 次尝试 —— {state}",
    ),
    "lab.instruction.backend_says": ("the backend says: {state}", "后端给出的状态：{state}"),
    "lab.instruction.failure": ("failure class {failure}", "失败类别 {failure}"),
    "lab.messages.empty": (
        "The Worker has not sent anything about this task.",
        "Worker 尚未就这项任务发送任何消息。",
    ),
    "lab.messages.outside": ("  [not in contract]", "  [超出契约]"),
    "lab.reported.empty": (
        "Nothing has been reported against this task.",
        "这项任务还没有任何上报。",
    ),
    "lab.output.owed": ("{name} — still owed", "{name} —— 仍待提交"),
    "lab.output.sent": ("{name} — already sent", "{name} —— 已提交"),
    # ── The administrator's screen ──────────────────────────────────────────
    "admin.panel.services": ("Processes", "进程"),
    "admin.panel.harness": ("Harness", "Harness"),
    "admin.panel.temporal": ("Temporal", "Temporal"),
    "admin.panel.backends": ("Backends", "后端"),
    "admin.panel.jobs": ("Jobs", "作业"),
    "admin.panel.reconciliation": ("Recovered runs", "已恢复的运行"),
    "admin.panel.runtime": ("This project", "本项目"),
    "admin.panel.logs": ("Where the logs are", "日志位置"),
    "admin.notice.no_logs": (
        "This deployment has not been told where its logs are.",
        "这个部署没有配置日志路径。",
    ),
    "admin.notice.read": ("Runtime health read.", "已读取运行时健康状态。"),
    # ── The command line ────────────────────────────────────────────────────
    "cli.description": (
        "The RAVEL console. Reads and controls; decides nothing.",
        "RAVEL 控制台。可读、可控，但不做决定。",
    ),
    "cli.url": (
        "the Gateway to talk to (default: %(default)s)",
        "要连接的 Gateway（默认：%(default)s）",
    ),
    "cli.project": ("open this project instead of asking", "直接打开这个项目，不再询问"),
    "cli.username": ("sign in without the form", "跳过登录表单，直接登录"),
    "cli.password": (
        "sign in without the form (or set {env})",
        "跳过登录表单，直接登录（或设置 {env}）",
    ),
}


class UnknownLanguage(ValueError):
    """A language this console does not speak was asked for."""


class UnknownMessage(KeyError):
    """A message key that is not in `MESSAGES`.

    Raised rather than answered with the key itself: a console that shows
    `owner.notice.paused` where a sentence belongs has a bug that reads as a
    rendering glitch, and one that raises says which key is missing.
    """


def catalog(code: str) -> dict[str, str]:
    """Every message in one language.

    Args:
        code: A language from `LANGUAGES`.

    Raises:
        UnknownLanguage: if `code` is not one this console speaks.
    """
    cleaned = code.strip().lower() if isinstance(code, str) else ""
    if cleaned not in LANGUAGES:
        raise UnknownLanguage(
            f"{code!r} is not a language this console has; it has {', '.join(LANGUAGES)}"
        )
    column = LANGUAGES.index(cleaned)
    return {key: pair[column] for key, pair in MESSAGES.items()}


#: Built once, because a lookup that rebuilt a dictionary per message would be
#: a catalogue read on every line of every panel.
CATALOGS: Final[dict[str, dict[str, str]]] = {code: catalog(code) for code in LANGUAGES}


def _chosen_at_startup() -> str:
    """The language `RAVEL_TUI_LANGUAGE` names, or the default."""
    asked = os.environ.get(LANGUAGE_ENV, "").strip()
    return set_language(asked) if asked else DEFAULT_LANGUAGE


def current() -> str:
    """The language the console is speaking now."""
    return _LANGUAGE


def set_language(code: str) -> str:
    """Switch to `code` and return it.

    Raises:
        UnknownLanguage: if `code` is not one this console speaks. Nothing is
            changed when it raises, so a bad value cannot leave the console
            half-switched.
    """
    global _LANGUAGE
    catalog(code)  # validates before anything is assigned
    _LANGUAGE = code.strip().lower()
    return _LANGUAGE


def toggle() -> str:
    """Switch to the next language in `LANGUAGES`, and return it."""
    here = LANGUAGES.index(_LANGUAGE)
    return set_language(LANGUAGES[(here + 1) % len(LANGUAGES)])


def t(key: str, /, **fields: object) -> str:
    """The message `key` in the current language, with `fields` filled in.

    A `.one` sibling is used instead when `count` is exactly 1 and such a key
    exists — see the module docstring.

    Raises:
        UnknownMessage: if there is no such key. A missing message is a bug in
            this program, not something a reader should see the key of.
    """
    wanted = key
    if fields.get("count") == 1 and f"{key}.one" in MESSAGES:
        wanted = f"{key}.one"
    try:
        message = CATALOGS[_LANGUAGE][wanted]
    except KeyError:
        raise UnknownMessage(wanted) from None
    return message.format(**fields) if fields else message


def fields_in(message: str) -> frozenset[str]:
    """The `{names}` a message asks for.

    Exists for the test that asserts both languages ask for the same ones: a
    translation that drops a placeholder is a line that silently loses a fact.
    """
    return frozenset(name for _, name, _, _ in Formatter().parse(message) if name)


#: The language in force. Assigned here, at the bottom of the module, because
#: `set_language` is what the environment variable is resolved through and it
#: has to exist before anything is read.
_LANGUAGE: str = _chosen_at_startup()


__all__ = [
    "CATALOGS",
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "LANGUAGE_ENV",
    "MESSAGES",
    "UnknownLanguage",
    "UnknownMessage",
    "catalog",
    "current",
    "fields_in",
    "set_language",
    "t",
    "toggle",
]
