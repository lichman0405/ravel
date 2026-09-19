"""The two rules `format.py` states, and the joins that are easy to get wrong.

The module is pure and takes exactly what a route returns, which makes it the
one part of the console that can be checked without a Gateway, a database or a
terminal — and the part where being wrong is quietest. A panel that renders
`satisfied` as absent, or a claim whose sources are read from the wrong key,
produces a screen that looks finished and says nothing. Two of these functions
were written against guessed field names and both were wrong; the tests below
are what would have caught them.

The two rules, from the docstring:

* **Nothing is padded to a fixed width.** A terminal 80 columns wide and one
  200 columns wide should both look right.
* **Empty is a sentence rather than a blank**, because a blank panel reads as a
  bug. Every formatter that can have nothing to show is asserted to say so.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from ravel.tui.format import (
    LINE,
    attention_lines,
    decision_lines,
    elide,
    evidence_lines,
    execution_lines,
    harness_lines,
    instruction_lines,
    lab_task_row,
    moment,
    node_row,
    node_summary,
    review_lines,
    runtime_lines,
    temporal_lines,
)
from ravel.tui.tokens import symbol_for


def rendered(answer: str | list[str]) -> str:
    """A formatter's answer as the panel would show it.

    The two return types are both in use — a summary is one line and a panel is
    several — so the empty check below needs them flattened to the same thing
    before it can ask its question of either.
    """
    return "\n".join([answer] if isinstance(answer, str) else answer)


# ── The two rules ───────────────────────────────────────────────────────────


def test_nothing_is_padded_to_a_fixed_width() -> None:
    """A short objective stays short, whatever the terminal is.

    The alternative — padding every row to one width — is how a console grows
    a column of trailing spaces that wraps on a narrow terminal and makes every
    line look like it ends in a different place on a wide one.
    """
    row = node_row({"display_id": "n-1", "status": "READY", "objective": "Mix it."})
    assert row[2] == "Mix it."


def test_a_long_line_is_elided_and_says_so() -> None:
    """The ellipsis is the point: a truncated sentence states that it is one."""
    cut = elide("x" * 500)
    assert len(cut) == LINE
    assert cut.endswith("…")


def test_a_newline_never_reaches_a_single_line_widget() -> None:
    """Collapsed rather than kept, because the widgets are one line each.

    A `\\n` reaching a `Label` is a layout surprise and reaching a `Static` is a
    silently truncated sentence, so the two renderings of the same record would
    disagree depending on which widget drew it.
    """
    assert elide("first\nsecond") == "first second"
    assert elide("a\tb\r\nc") == "a b c"


@pytest.mark.parametrize(
    "empty",
    [
        rendered(node_summary([])),
        rendered(execution_lines([], [])),
        rendered(decision_lines([])),
        rendered(review_lines([])),
        rendered(evidence_lines([])),
        rendered(instruction_lines({})),
    ],
    ids=["summary", "execution", "decisions", "reviews", "evidence", "instruction"],
)
def test_an_empty_panel_is_a_sentence_not_a_blank(empty: str) -> None:
    """Every formatter that can have nothing to show says something.

    Joined first and asserted over the lines rather than over the list, because
    `[""]` is a one-element list that renders as exactly the blank panel this
    rule exists to forbid — a `len()` check would pass it.
    """
    assert empty.strip(), "an empty panel reads as a bug"
    assert all(line.strip() for line in empty.splitlines()), "a blank line inside a panel"


# ── The joins ───────────────────────────────────────────────────────────────


def test_a_node_row_is_the_three_fields_the_table_needs() -> None:
    """Status, identifier, objective — in that order, because the table colours
    the first, dims the second and shows the third in full."""
    row = node_row({"display_id": "node-0007", "status": "RUNNING", "objective": "Run it."})
    assert row == ("RUNNING", "node-0007", "Run it.")


def test_a_node_row_falls_back_to_the_identifier_it_has() -> None:
    """`display_id` is derived and `node_id` is not; a node missing the first is
    still a node, and the identifier column is twelve characters of whichever
    one it has."""
    row = node_row({"node_id": "a-very-long-node-identifier", "status": "READY"})
    assert row[1] == "a-very-long-"


def test_a_summary_leads_with_what_is_wrong() -> None:
    """Worst first, because the reason to read a summary is to find the problem.

    A summary that opened with `PLANNED 12` makes the reader do the sorting,
    and the reader is usually doing it in a hurry.
    """
    nodes = [
        {"status": "PLANNED"},
        {"status": "PASSED"},
        {"status": "FAILED"},
        {"status": "PLANNED"},
    ]
    summary = node_summary(nodes)
    assert summary.index("FAILED") < summary.index("PLANNED") < summary.index("PASSED")
    assert "PLANNED 2" in summary


def test_a_summary_counts_a_status_it_has_no_order_for() -> None:
    """Dropped would be worse than last: a node nobody counted is a node nobody
    looks for, and the counting is done from the nodes rather than the list."""
    assert "SOMETHING_NEW 1" in node_summary([{"status": "SOMETHING_NEW"}, {"status": "FAILED"}])


def test_attention_shows_a_failure_even_though_the_projection_calls_it_ended() -> None:
    """The reason this panel derives its stalled list rather than reading one.

    `ProjectAudit.unfinished` is the nodes that have not ended, and a FAILED
    node has ended terminally — so a panel that read only that list would be
    silent about the one row a person most needs to see. The failure is present
    here with the deviation that explains it.
    """
    lines = attention_lines(
        {"open_deviations": []},
        [
            {"status": "PASSED", "display_id": "n-1", "objective": "Done."},
            {"status": "FAILED", "display_id": "n-2", "objective": "The control drifted."},
            {"status": "RUNNING", "display_id": "n-3", "objective": "Under way."},
        ],
    )
    assert len(lines) == 1
    assert "n-2" in lines[0]
    assert "The control drifted." in lines[0]


def test_attention_puts_what_has_stopped_ahead_of_what_is_waiting_on_a_person() -> None:
    """Ordered by kind, and that is the only ordering performed here.

    Within a kind the nodes keep the DAG's own sequence rather than being
    ranked again: somebody reading this panel and the graph above it is looking
    at the same order, and a second ranking invented in this module would be
    one more thing able to disagree with the record. So the blocked node stays
    ahead of the failed one, and both stay ahead of the deviation.
    """
    lines = attention_lines(
        {"open_deviations": [{"node_id": "n-3", "description": "needs a ruling"}]},
        [
            {"status": "BLOCKED", "display_id": "n-1", "objective": "Waiting on a permit."},
            {"status": "FAILED", "display_id": "n-2", "objective": "The control drifted."},
        ],
    )
    assert len(lines) == 3
    assert "Waiting on a permit." in lines[0]
    assert "The control drifted." in lines[1]
    assert "n-3" in lines[2]


def test_attention_says_so_when_nothing_does() -> None:
    """The sentence a person reads on a healthy project, and the reason the
    panel is never blank."""
    assert attention_lines({}, [{"status": "RUNNING", "display_id": "n-1"}]) == [
        "Nothing needs you. Master is working."
    ]


def test_attention_prefers_the_projection_over_a_second_opinion() -> None:
    """The list a person reads and the list Master resumes from are one list.

    `attention_lines` reads `open_deviations` from the projection the Gateway
    computed rather than re-deriving them from the nodes, so the deviation
    reaches the screen even though the node it names is running normally and
    would be invisible to a check written against node status.
    """
    lines = attention_lines(
        {"open_deviations": [{"node_id": "n-9", "description": "the range was wrong"}]},
        [{"status": "RUNNING", "display_id": "n-9", "objective": "Run it."}],
    )
    assert any("deviation on n-9" in line for line in lines)
    assert any("the range was wrong" in line for line in lines)


def test_execution_joins_a_job_to_its_node() -> None:
    """A backend job is a fact about a node, and the reader is asking "where is
    my work" — so the row names the node and not only the job."""
    lines = execution_lines(
        [{"node_id": "n-1", "display_id": "node-0001"}],
        [{"node_id": "n-1", "state": "RUNNING", "backend": "mock-compute", "attempt": 2}],
    )
    assert len(lines) == 1
    assert "node-0001" in lines[0]
    assert "RUNNING on mock-compute" in lines[0]
    assert "attempt 2" in lines[0]


def test_a_review_shows_every_criterion_satisfied_or_not() -> None:
    """A review that passed four of five is the case a person most needs.

    The two field names are the point: `CriterionResult` carries `satisfied`,
    `statement` and `observed`, and this function was written against `passed`
    and `criterion` and drew every criterion as unsatisfied and unnamed.
    """
    lines = review_lines(
        [
            {
                "outcome": "NEEDS_MORE",
                "checkpoint": "PRE_RUN",
                "node_id": "n-1",
                "criterion_results": [
                    {"satisfied": True, "statement": "The control is named.", "observed": "yes"},
                    {"satisfied": False, "statement": "The range is stated."},
                ],
            }
        ]
    )
    assert "NEEDS_MORE" in lines[0]
    # The mark is the fifth character: four spaces of indent, then the glyph.
    # Compared against the two symbols rather than against each other, so the
    # test says which one is which rather than only that they differ.
    assert lines[1][4] == symbol_for("PASSED")
    assert lines[2][4] == symbol_for("FAILED")
    assert "The control is named." in lines[1]
    assert "yes" in lines[1]
    assert "The range is stated." in lines[2]


def test_evidence_prints_the_reference_and_not_a_count() -> None:
    """§5's whole claim about research being real is that a person can go and
    look, so the DOI or the URL is on the screen.

    `doi or url or source_id` in that order: a DOI is the stable one, a URL is
    the one a browser takes, and a source id is the last resort for a record
    that has neither.
    """
    lines = evidence_lines(
        [
            {
                "evidence": {
                    "claim_class": "FACT",
                    "source_tier": "PRIMARY",
                    "statement": "The 2019 series reports 15%.",
                },
                "sources": [
                    {
                        "title": "A paper",
                        "doi": "10.1000/xyz",
                        "url": "https://example.invalid/a",
                        "access_status": "OPEN",
                    },
                    {"title": "A page", "url": "https://example.invalid/b"},
                ],
            }
        ]
    )
    assert "[FACT PRIMARY]" in lines[0]
    assert "10.1000/xyz" in lines[1]
    assert "https://example.invalid/b" in lines[2]


def test_a_claim_with_no_source_says_so() -> None:
    """A `HYPOTHESIS` may rest on nothing, and the reader is entitled to know
    that this is one — so the absence is stated rather than rendered as a gap."""
    lines = evidence_lines(
        [{"evidence": {"claim_class": "HYPOTHESIS", "statement": "It might be boron."}}]
    )
    assert "no source cited" in lines[1]


def test_an_unbound_contract_tells_the_bench_not_to_start() -> None:
    """A node Master has planned but not equipped is a task nobody should run.

    An empty procedure would read as "nothing to do", which is the opposite of
    what an unbound contract means.
    """
    assert instruction_lines({"instruction": None}) == [
        "No execution contract yet. Do not start this task."
    ]
    assert instruction_lines({}) == ["No execution contract yet. Do not start this task."]


def test_an_instruction_states_the_ranges_and_the_frozen_moment() -> None:
    """The ranges are what a bench works inside, and the frozen moment is what
    makes the instruction an instruction rather than a draft."""
    lines = instruction_lines(
        {
            "instruction": {
                "objective": "Measure it.",
                "allowed_actions": ["run_measurement"],
                "allowed_ranges": {"temperature_c": "18..24"},
                "required_outputs": ["conductivity.csv"],
                "frozen_at": "2026-09-19T09:30:00+00:00",
            }
        }
    )
    assert lines[0] == "Measure it."
    assert "allowed actions: run_measurement" in lines[2]
    assert "temperature_c 18..24" in lines[3]
    assert "required outputs: conductivity.csv" in lines[4]
    assert lines[5].startswith("frozen ")


def test_an_instruction_with_no_ranges_says_none_rather_than_nothing() -> None:
    """An empty range list is a contract that permits no parameter at all.

    A blank there reads as "the ranges were not shown", which is a different
    statement from "there are none" — and the second is the one a bench has to
    act on.
    """
    lines = instruction_lines({"instruction": {"objective": "Measure it.", "allowed_actions": []}})
    assert "allowed actions: none" in lines[2]
    assert "allowed ranges:  none" in lines[3]
    assert "required outputs: none" in lines[4]
    assert not any(line.startswith("frozen") for line in lines)


def test_a_lab_task_row_is_the_node_not_the_contract() -> None:
    """The list shows what the task *is*; the panel below shows what it is
    under. Reading the contract's objective into the row would make the two
    views agree about the wrong thing."""
    row = lab_task_row(
        {
            "node": {"display_id": "node-0003", "status": "READY", "objective": "Run the series."},
            "instruction": {"objective": "A different sentence."},
        }
    )
    assert row == ("READY", "node-0003", "Run the series.")


# ── The admin's three panels ────────────────────────────────────────────────


def test_an_unstarted_pool_is_a_sentence_and_not_three_zeroes() -> None:
    """`pool_started: false` and "a pool with nothing in it" are different facts.

    Three zeroes render identically for both, and only one of them is worth
    waking up for — so the first gets a sentence and the second gets numbers.
    """
    harness = {"name": "DeepSeek Harness", "tag": "dsh-v0.1.5-rc.1", "home_exists": True}
    unstarted = harness_lines(harness, {"harness": {"pool_started": False}})
    assert "No runtime has been started in this process." in unstarted

    running = harness_lines(
        harness,
        {
            "harness": {
                "pool_started": True,
                "live_runtimes": 2,
                "live_sessions": 1,
                "total_turns": 9,
                "scopes": [{"role": "MASTER", "project_id": "p-1"}],
            }
        },
    )
    assert any("2 runtime(s)" in line for line in running)
    assert any("MASTER for p-1" in line for line in running)


def test_a_deployment_carrying_patches_says_so() -> None:
    """The one field whose being true means this is not the build that was
    verified, which is worth a line rather than a field."""
    lines = harness_lines(
        {"name": "DeepSeek Harness", "patches_required": True, "home_exists": True},
        {"harness": {}},
    )
    assert any("carries patches" in line for line in lines)

    lines = harness_lines({"name": "DeepSeek Harness", "home_exists": False}, {"harness": {}})
    assert any("harness home has not been created" in line for line in lines)


def test_a_down_temporal_is_reported_and_not_raised() -> None:
    """A health panel that cannot say "down" is a panel that is only readable
    when it has nothing to say."""
    lines = temporal_lines(
        {
            "host": "localhost:7233",
            "namespace": "default",
            "task_queue": "ravel",
            "reachable": False,
        }
    )
    assert lines[0].startswith(symbol_for("FAILED"))
    assert "task queue ravel" in lines[1]

    up = temporal_lines({"host": "h", "namespace": "n", "task_queue": "q", "reachable": True})
    assert up[0].startswith(symbol_for("RUNNING"))


def test_runtime_counts_and_the_log_paths() -> None:
    """Where to look when the numbers say something is wrong.

    Paths rather than contents: the Gateway has no business reading a log into
    a response, and a screen that made somebody go and find the path has stopped
    one step short.
    """
    lines = runtime_lines(
        {
            "nodes": {"total": 3, "by_status": {"READY": 2, "PASSED": 1}},
            "backend_jobs": {"total": 4, "unfinished": 1},
            "logs": {"dsh_home": "/srv/dsh", "session_logs": "/srv/dsh/sessions"},
        }
    )
    assert "nodes 3" in lines[0]
    assert "READY 2" in lines[0]
    assert "backend jobs 4, of which 1 unfinished" in lines[1]
    assert "dsh home: /srv/dsh" in lines[2]
    assert "session logs: /srv/dsh/sessions" in lines[3]


# ── Time, which is the one formatter with a fallback for nonsense ───────────


def test_a_timestamp_becomes_something_readable() -> None:
    """Shape rather than a fixed date, because `moment` renders in local time.

    A test that pinned `2026-09-19` would pass here and fail for a reader in
    another zone, which is a test reporting on the machine rather than on the
    code. What is actually promised is a minute-resolution local timestamp.
    """
    # Parsed back rather than indexed: this asserts the documented format
    # exactly, and a reader in any zone gets the same answer out of it.
    datetime.strptime(moment("2026-09-19T12:00:00+00:00"), "%Y-%m-%d %H:%M")
    # A naive timestamp is read as UTC rather than refused.
    datetime.strptime(moment("2026-09-19T12:00:00"), "%Y-%m-%d %H:%M")


@pytest.mark.parametrize("value", [None, "", 7, {"not": "a time"}, "not a time"])
def test_a_non_timestamp_does_not_raise(value: Any) -> None:
    """A formatter that raises on unexpected data turns a display bug into a
    crash, and this module exists so that a display bug is only a display bug."""
    assert isinstance(moment(value), str)
