"""Turning Gateway responses into lines of text, with no widget in sight.

Every function here is pure and takes exactly what a route returns, which is
what makes the screens easy to read and this module easy to test: a screen says
*which* facts it wants and in *what order*, and the question of how a timestamp
is abbreviated or a node is summarised is answered once, here.

The house style is the dense console `docs/08` §4 asks for. A row is a status
glyph, a short identifier, and the thing itself — no box drawing, no alignment
theatre. Two rules do most of the work: nothing is padded to a fixed width
(a terminal that is 80 columns wide and one that is 200 should both look
right), and empty is a sentence rather than a blank, because a blank panel
reads as a bug.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ravel.tui.tokens import symbol_for

#: How much of a long objective, decision or message to show before eliding.
#: Generous, because these panels are the whole point of the screen and a
#: person reading one should not have to open a second window.
LINE = 160


def elide(text: str, width: int = LINE) -> str:
    """One line, no newlines, and not longer than `width`.

    Newlines are collapsed because every caller is drawing into a single-line
    widget, and a `\\n` that reaches a `Label` is either a layout surprise or a
    silently truncated sentence depending on the widget. Collapsing it is the
    honest version of the same thing.
    """
    flat = " ".join(text.split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def moment(value: Any) -> str:
    """An ISO timestamp as something a person can read at a glance.

    Local time, because a person comparing it against their own clock is the
    only reason to show it at all. Returns the input unchanged if it is not a
    timestamp — a formatter that raises on unexpected data turns a display bug
    into a crash.
    """
    if not isinstance(value, str) or not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:19]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M")


def ago(value: Any, *, now: datetime | None = None) -> str:
    """How long ago, coarsely. For "is this still moving" and nothing finer."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    seconds = ((now or datetime.now(UTC)) - parsed).total_seconds()
    if seconds < 0:
        return "just now"
    for limit, unit, size in (
        (90, "second", 1),
        (5400, "minute", 60),
        (172800, "hour", 3600),
    ):
        if seconds < limit:
            count = int(seconds // size)
            return f"{count} {unit}{'s' if count != 1 else ''} ago"
    return f"{int(seconds // 86400)} days ago"


def status_line(status: str, label: str = "") -> str:
    """The glyph and the status, which is what a status column is."""
    return f"{symbol_for(status)} {label or status.upper()}"


# ── The DAG ─────────────────────────────────────────────────────────────────


def node_row(node: dict[str, Any]) -> tuple[str, str, str]:
    """A node as (status, identifier, objective).

    Three fields rather than one string because the screen colours the first,
    dims the second and shows the third in full — and a formatter that had
    already joined them would make the screen take them apart again.
    """
    return (
        str(node.get("status", "")),
        str(node.get("display_id") or node.get("node_id", ""))[:12],
        elide(str(node.get("objective", ""))),
    )


def node_summary(nodes: list[dict[str, Any]]) -> str:
    """How many of each status, worst first.

    Worst first because the reason to look at a summary is to find out whether
    anything needs attention, and a summary that opens with `PLANNED 12` makes
    the reader do the sorting.
    """
    if not nodes:
        return "No nodes yet. Master has not planned this project."
    order = [
        "FAILED",
        "BLOCKED",
        "PARTIAL",
        "WAITING_DECISION",
        "REVIEWING",
        "RUNNING",
        "WAITING_EXTERNAL",
        "READY",
        "PLANNED",
        "PASSED",
        "CANCELLED",
    ]
    counts: dict[str, int] = {}
    for node in nodes:
        status = str(node.get("status", ""))
        counts[status] = counts.get(status, 0) + 1
    def described(status: str, count: int) -> str:
        return f"{symbol_for(status)} {status} {count}"

    parts = [described(status, counts[status]) for status in order if status in counts]
    # Anything this program does not have an order for is still counted, at
    # the end, rather than dropped for not being in a list written here.
    parts += [described(status, count) for status, count in counts.items() if status not in order]
    return "   ".join(parts)


# ── Attention ───────────────────────────────────────────────────────────────


def attention_lines(projection: dict[str, Any], nodes: list[dict[str, Any]]) -> list[str]:
    """What wants a person, in the order they would want it.

    The facts come from two places and the split is deliberate. Which
    deviations are still open, whether the project can be concluded and whether
    it has ended are the Gateway's to say — each is decided by a rule Master
    enforces, and a second opinion here would be a console that could disagree
    with the runtime about whether an escalation had been answered.

    The stalled nodes are derived instead, because the projection cannot answer
    this one. `ProjectAudit.unfinished` is the nodes that have not *ended*, and
    a FAILED node has ended — terminally, with no outgoing edge in the
    transition table, which is what the acceptance freeze is for. So a failure
    is absent from that list and present here, and it is the case this panel
    exists for.
    """
    lines: list[str] = []

    stalled = [
        node
        for node in nodes
        if str(node.get("status", "")) in {"FAILED", "BLOCKED", "PARTIAL"}
    ]
    for node in stalled:
        lines.append(
            f"{symbol_for(str(node['status']))} {node.get('display_id', '')} "
            f"{elide(str(node.get('objective', '')), 100)}"
        )

    for deviation in projection.get("open_deviations", []):
        lines.append(
            f"{symbol_for('WAITING_DECISION')} deviation on {deviation.get('node_id', '')}: "
            f"{elide(str(deviation.get('description', '')), 90)}"
        )

    if projection.get("is_concludable"):
        lines.append(
            f"{symbol_for('READY')} nothing left to run — Master can conclude "
            "this project"
        )
    if projection.get("is_finished"):
        lines.append(f"{symbol_for('COMPLETED')} this project has ended")

    return lines or ["Nothing needs you. Master is working."]


def execution_lines(nodes: list[dict[str, Any]], executions: list[dict[str, Any]]) -> list[str]:
    """What is running, and what it is running on.

    The join is by node, because a backend job is a fact about a node and a
    person reading this panel is asking "where is my work".
    """
    if not executions:
        return ["Nothing has been submitted to a backend yet."]

    by_node = {str(node.get("node_id", "")): node for node in nodes}
    lines = []
    for job in executions:
        node = by_node.get(str(job.get("node_id", "")), {})
        state = str(job.get("state", ""))
        parts = [
            symbol_for(state),
            str(node.get("display_id") or job.get("node_id", "")),
            f"{state} on {job.get('backend', '?')}",
        ]
        if job.get("attempt", 1) > 1:
            parts.append(f"attempt {job['attempt']}")
        if job.get("failure_class"):
            # The class is why an operator is looking at this row at all, so it
            # goes on the line rather than behind a detail view.
            parts.append(elide(str(job["failure_class"]), 60))
        lines.append("  ".join(parts))
    return lines


# ── The record ──────────────────────────────────────────────────────────────


def decision_lines(decisions: list[dict[str, Any]]) -> list[str]:
    """Master's decisions, newest last, each with what it was about."""
    if not decisions:
        return ["No decisions yet."]
    return [
        f"{moment(decision.get('created_at'))}  {decision.get('decision_type', '')}  "
        f"{elide(str(decision.get('rationale', '')), 110)}"
        for decision in decisions
    ]


def review_lines(reviews: list[dict[str, Any]]) -> list[str]:
    """Verdicts, with the criteria that decided them.

    Every criterion is shown, satisfied or not, because a review that passed
    four of five is the case a person most needs to see and a summary that
    showed only the failures would look identical to one that failed outright.
    """
    if not reviews:
        return ["No reviews yet."]
    lines = []
    for review in reviews:
        outcome = str(review.get("outcome", ""))
        lines.append(
            f"{symbol_for(outcome)} {review.get('checkpoint', '')} on "
            f"{review.get('node_id', '')} — {outcome}"
        )
        for result in review.get("criterion_results", []):
            mark = symbol_for("PASSED" if result.get("satisfied") else "FAILED")
            observed = str(result.get("observed", ""))
            suffix = f"  ({elide(observed, 60)})" if observed else ""
            lines.append(f"    {mark} {elide(str(result.get('statement', '')), 110)}{suffix}")
    return lines


def research_lines(evidence: list[dict[str, Any]]) -> list[str]:
    """What the research found, as a count per class, worst-supported first.

    This is the *shape* of the evidence rather than the evidence, and the two
    are separate panels for a reason: the count per class answers "how much of
    this rests on something anybody checked" at a glance, which is the question
    somebody asks before reading, and the Evidence panel is where they read.

    `HYPOTHESIS` is listed before `FACT` and the ordering is deliberate: the
    interesting number on this panel is how much of the answer is not yet
    supported, and a list that led with the facts would bury it.
    """
    if not evidence:
        return ["No research results yet. Nothing has been recorded as evidence."]

    by_class: dict[str, int] = {}
    by_tier: dict[str, int] = {}
    unsourced = 0
    for row in evidence:
        claim = row.get("evidence", {})
        by_class[str(claim.get("claim_class", "?"))] = (
            by_class.get(str(claim.get("claim_class", "?")), 0) + 1
        )
        by_tier[str(claim.get("source_tier", "?"))] = (
            by_tier.get(str(claim.get("source_tier", "?")), 0) + 1
        )
        if not row.get("sources"):
            unsourced += 1

    lines = [f"{len(evidence)} claim(s) recorded"]
    for name, count in sorted(by_class.items(), key=lambda pair: pair[0] != "HYPOTHESIS"):
        lines.append(f"    {name}: {count}")
    lines.append("by source tier:  " + "   ".join(f"{k} {v}" for k, v in sorted(by_tier.items())))
    if unsourced:
        # A `FACT` cannot rest on nothing and a `HYPOTHESIS` may, so this is a
        # count and not a fault — but it is the number that says how much of
        # the answer nobody has supported, which is why it is on the screen.
        lines.append(f"    {unsourced} of them cite no source at all")
    return lines


def approval_lines(approvals: list[dict[str, Any]]) -> list[str]:
    """Questions waiting on a human, with the ones still open first.

    An open approval is the whole reason an owner opens this panel — it is the
    one state where the runtime has stopped and is waiting for them — so the
    open ones lead, and a resolved one stays on the screen afterwards rather
    than disappearing, because "who answered this and what did they say" is
    asked immediately after.
    """
    if not approvals:
        return ["Nothing has needed a human decision."]
    open_now = [item for item in approvals if str(item.get("status", "")) == "PENDING"]
    resolved = [item for item in approvals if str(item.get("status", "")) != "PENDING"]

    lines: list[str] = []
    for approval in open_now:
        lines.append(
            f"{symbol_for('WAITING_DECISION')} {elide(str(approval.get('question', '')), 120)}"
        )
        lines.append(
            f"    needs {approval.get('required_role', '?')}   "
            f"raised {ago(approval.get('requested_at'))}"
        )
    for approval in resolved:
        answer = "approved" if approval.get("approved") else "refused"
        lines.append(
            f"{symbol_for('PASSED' if approval.get('approved') else 'FAILED')} "
            f"{elide(str(approval.get('question', '')), 100)} — {answer}"
        )
        if approval.get("note"):
            lines.append(f"    {elide(str(approval['note']), 120)}")
    return lines


def member_lines(members: list[dict[str, Any]]) -> list[str]:
    """Who is in this project, and who put them there.

    `granted_by` is on every line and that is the point of the panel: authority
    in RAVEL is conferred rather than assumed, so "who gave this person their
    role" is the question that follows "why can they do that". A membership
    nobody granted is the project's creator and says so.
    """
    if not members:
        return ["Nobody is a member of this project."]
    lines = []
    for member in members:
        granter = member.get("granted_by")
        # The project's creator is the one membership nobody conferred, and
        # saying so is better than an empty field that reads as a missing name.
        by = str(granter) if granter else "nobody — the project's first member"
        lines.append(
            f"{member.get('role', '?')}  {member.get('username', '?')}"
            f"   added by {by}   {ago(member.get('granted_at'))}"
        )
    return lines


def member_row(member: dict[str, Any]) -> tuple[str, str, str]:
    """A member as (role, username, who put them here).

    The same three facts as `member_lines`, split rather than joined because
    the owner screen colours the role and puts the cursor on the username. The
    granter's line is built the same way in both, including the sentence for a
    membership nobody conferred — a blank there would read as a missing name
    rather than as the project's first member.
    """
    granter = member.get("granted_by")
    by = str(granter) if granter else "nobody — the first member"
    return (
        str(member.get("role", "?")),
        str(member.get("username", "?")),
        f"added by {by}",
    )


def evidence_lines(evidence: list[dict[str, Any]]) -> list[str]:
    """What the research found, and where it came from.

    Each row of the route's answer is a claim *and* the sources it cites, and
    both halves go on the screen: the statement is what Master may decide on,
    and the reference is what makes it checkable. §5's whole claim about
    research being real is that a person can go and look, so a source's URL or
    DOI is printed rather than a count of sources.

    A claim with no source says so. That is a `HYPOTHESIS` legitimately — the
    domain allows one to rest on nothing — but a FACT cannot, so an empty
    source list here means the reader is looking at a claim nobody has
    supported yet, which is worth knowing.
    """
    if not evidence:
        return ["No evidence recorded yet."]
    lines = []
    for row in evidence:
        claim = row.get("evidence", {})
        lines.append(
            f"[{claim.get('claim_class', '?')} {claim.get('source_tier', '?')}] "
            f"{elide(str(claim.get('statement', '')), 120)}"
        )
        sources = row.get("sources", [])
        if not sources:
            lines.append("    no source cited")
            continue
        for source in sources:
            reference = source.get("doi") or source.get("url") or source.get("source_id", "")
            lines.append(
                f"    {elide(str(source.get('title', '')), 50)}  {reference}  "
                f"{source.get('access_status', '')}"
            )
    return lines


# ── The lab ─────────────────────────────────────────────────────────────────


def instruction_lines(task: dict[str, Any]) -> list[str]:
    """The approved procedure, the allowed actions and what must come back.

    The contract is the instruction — there is no second record — so a task
    whose contract has not been written says so rather than showing an empty
    procedure, which would read as "nothing to do".
    """
    contract = task.get("instruction")
    if not contract:
        return ["No execution contract yet. Do not start this task."]
    lines = [
        elide(str(contract.get("objective", ""))),
        "",
        f"allowed actions: {', '.join(contract.get('allowed_actions', [])) or 'none'}",
    ]
    ranges = contract.get("allowed_ranges") or {}
    lines.append(
        "allowed ranges:  "
        + (", ".join(f"{key} {value}" for key, value in ranges.items()) if ranges else "none")
    )
    lines.append(f"required outputs: {', '.join(contract.get('required_outputs', [])) or 'none'}")
    if contract.get("frozen_at"):
        lines.append(f"frozen {moment(contract['frozen_at'])}")
    return lines


def lab_task_row(task: dict[str, Any]) -> tuple[str, str, str]:
    """A task as (status, identifier, objective)."""
    node = task.get("node", {})
    return (
        str(node.get("status", "")),
        str(node.get("display_id", "")),
        elide(str(node.get("objective", ""))),
    )


def preparation_lines(task: dict[str, Any]) -> list[str]:
    """What the bench was handed: the package, its documents, what it still owes.

    The documents are listed by the name the manifest recorded and never by a
    directory listing, so what is on this screen is what RAVEL wrote. The
    required outputs are split into what has arrived and what has not, and the
    split is the point of the panel: a bench that has sent two of three files
    needs to see which one is missing without counting a list.

    A task with no handover says so and says what that means — the package is
    built when work is handed over, so a task that is planned but not yet
    given to anybody is a task nobody should be working on. That is a different
    state from a package that was built and has been lost, which is answered by
    the package being absent while the handover is present.
    """
    handover = task.get("handover")
    if not handover:
        return [
            "Nothing has been handed to a bench for this task yet.",
            "The prepared package is built when the work is handed over; until "
            "then there is nothing to work from.",
        ]

    record = handover.get("handover") or {}
    lines: list[str] = []
    package = handover.get("package")
    if package is None:
        lines.append(
            f"{symbol_for('BLOCKED')} a package was handed over and is no longer "
            "readable here; what survives is the manifest, which records each "
            "file's name and hash"
        )
    else:
        lines.append(
            f"prepared by {package.get('materializer', '?')} "
            f"{package.get('materializer_version', '')}"
        )
        for document in package.get("documents", []):
            lines.append(
                f"    {elide(str(document.get('name', '')), 60)}  "
                f"{str(document.get('sha256', ''))[:12]}"
            )
        if not package.get("documents"):
            lines.append("    the package records no files")

    answered = list(handover.get("answered_outputs", []))
    missing = list(handover.get("missing_outputs", []))
    lines.append("")
    lines.append(
        "required outputs: "
        + (", ".join(answered + missing) if answered or missing else "none")
    )
    for name in answered:
        lines.append(f"    {symbol_for('PASSED')} {name} — sent")
    for name in missing:
        # The artifact identifier is what a person quotes when asking about the
        # file; the fact that it is missing is what they do about it.
        lines.append(f"    {symbol_for('WAITING_EXTERNAL')} {name} — still owed")

    state = str(record.get("state", ""))
    if state:
        lines.append("")
        lines.append(f"handover {state} — attempt {record.get('attempt', '?')}")
    return lines


# ── Admin ───────────────────────────────────────────────────────────────────


def harness_lines(health: dict[str, Any], runtime: dict[str, Any]) -> list[str]:
    """The harness, and whether anything is actually running on it.

    `pool_started: false` renders as a sentence, not as three zeroes, because
    "the Gateway has not reached the harness" and "the harness is idle" are
    different facts and only one of them is worth waking up for.
    """
    lines = [
        f"{health.get('name', '?')} {health.get('tag', '')} "
        f"({str(health.get('commit', ''))[:12]})",
        f"provider {health.get('provider', '?')}   model {health.get('model', '?')}",
    ]
    if health.get("patches_required"):
        lines.append(f"{symbol_for('FAILED')} this deployment carries patches; the pin does not")
    if not health.get("home_exists", False):
        lines.append(f"{symbol_for('BLOCKED')} harness home has not been created")

    pool = runtime.get("harness", {})
    if not pool.get("pool_started"):
        lines.append("No runtime has been started in this process.")
    else:
        lines.append(
            f"{symbol_for('RUNNING')} {pool.get('live_runtimes', 0)} runtime(s), "
            f"{pool.get('live_sessions', 0)} session(s), {pool.get('total_turns', 0)} turn(s)"
        )
        for scope in pool.get("scopes", []):
            lines.append(f"    {scope.get('role', '?')} for {scope.get('project_id', '?')}")
    return lines


def temporal_lines(health: dict[str, Any]) -> list[str]:
    """Whether the queue is up, said plainly. A down service is information."""
    mark = symbol_for("RUNNING") if health.get("reachable") else symbol_for("FAILED")
    return [
        f"{mark} {health.get('host', '?')} namespace {health.get('namespace', '?')}",
        f"task queue {health.get('task_queue', '?')} — {health.get('detail', '')}",
    ]


def service_lines(health: dict[str, Any]) -> list[str]:
    """Which long-running processes are reporting, and how long they have been quiet.

    Three states, and the screen keeps them apart because an operator does
    different things about each. A service that is reporting and fresh is fine.
    A service that reported and has gone quiet is a process that died, and the
    row names the `host:pid` that is no longer answering. A service that has
    *never* reported is not a fault on this screen — a deployment that has not
    started a worker has not lost one — and rendering it as a failure would
    send somebody looking for a crash that never happened.

    `holds_this_project` is on each line rather than only in the header,
    because "the supervisor is up" and "the supervisor is up and has never
    heard of my project" are different answers, and the second is the one worth
    restarting something over.
    """
    services = health.get("services", [])
    if not services:
        return ["This deployment names no long-running services."]
    lines = []
    for service in services:
        name = str(service.get("service", "?"))
        if not service.get("reporting"):
            lines.append(f"{symbol_for('PLANNED')} {name} — has never reported here")
            continue
        if service.get("stale"):
            mark, note = symbol_for("FAILED"), "STALE"
        else:
            mark, note = symbol_for("RUNNING"), "alive"
        seconds = float(service.get("silent_for_seconds") or 0.0)
        lines.append(
            f"{mark} {name} — {note}, last spoke {seconds:.0f}s ago "
            f"(budget {float(service.get('silence_budget_seconds') or 0):.0f}s)"
        )
        lines.append(
            f"    {service.get('instance', '?')} "
            f"since {moment(service.get('started_at'))}"
        )
        held = int(service.get("projects_held") or 0)
        mine = "including this one" if service.get("holds_this_project") else "not this one"
        lines.append(f"    holding {held} project(s), {mine}")
    return lines


def backend_lines(health: dict[str, Any]) -> list[str]:
    """What the work has actually run on, and how the cluster is configured.

    Two halves that answer to different evidence and say so. The first is read
    from `backend_jobs`, which is what happened; the second is read from
    configuration, which is what was set up. The route's own sentence about
    reachability is printed verbatim at the bottom rather than summarised,
    because it is the one thing on this panel a reader might otherwise assume
    had been tested.
    """
    used = health.get("used", [])
    lines: list[str] = []
    if not used:
        lines.append("No backend has been handed any of this project's work yet.")
    for backend in used:
        states = "  ".join(
            f"{state} {count}" for state, count in (backend.get("states") or {}).items()
        )
        kinds = ", ".join(backend.get("node_types", [])) or "unknown kind"
        lines.append(
            f"{backend.get('backend', '?')} — {backend.get('jobs', 0)} job(s) for {kinds}"
        )
        lines.append(f"    {states}")

    slurm = health.get("slurm", {})
    lines.append("")
    if slurm.get("configured"):
        lines.append(
            f"{symbol_for('READY')} slurm: {slurm.get('username', '?')}@"
            f"{slurm.get('host', '?')}:{slurm.get('port', '?')}"
        )
    else:
        lines.append(f"{symbol_for('PLANNED')} no Slurm cluster is configured")
    lines.append(f"    authentication {slurm.get('authentication', '?')}")
    lines.append(
        f"    jobs root {slurm.get('jobs_root', '?')}   "
        f"unknown host keys {'accepted' if slurm.get('trust_unknown_host') else 'refused'}"
    )
    lines.append(f"    reachability: {slurm.get('reachability', '')} — {slurm.get('why', '')}")
    return lines


def job_lines(jobs: dict[str, Any]) -> list[str]:
    """Backend jobs, the open ones first, each naming the node it is for.

    Open before ended, because this panel is read to find out what is happening
    now; the ended ones are there for the question that follows, which is what
    happened to the one that is not moving any more. A failure class is printed
    when there is one, since it is the reason an operator opened this panel.
    """
    open_now = jobs.get("open", [])
    ended = jobs.get("ended", [])
    if not open_now and not ended:
        return ["No backend job has ever been submitted for this project."]

    lines: list[str] = []
    for job in open_now:
        lines.append(
            f"{symbol_for(str(job.get('state', '')))} {job.get('display_id') or job.get('node_id')}"
            f"  {job.get('state', '')} on {job.get('backend', '?')} "
            f"attempt {job.get('attempt', '?')}"
        )
        if job.get("backend_state"):
            lines.append(f"    the backend says: {elide(str(job['backend_state']), 100)}")
    for job in ended:
        failure = job.get("failure_class")
        lines.append(
            f"{symbol_for(str(job.get('state', '')))} {job.get('display_id') or job.get('node_id')}"
            f"  {job.get('state', '')} on {job.get('backend', '?')}"
            + (f"  [{failure}]" if failure else "")
            + f"  ended {moment(job.get('ended_at'))}"
        )
        if job.get("detail"):
            lines.append(f"    {elide(str(job['detail']), 120)}")

    classes = jobs.get("failed_by_class") or {}
    if classes:
        lines.append("")
        lines.append(
            "failures by class:  " + "   ".join(f"{k} {v}" for k, v in classes.items())
        )
    return lines


def reconciliation_lines(recovered: dict[str, Any]) -> list[str]:
    """Runs RAVEL found dead, and what it did with the node.

    A project with none is the healthy answer, and it is printed as a sentence
    rather than an empty list, because "nothing has ever gone wrong here" is
    the answer somebody came to this panel for.

    `observed` leads each line because it is the distinction the route exists
    to preserve: a run that was *found* gone is a recovery, and a run Temporal
    could not be asked about is a probe that failed, and only one of the two is
    evidence that the run died.
    """
    total = int(recovered.get("total") or 0)
    if total == 0:
        return ["No run has had to be recovered in this project."]
    lines = [f"{total} run(s) recovered"]
    by_class = recovered.get("by_class") or {}
    if by_class:
        lines.append("by class:  " + "   ".join(f"{k} {v}" for k, v in by_class.items()))
    by_observation = recovered.get("by_observation") or {}
    if by_observation:
        lines.append(
            "the probe said:  " + "   ".join(f"{k} {v}" for k, v in by_observation.items())
        )
    lines.append("")
    for record in recovered.get("recent", []):
        lines.append(
            f"{symbol_for(str(record.get('failure_class', '')))} "
            f"{record.get('observed', '?')}  {record.get('node_id', '')[:12]}  "
            f"{record.get('node_status_before', '?')} → {record.get('node_status_after', '?')}"
        )
        lines.append(
            f"    workflow {str(record.get('workflow_id', ''))[:40]}  "
            f"{moment(record.get('created_at'))}"
        )
        if record.get("detail"):
            lines.append(f"    {elide(str(record['detail']), 120)}")
    return lines


def runtime_lines(runtime: dict[str, Any]) -> list[str]:
    """Node and job counts, and where the logs are."""
    nodes = runtime.get("nodes", {})
    jobs = runtime.get("backend_jobs", {})
    counted = "  ".join(
        f"{status} {count}" for status, count in (nodes.get("by_status") or {}).items()
    )
    lines = [
        f"nodes {nodes.get('total', 0)}   {counted}",
        f"backend jobs {jobs.get('total', 0)}, of which {jobs.get('unfinished', 0)} unfinished",
    ]
    logs = runtime.get("logs", {})
    for name, path in logs.items():
        lines.append(f"{name.replace('_', ' ')}: {path}")
    return lines


__all__ = [
    "LINE",
    "ago",
    "approval_lines",
    "attention_lines",
    "backend_lines",
    "decision_lines",
    "elide",
    "evidence_lines",
    "execution_lines",
    "harness_lines",
    "instruction_lines",
    "job_lines",
    "lab_task_row",
    "member_lines",
    "member_row",
    "moment",
    "node_row",
    "node_summary",
    "preparation_lines",
    "reconciliation_lines",
    "research_lines",
    "review_lines",
    "runtime_lines",
    "service_lines",
    "status_line",
    "temporal_lines",
]
