#!/usr/bin/env bash
#
# Stop RAVEL V0 infrastructure.
#
#   scripts/dev_down.sh        stop containers, keep volumes (data survives)
#   scripts/dev_down.sh -v     stop containers and DELETE all data
#
# The -v form also clears RAVEL runtime state, so a fresh start cannot inherit
# a DSH home or workspace that disagrees with an empty database.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

PURGE=0
for arg in "$@"; do
    case "$arg" in
        -v|--volumes) PURGE=1 ;;
        *) printf 'usage: %s [-v|--volumes]\n' "$0" >&2; exit 2 ;;
    esac
done

if [[ $PURGE -eq 1 ]]; then
    printf '\033[33m! Removing containers AND volumes — all database and object data will be lost.\033[0m\n'
    read -r -p "Type 'yes' to continue: " reply
    [[ "$reply" == "yes" ]] || { echo "aborted"; exit 1; }
    docker compose down -v --remove-orphans
    printf '  \033[32m✓\033[0m containers and volumes removed\n'

    # Runtime state must not outlive the database that describes it.
    for dir in runtime/dsh_home runtime/workspaces runtime/snapshots; do
        if [[ -e "$dir" ]]; then
            rm -rf "$dir"
            printf '  \033[32m✓\033[0m removed %s\n' "$dir"
        fi
    done
else
    docker compose down --remove-orphans
    printf '  \033[32m✓\033[0m containers stopped (volumes kept)\n'
fi
