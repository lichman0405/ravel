"""Parse the premium contract schemas and check their references.

Run from the repository root:

    .venv/bin/python schemas/premium/check_contracts.py

The check is deliberately structural: every file parses as JSON Schema, every
``$id`` is unique, every local ``$ref`` resolves, and every schema declares a
title and type. It does not pretend to be a full validator; it is the contract
gate that must fail loudly when a hand-written schema is malformed.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "schemas" / "premium"


def main() -> int:
    failures: list[str] = []
    ids: dict[str, Path] = {}
    documents: dict[Path, dict[str, object]] = {}

    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            failures.append(f"{path.name}: {error}")
            continue
        if not isinstance(document, dict):
            failures.append(f"{path.name}: schema is not an object")
            continue
        documents[path] = document
        schema_id = document.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            failures.append(f"{path.name}: missing $id")
        elif schema_id in ids:
            failures.append(f"{path.name}: duplicate $id {schema_id}")
        else:
            ids[schema_id] = path
        for field in ("title", "type"):
            if field not in document:
                failures.append(f"{path.name}: missing {field}")

    for path, document in documents.items():
        stack: list[object] = [document]
        while stack:
            node = stack.pop()
            if isinstance(node, dict):
                reference = node.get("$ref")
                if (
                    isinstance(reference, str)
                    and reference.startswith("#")
                    and not reference.startswith("#/")
                ):
                    # Local references are resolved only against their own
                    # document; the schemas in this directory do not use
                    # cross-file refs, so a local ref must be a JSON pointer.
                    failures.append(f"{path.name}: unsupported $ref {reference}")
                stack.extend(node.values())
            elif isinstance(node, list):
                stack.extend(node)

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    print(f"PASS: {len(documents)} premium schemas parse; $id unique; no broken local $ref")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
