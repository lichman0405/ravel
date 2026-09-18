#!/usr/bin/env bash
#
# Real-Internet research acceptance.
#
#   scripts/test_live_research.sh
#
# This suite is never mocked. It reaches the real Internet, opens real sources,
# and asserts that every registered Evidence row carries a retrievable URL, a
# real retrieval timestamp, and a content hash matching the stored bytes.
#
# Per START_PROMPT.md section 3, a search result is only a lead: the suite fails
# if Evidence was registered without the original source having been opened.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d .venv ]]; then
    printf '\033[31m✗\033[0m .venv missing; run scripts/bootstrap_ubuntu.sh first\n' >&2
    exit 1
fi
# shellcheck disable=SC1091
. .venv/bin/activate

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

printf '\n\033[1m── connectivity ──\033[0m\n'
python - <<'PY'
import sys
import httpx

# Keyless real sources. If none is reachable there is no legitimate way to run
# this suite, and mocking is not an acceptable substitute.
SOURCES = [
    ("Crossref", "https://api.crossref.org/works?rows=1"),
    ("OpenAlex", "https://api.openalex.org/works?per-page=1"),
    ("arXiv", "https://export.arxiv.org/api/query?max_results=1"),
]
reachable = []
for name, url in SOURCES:
    try:
        response = httpx.get(url, timeout=15.0, follow_redirects=True)
        ok = response.status_code == 200
        print(f"  {'✓' if ok else '✗'} {name}: HTTP {response.status_code}")
        if ok:
            reachable.append(name)
    except Exception as exc:
        print(f"  ✗ {name}: {type(exc).__name__}: {exc}")

if not reachable:
    sys.exit("  no real research source reachable; refusing to run mocked")
print(f"  {len(reachable)} source(s) reachable")
PY

printf '\n\033[1m── live research ──\033[0m\n'
pytest tests/live_research -m live "$@"

printf '\n\033[32m✓ live research suite passed\033[0m\n\n'
