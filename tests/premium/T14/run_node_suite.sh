#!/usr/bin/env bash
# Gate entrypoint for the desktop's JavaScript tests.
#
# The gate runs on Linux, while the host that owns the Electron binary, the
# Windows packaging tools and the Windows node_modules is Windows. Installing a
# Linux node_modules into the shared checkout would replace the host's native
# esbuild/rollup/Electron binaries and break the Windows launch test, so this
# script builds the committed tree in a throwaway Linux directory instead:
#
#   git archive HEAD -> mktemp -> pnpm install --frozen-lockfile
#   -> typecheck -> build -> node --test
#
# The built dist is copied back (untracked, ignored) because the Python cases
# exercise the same bundle against a real Gateway. Nothing here is a mock, and
# the only external need is the npm registry for the pinned toolchain and
# lockfile.

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
workdir="$(mktemp -d "${TMPDIR:-/tmp}/ravel-t14-node.XXXXXX")"
trap 'rm -rf "$workdir"' EXIT

git -C "$repo_root" archive HEAD | tar -x -C "$workdir"

cd "$workdir"
npx --yes \
  --package=node@22.23.3 \
  --package=pnpm@9.15.9 \
  bash -eu -o pipefail -c '
    printf "node %s\n" "$(node --version)"
    printf "pnpm %s\n" "$(pnpm --version)"
    pnpm install --frozen-lockfile
    pnpm --dir apps/desktop typecheck
    pnpm --dir apps/desktop build
    pnpm --dir apps/desktop test:unit
  '

rm -rf "$repo_root/apps/desktop/dist"
mkdir -p "$repo_root/apps/desktop"
cp -a "$workdir/apps/desktop/dist" "$repo_root/apps/desktop/dist"
printf "copied Linux-built desktop dist to %s\n" "$repo_root/apps/desktop/dist"
