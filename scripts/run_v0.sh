#!/usr/bin/env bash
#
# Start RAVEL V0 on one machine.
#
#   scripts/run_v0.sh                    infrastructure, Gateway, worker, supervisor, TUI
#   scripts/run_v0.sh --project <id>     the same, but drive only that project
#   scripts/run_v0.sh --no-tui           the same without the console (a server)
#
# Four processes by default, and the order matters only in that the Gateway and
# the Temporal worker both need the database to be reachable. Everything else
# about them is independent: the Gateway serves the console, the Temporal
# Execution Worker hosts the activities, the supervisor discovers and drives
# active projects, and the TUI is the console a person sits at.
#
# **The console is a client, not the thing that runs.** Closing it leaves the
# supervisor, the Gateway and the Temporal worker up, which is Phase 10's
# headline: a person creates a project and walks away from it. Ctrl-C is what
# stops a deployment — it takes down the children this script started, and
# nothing else; the containers are `scripts/dev_down.sh`'s.
#
# With `--project` the script drives a single named project through
# `scripts/run_project.py`, which is a debugging entry point. Without
# `--project` it starts the unattended `scripts/run_supervisor.py`, which
# discovers every active project and drives each one until it ends.

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

# Through `run_gateway.py` rather than `uvicorn` directly, so that this
# launcher and `infra/systemd/ravel-gateway.service` start the Gateway the same
# way: the entry point configures logging for the service — uvicorn's own
# `log_config` replaces the root handler, which would drop the `service` field
# from every line — and it beats the heartbeat the administrator's screen reads.
start gateway "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/run_gateway.py" \
    --host "${RAVEL_GATEWAY_HOST:-127.0.0.1}" --port "${RAVEL_GATEWAY_PORT:-8000}"
start worker "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/run_temporal_worker.py"

if [[ -n "$PROJECT" ]]; then
    start project "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/run_project.py" \
        --project "$PROJECT"
else
    start supervisor "$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/run_supervisor.py"
fi

printf '\n\033[1mRAVEL V0 is up.\033[0m\n'
printf '  Gateway   http://%s:%s\n' "${RAVEL_GATEWAY_HOST:-127.0.0.1}" "${RAVEL_GATEWAY_PORT:-8000}"
printf '  Temporal  http://127.0.0.1:%s\n' "${RAVEL_TEMPORAL_UI_PORT:-8088}"
printf '  logs      runtime/logs/\n'
if [[ -n "$PROJECT" ]]; then
    printf '  driving   project %s\n' "$PROJECT"
else
    printf '  supervisor discovering active projects\n'
fi
printf '\n'

if [[ $WITH_TUI -eq 1 ]]; then
    step "Console"
    printf '  closing the console does not stop RAVEL; Ctrl-C does\n\n'
    "$REPO_ROOT/.venv/bin/python" -m ravel.tui || true

    # **Leaving the console is not stopping RAVEL.** That is the thing the
    # supervisor exists for, and the headline claim a person checks first: they
    # create a project, close the console, and the project runs. So this script
    # stays in the foreground holding the services, and the trap above is what
    # ends them — which makes Ctrl-C the one gesture that stops a deployment.
    #
    # `wait` rather than `wait -n` here, deliberately: one service dying is not
    # a reason to tear down the four others and the projects they are driving,
    # and the log the script wrote says which one it was.
    printf '\033[1mThe console is closed; RAVEL is not.\033[0m\n'
    printf '  the supervisor is still discovering and driving active projects\n'
    printf '  logs      runtime/logs/\n'
    printf '  Ctrl-C to stop\n\n'
    wait || true
else
    printf '  Ctrl-C to stop\n\n'
    wait -n || true
fi
