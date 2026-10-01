#!/usr/bin/env bash
# Run the golden-profile downgrade tests against an older Wall-in-One build.
#
#   tools/golden-downgrade.sh [REV] [PYTEST ARGS...]
#
# REV defaults to dbfbaa0 (v0.1.4, the packaged release before the
# forward-compatibility guard). The old source is checked out with
# `git worktree add --detach` into a temporary directory, the tests in
# tests/golden/test_downgrade.py run with WIO_OLD_SRC pointing at it, and the
# worktree is removed again on exit.
#
# Run it from the dev shell (`nix develop --command tools/golden-downgrade.sh`).
# Every test builds its own sandbox home; the old build only ever sees that
# sandbox, never the real profile.
set -euo pipefail

rev="${1:-dbfbaa0}"
if [[ $# -gt 0 ]]; then
  shift
fi

repo="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
work="$(mktemp -d "${TMPDIR:-/tmp}/wio-golden-downgrade.XXXXXX")"
old="$work/old"

cleanup() {
  if [[ -d "$old" ]]; then
    git -C "$repo" worktree remove --force "$old" || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

git -C "$repo" worktree add --detach --quiet "$old" "$rev"
echo "old build: $(git -C "$old" log -1 --format='%h %s')" >&2

cd "$repo"
WIO_OLD_SRC="$old/src" python -m pytest tests/golden/test_downgrade.py \
  -m downgrade -p no:cacheprovider -rxXfs "$@"
