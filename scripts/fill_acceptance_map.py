"""Fill the T04-T29 acceptance map from the frozen task cards.

Run from the T03 worktree:  .venv/bin/python /tmp/fill_acceptance_map.py
It preserves the already-executed T00-T02 entries and every existing AC id.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PREMIUM = ROOT / "docs" / "premium"
SPECS = json.loads((PREMIUM / "TASKS.json").read_text(encoding="utf-8"))
MAP_PATH = PREMIUM / "acceptance-map.json"
MAP = json.loads(MAP_PATH.read_text(encoding="utf-8"))

DB_TASKS = {
    "T04", "T05", "T06", "T07", "T08", "T09", "T10", "T11", "T12",
    "T13", "T15", "T16", "T17", "T18", "T19", "T20", "T21", "T23",
}
EXTERNAL_TASKS = {"T24", "T25", "T26", "T27", "T28", "T29"}
DOC_TASKS = {"T03", "T14", "T22"}

FULLWIDTH_COLON = chr(0xFF1A)
AC_PATTERN = re.compile(
    r"\*\*(T\d+-AC\d+)\*\*(?:" + FULLWIDTH_COLON + r"|:)\s*(.+)"
)
TITLE_PATTERN = re.compile(r"^#\s+T\d+\s+—\s+(.+)$", re.MULTILINE)


def card_text(task: dict[str, object]) -> str:
    return (PREMIUM / str(task["card"])).read_text(encoding="utf-8")


def acceptance_from_card(task: dict[str, object]) -> list[dict[str, str]]:
    text = card_text(task)
    found = []
    for ac_id, wording in AC_PATTERN.findall(text):
        found.append({"id": ac_id, "text": wording.strip()})
    return found


for task in SPECS["tasks"]:
    task_id = task["id"]
    if task_id in {"T00", "T01", "T02"}:
        continue

    row = MAP["tasks"].setdefault(task_id, {})
    existing_ids = list(row.get("acceptance_ids", []))
    acs = acceptance_from_card(task)
    ids = [ac["id"] for ac in acs]
    if existing_ids and set(existing_ids) != set(ids):
        raise SystemExit(
            f"{task_id}: card AC ids differ from acceptance-map: "
            f"{existing_ids} vs {ids}"
        )
    row["acceptance_ids"] = ids or existing_ids

    classification = "external" if task_id in EXTERNAL_TASKS else "internal"
    row["classification"] = classification
    row["status"] = "PLANNED"
    row["database_suite"] = task_id in DB_TASKS
    row["acceptance"] = [
        {
            "id": ac["id"],
            "type": (
                "EXTERNAL"
                if task_id in EXTERNAL_TASKS
                else "MANUAL"
                if task_id in DOC_TASKS
                else "TEST"
            ),
            "expected_evidence": ac["text"],
        }
        for ac in acs
    ]

    if task_id in EXTERNAL_TASKS:
        row["test_nodeids"] = []
        row["manual_evidence_refs"] = [
            f"premium-live TASK={task_id}",
            f"tasklogs/{task_id}.md",
        ]
        row["execution_commands"] = [
            {
                "argv": ["make", "premium-live", f"TASK={task_id}"],
                "allow_skips": True,
                "needs_database": False,
                "external": True,
            }
        ]
    elif task_id == "T03":
        row["test_nodeids"] = []
        row["manual_evidence_refs"] = [
            "CONTRACTS.md",
            "schemas/premium",
            "acceptance-map.json",
            "ADR-001.md",
        ]
        row["execution_commands"] = [
            {
                "argv": ["git", "diff", "--check"],
                "allow_skips": False,
                "needs_database": False,
                "external": False,
            },
            {
                "argv": ["python", "docs/premium/tools/tracker.py", "validate"],
                "allow_skips": False,
                "needs_database": False,
                "external": False,
            },
        ]
    else:
        row["test_nodeids"] = [f"tests/premium/{task_id}"]
        row["manual_evidence_refs"] = []
        row["execution_commands"] = [
            {
                "argv": [
                    ".venv/bin/python",
                    "-m",
                    "pytest",
                    f"tests/premium/{task_id}",
                    "-q",
                    "-rxXs",
                ],
                "allow_skips": False,
                "needs_database": task_id in DB_TASKS,
                "external": False,
            }
        ]
    row["fill_rule"] = (
        "T03 frozen plan: each AC has a unique id, a type, and expected evidence; "
        "internal tests must be concrete nodeids before a task can PASS."
    )

MAP_PATH.write_text(json.dumps(MAP, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("updated", len(MAP["tasks"]), "tasks")
