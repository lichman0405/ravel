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
    "attention_lines",
    "decision_lines",
    "elide",
    "evidence_lines",
    "execution_lines",
    "harness_lines",
    "instruction_lines",
    "lab_task_row",
    "moment",
    "node_row",
    "node_summary",
    "review_lines",
    "runtime_lines",
    "status_line",
    "temporal_lines",
]
