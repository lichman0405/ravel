#!/usr/bin/env bash
#
# Start RAVEL V0 infrastructure and wait until it is actually usable.
#
#   scripts/dev_up.sh
#
# Waits on real readiness, not just container start: PostgreSQL accepting
# queries, Temporal reporting a healthy cluster, MinIO answering its health
# endpoint.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

if [[ -f .env ]]; then
    set -a
    # shellcheck disable=SC1091
    . ./.env
    set +a
fi

POSTGRES_PORT="${RAVEL_POSTGRES_PORT:-55432}"
POSTGRES_USER="${RAVEL_POSTGRES_USER:-ravel}"
POSTGRES_DB="${RAVEL_POSTGRES_DB:-ravel}"
TEMPORAL_PORT="${RAVEL_TEMPORAL_PORT:-7233}"
S3_PORT="${RAVEL_S3_PORT:-9100}"

step() { printf '\n\033[1m%s\033[0m\n' "$1"; }
pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

step "Starting containers"
docker compose up -d --wait
pass "containers healthy"

step "PostgreSQL"
for _ in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1; then
        pass "accepting connections on 127.0.0.1:${POSTGRES_PORT}"
        break
    fi
    sleep 1
done
docker compose exec -T postgres pg_isready -U "$POSTGRES_USER" -d "$POSTGRES_DB" >/dev/null 2>&1 \
    || fail "postgres not ready"

step "Temporal"
for _ in $(seq 1 60); do
    if docker compose exec -T temporal tctl --address temporal:7233 cluster health >/dev/null 2>&1; then
        pass "cluster healthy on 127.0.0.1:${TEMPORAL_PORT}"
        break
    fi
    sleep 2
done
docker compose exec -T temporal tctl --address temporal:7233 cluster health >/dev/null 2>&1 \
    || fail "temporal not ready"

step "MinIO"
for _ in $(seq 1 30); do
    if curl -fsS "http://127.0.0.1:${S3_PORT}/minio/health/live" >/dev/null 2>&1; then
        pass "live on 127.0.0.1:${S3_PORT}"
        break
    fi
    sleep 1
done
curl -fsS "http://127.0.0.1:${S3_PORT}/minio/health/live" >/dev/null 2>&1 || fail "minio not ready"

printf '\n\033[1mInfrastructure ready.\033[0m\n'
printf '  Temporal UI  http://127.0.0.1:%s\n' "${RAVEL_TEMPORAL_UI_PORT:-8088}"
printf '  MinIO console http://127.0.0.1:%s\n\n' "${RAVEL_S3_CONSOLE_PORT:-9101}"
