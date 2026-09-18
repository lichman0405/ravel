#!/usr/bin/env bash
#
# Unified RAVEL V0 test entry point.
#
#   scripts/test_all.sh              unit + integration (needs dev_up.sh)
#   scripts/test_all.sh --unit       unit only, no services required
#   scripts/test_all.sh --all        everything, including live research and e2e
#
# The live research suite reaches the real Internet and is never mocked. It is
# excluded from the default run only because it is slow, never to avoid it.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ ! -d .venv ]]; then
    printf '\033[31m✗\033[0m .venv missing; run scripts/bootstrap_ubuntu.sh first\n' >&2
    exit 1
fi
# shellcheck disable=SC1091
. .venv/bin/activate

MODE="default"
case "${1:-}" in
    --unit) MODE="unit" ;;
    --all)  MODE="all" ;;
    "")     ;;
    *) printf 'usage: %s [--unit|--all]\n' "$0" >&2; exit 2 ;;
esac

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

banner() { printf '\n\033[1m── %s ──\033[0m\n' "$1"; }

banner "lint"
ruff check src tests

case "$MODE" in
    unit)
        banner "unit tests"
        pytest tests/unit -m "not live"
        ;;
    default)
        banner "unit tests"
        pytest tests/unit
        banner "integration tests"
        pytest tests/integration tests/dsh
        ;;
    all)
        banner "unit tests"
        pytest tests/unit
        banner "integration tests"
        pytest tests/integration tests/dsh
        banner "live research"
        pytest tests/live_research -m live
        banner "end-to-end"
        pytest tests/e2e
        banner "acceptance A01-A20"
        pytest tests/acceptance -m acceptance
        ;;
esac

printf '\n\033[32m✓ %s suite passed\033[0m\n\n' "$MODE"
