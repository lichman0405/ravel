#!/usr/bin/env bash
#
# Install RAVEL's three long-running processes as services.
#
#   scripts/install_services.sh                     system units, /opt/ravel, user ravel
#   scripts/install_services.sh --user              units for the current user, in this checkout
#   scripts/install_services.sh --prefix /srv/ravel --user ravel
#   scripts/install_services.sh --dry-run           print what would be installed
#
# The three units are `infra/systemd/*.service` with two substitutions: where
# the checkout is, and which account the processes run as. Everything else —
# `Restart=always`, `KillSignal=SIGTERM`, the timeouts, the journal — is what
# the repository carries and what `tests/unit/test_service_units.py` reads, so
# an installer that rewrote more than those two things would be installing
# something other than what is verified.
#
# **`--user` is what a machine without root gets**, and it is not a degraded
# mode: the same restart policy, the same signals, the same journal. Two things
# differ and both are systemd's rules rather than choices: a user unit cannot
# carry `User=`, and it lives under `~/.config/systemd/user` and is managed by
# `systemctl --user` instead of `systemctl`. It also only runs while its owner
# has a session unless `loginctl enable-linger` has been run, which this script
# tells you about rather than doing.
#
# The units are installed, not started. Whether a deployment wants these three
# running is a decision about that deployment, and `systemctl start` is one
# word.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOURCES="$REPO_ROOT/infra/systemd"

MODE=system
PREFIX="/opt/ravel"
SERVICE_USER="ravel"
DRY_RUN=0

usage() {
    printf 'usage: %s [--user] [--prefix <dir>] [--service-user <name>] [--dry-run]\n' \
        "$(basename "$0")" >&2
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user) MODE=user ;;
        --system) MODE=system ;;
        --prefix) PREFIX="${2:?--prefix needs a directory}"; shift ;;
        --service-user) SERVICE_USER="${2:?--service-user needs a name}"; shift ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
    shift
done

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
note() { printf '  \033[2m%s\033[0m\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

if [[ "$MODE" == user ]]; then
    # A user unit names no account, and cannot. The checkout is the one this
    # script was run from, because that is the only one the invoking user is
    # known to be able to read.
    DEST="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    PREFIX="$REPO_ROOT"
    SERVICE_USER=""
    SYSTEMCTL=(systemctl --user)
else
    DEST="/etc/systemd/system"
    SYSTEMCTL=(systemctl)
fi

[[ -d "$SOURCES" ]] || { warn "no units at $SOURCES"; exit 1; }
# The writability check comes after the dry run's, deliberately: printing the
# units is what somebody who does *not* have root should be able to do first,
# and a script that refused to show them the file until they could write it
# would be asking them to install something they had not read.
if [[ "$MODE" == system && $DRY_RUN -eq 0 && ! -w "$DEST" ]]; then
    warn "$DEST is not writable; re-run with sudo, or use --user"
    exit 1
fi

render() {  # render <unit file>
    # Two substitutions, applied to the deployment's own name for itself.
    # `/opt/ravel` and `ravel` are what the checked-in units carry, so a
    # default install rewrites them to themselves and the result is the file
    # that was verified.
    local source="$1"
    sed -e "s#/opt/ravel#${PREFIX}#g" "$source" \
        | if [[ -z "$SERVICE_USER" ]]; then
            # `User=` and `Group=` are refused by a user manager. Dropped
            # rather than emptied: `User=` with no value is a parse error.
            grep -v -E '^(User|Group)='
        else
            sed -e "s#^User=.*#User=${SERVICE_USER}#" -e "s#^Group=.*#Group=${SERVICE_USER}#"
        fi
}

step "Services"
printf '  %s\n' "$MODE units -> $DEST"
printf '  %s\n' "checkout    $PREFIX"
[[ -n "$SERVICE_USER" ]] && printf '  %s\n' "running as  $SERVICE_USER"

if [[ $DRY_RUN -eq 1 ]]; then
    for source in "$SOURCES"/*.service; do
        printf '\n\033[1m── %s ──\033[0m\n' "$(basename "$source")"
        render "$source"
    done
    exit 0
fi

if [[ "$MODE" == system ]]; then
    mkdir -p "$DEST"
else
    mkdir -p "$DEST"
fi

for source in "$SOURCES"/*.service; do
    name="$(basename "$source")"
    render "$source" >"$DEST/$name"
    pass "$name"
done

"${SYSTEMCTL[@]}" daemon-reload
pass "reloaded"

for source in "$SOURCES"/*.service; do
    "${SYSTEMCTL[@]}" enable "$(basename "$source")" >/dev/null 2>&1
done
pass "enabled at boot"

step "Next"
if [[ "$MODE" == user ]]; then
    note "systemctl --user start ravel-gateway ravel-supervisor ravel-temporal-worker"
    note "journalctl --user -u ravel-supervisor -f"
    if ! loginctl show-user "$USER" -p Linger --value 2>/dev/null | grep -q yes; then
        warn "lingering is off, so these stop when your last session ends:"
        warn "  sudo loginctl enable-linger $USER"
    fi
else
    note "systemctl start ravel-gateway ravel-supervisor ravel-temporal-worker"
    note "journalctl -u ravel-supervisor -f"
fi
printf '\n'
