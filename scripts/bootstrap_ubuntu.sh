#!/usr/bin/env bash
#
# RAVEL V0 — Ubuntu 24.04 canonical environment bootstrap.
#
# Idempotent: safe to re-run. Installs only what is missing, and never
# overwrites an existing `.env`.
#
#   scripts/bootstrap_ubuntu.sh            full bootstrap
#   scripts/bootstrap_ubuntu.sh --no-apt   skip system packages
#
# Canonical platform per docs/14_DEVELOPMENT_ENVIRONMENT.md.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$REPO_ROOT/.venv"
SKIP_APT=0
[[ "${1:-}" == "--no-apt" ]] && SKIP_APT=1

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1" >&2; }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

# ── Platform ────────────────────────────────────────────────────────────────

step "Platform"

[[ -r /etc/os-release ]] || fail "cannot read /etc/os-release"
# shellcheck disable=SC1091
. /etc/os-release
[[ "${ID:-}" == "ubuntu" ]] || fail "RAVEL V0 targets Ubuntu; found ID=${ID:-unknown}"
[[ "${VERSION_ID:-}" == "24.04" ]] || warn "expected Ubuntu 24.04, found ${VERSION_ID:-unknown}"
[[ "$(uname -m)" == "x86_64" ]] || fail "RAVEL V0 targets x86_64; found $(uname -m)"
pass "Ubuntu ${VERSION_ID} $(uname -m)"

# ── System packages ─────────────────────────────────────────────────────────

step "System packages"

APT_PACKAGES=(git curl ca-certificates build-essential pkg-config)
MISSING=()
for pkg in "${APT_PACKAGES[@]}"; do
    dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done

if [[ ${#MISSING[@]} -eq 0 ]]; then
    pass "baseline packages present"
elif [[ $SKIP_APT -eq 1 ]]; then
    warn "missing: ${MISSING[*]} (skipped, --no-apt)"
else
    command -v apt-get >/dev/null || fail "apt-get unavailable; cannot install ${MISSING[*]}"
    echo "  installing: ${MISSING[*]}"
    sudo apt-get update -qq
    sudo apt-get install -y -qq "${MISSING[@]}"
    pass "installed ${MISSING[*]}"
fi

# ── Python 3.12 ─────────────────────────────────────────────────────────────

step "Python"

command -v python3.12 >/dev/null || fail "python3.12 not found; install it (apt install python3.12 python3.12-venv)"
PY_VERSION="$(python3.12 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
[[ "$PY_VERSION" == "3.12" ]] || fail "expected Python 3.12, found $PY_VERSION"
pass "python3.12 ($(python3.12 -c 'import sys; print(sys.version.split()[0])'))"

# uv is the preferred package manager; pip works but is slower.
if ! command -v uv >/dev/null; then
    warn "uv not found; falling back to pip. Install uv from https://docs.astral.sh/uv/"
    UV=0
else
    pass "uv $(uv --version | awk '{print $2}')"
    UV=1
fi

# ── Virtual environment ─────────────────────────────────────────────────────

step "Virtual environment"

if [[ ! -d "$VENV_DIR" ]]; then
    python3.12 -m venv "$VENV_DIR"
    pass "created $VENV_DIR"
else
    pass "$VENV_DIR exists"
fi

# shellcheck disable=SC1091
. "$VENV_DIR/bin/activate"

if [[ $UV -eq 1 ]]; then
    uv pip install -q -e ".[dev]"
else
    python -m pip install -q --upgrade pip
    python -m pip install -q -e ".[dev]"
fi
pass "installed project dependencies (editable)"

# ── DeepSeek Harness pin ────────────────────────────────────────────────────

step "DeepSeek Harness"

python - <<'PY'
import importlib.metadata as md
import sys

try:
    sdk = md.version("deepseek-harness-sdk")
    runtime = md.version("deepseek-harness-runtime-bin")
except md.PackageNotFoundError as exc:  # pragma: no cover - bootstrap guard
    sys.exit(f"  \033[31m✗\033[0m missing distribution: {exc.name}")
print(f"  \033[32m✓\033[0m deepseek-harness-sdk=={sdk}, runtime-bin=={runtime}")
if sdk != "0.1.5rc1" or runtime != "0.1.5rc1":
    sys.exit("  \033[31m✗\033[0m version drift from vendor/DSH_PIN.json; expected 0.1.5rc1")
PY

# ── Playwright Chromium ─────────────────────────────────────────────────────

step "Playwright Chromium"

if [[ "${RAVEL_SKIP_PLAYWRIGHT:-0}" == "1" ]]; then
    warn "skipped (RAVEL_SKIP_PLAYWRIGHT=1)"
else
    python -m playwright install --with-deps chromium >/dev/null 2>&1 \
        || python -m playwright install chromium
    pass "chromium installed"
fi

# ── Docker ──────────────────────────────────────────────────────────────────

step "Docker"

if ! command -v docker >/dev/null; then
    fail "docker not found; install Docker Engine (https://docs.docker.com/engine/install/ubuntu/)"
fi
docker compose version >/dev/null 2>&1 || fail "docker compose v2 plugin not found"
if ! docker info >/dev/null 2>&1; then
    fail "docker daemon unreachable; start it or add $USER to the docker group"
fi
pass "docker $(docker --version | awk '{print $3}' | tr -d ,), compose $(docker compose version --short)"

# ── Environment file ────────────────────────────────────────────────────────

step "Environment file"

if [[ -f "$REPO_ROOT/.env" ]]; then
    pass ".env exists (left untouched)"
else
    # Owner-only: this file is where a model credential goes, and it is
    # git-ignored precisely because it holds secrets.
    install -m 600 "$REPO_ROOT/.env.example" "$REPO_ROOT/.env"
    warn "created .env from .env.example (mode 600) — set DEEPSEEK_API_KEY before running agents"
fi

printf '\n\033[1mBootstrap complete.\033[0m Next:\n'
printf '  1. Put a real key in .env:  DEEPSEEK_API_KEY=...\n'
printf '  2. Start infrastructure:    scripts/dev_up.sh\n'
printf '  3. Verify everything:       make env-check\n\n'
