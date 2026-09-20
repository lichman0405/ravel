#!/usr/bin/env bash
#
# Start RAVEL V0 on one machine.
#
#   scripts/run_v0.sh                    infrastructure, Gateway, worker, TUI
#   scripts/run_v0.sh --project <id>     the same, and drive that project
#   scripts/run_v0.sh --no-tui           the same without the console (a server)
#
# Four processes, and the order matters only in that the Gateway and the worker
# both need the database to be reachable. Everything else about them is
# independent: the Gateway serves the console, the worker executes nodes, and a
# project's loop asks Master and Review what to decide. Stopping this script
# stops the three it started; the containers are `scripts/dev_down.sh`'s.
#
# Nothing here is a supervisor. It is a development and single-host deployment
# convenience: if a child dies, this reports the exit and stops the rest rather
# than restarting into a state nobody is watching.
#
# A project is not started by this script unless `--project` names one, because
# which projects run is an operator's decision and there is no scheduler in V0.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

WITH_TUI=1
PROJECT=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-tui) WITH_TUI=0 ;;
        --project) PROJECT="${2:?--project needs an identifier}"; shift ;;
        *) printf 'usage: %s [--no-tui] [--project <id>]\n' "$0" >&2; exit 2 ;;
    esac
    shift
done

[[ -x "$REPO_ROOT/.venv/bin/python" ]] \
    || { printf '\033[31m✗\033[0m .venv missing; run scripts/bootstrap_ubuntu.sh\n' >&2; exit 1; }
[[ -f "$REPO_ROOT/.env" ]] \
    || { printf '\033[31m✗\033[0m .env missing; copy .env.example and fill it in\n' >&2; exit 1; }

set -a
# shellcheck disable=SC1091
. "$REPO_ROOT/.env"
set +a

LOGS="$REPO_ROOT/runtime/logs"
mkdir -p "$LOGS"

# The children's stdout goes to a file, which Python block-buffers: a service
# that is killed before it writes 8 KiB would leave a log with none of its
# startup output in it, which is exactly the output somebody reads a log for.
export PYTHONUNBUFFERED=1

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# The children this script started, so the trap stops what it began and nothing
# else. `set -u` and an empty array are the reason for the `${arr[@]+...}` form.
PIDS=()

stop_children() {
    trap - EXIT INT TERM
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do
        kill "$pid" 2>/dev/null || true
    done
    for pid in ${PIDS[@]+"${PIDS[@]}"}; do
        wait "$pid" 2>/dev/null || true
    done
}
trap stop_children EXIT INT TERM

start() {  # start <name> <command...>
    local name="$1"; shift
    "$@" >"$LOGS/$name.log" 2>&1 &
    PIDS+=("$!")
    pass "$name (pid $!, log: runtime/logs/$name.log)"
}

step "Infrastructure"
scripts/dev_up.sh >/dev/null
pass "PostgreSQL, Temporal and MinIO are up"

step "Migrations"
"$REPO_ROOT/.venv/bin/alembic" upgrade head >/dev/null
pass "schema at head"

step "Services"
if [[ -z "${DEEPSEEK_API_KEY:-}" ]]; then
    warn "DEEPSEEK_API_KEY is empty: Master and Review cannot take a turn, so a"
    warn "project will not plan or review. Everything else below still starts."
fi

start gateway "$REPO_ROOT/.venv/bin/uvicorn" ravel.gateway.app:create_app --factory \
    --host "${RAVEL_GATEWAY_HOST:-127.0.0.1}" --port "${RAVEL_GATEWAY_PORT:-8000}"
start worker "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/run_temporal_worker.py"

if [[ -n "$PROJECT" ]]; then
    start project "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/run_project.py" \
        --project "$PROJECT"
fi

printf '\n\033[1mRAVEL V0 is up.\033[0m\n'
printf '  Gateway   http://%s:%s\n' "${RAVEL_GATEWAY_HOST:-127.0.0.1}" "${RAVEL_GATEWAY_PORT:-8000}"
printf '  Temporal  http://127.0.0.1:%s\n' "${RAVEL_TEMPORAL_UI_PORT:-8088}"
printf '  logs      runtime/logs/\n'
if [[ -n "$PROJECT" ]]; then
    printf '  driving   project %s\n' "$PROJECT"
else
    printf '\n  drive a project with:\n'
    printf '    .venv/bin/python scripts/run_project.py --project <project_id>\n'
fi
printf '\n'

if [[ $WITH_TUI -eq 1 ]]; then
    step "Console"
    printf '  quitting the console stops the services this script started\n\n'
    "$REPO_ROOT/.venv/bin/python" -m ravel.tui || true
else
    printf '  Ctrl-C to stop\n\n'
    wait -n || true
fi
