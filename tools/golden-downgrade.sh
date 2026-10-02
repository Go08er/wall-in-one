#!/usr/bin/env bash
# Run the golden-profile downgrade tests against v0.1.4.
#
#   tools/golden-downgrade.sh [REV] [PYTEST ARGS...]
#
# There is no 0.1.5: v0.1.4 is the only release anyone rolls back to from
# 0.2.0, and it has no forward-compatibility guard. REV defaults to dbfbaa0,
# the commit tagged v0.1.4; it exists only to name that commit another way
# (`v0.1.4`), and the tests fail for any other build. The old source is
# checked out with `git worktree add --detach` into a temporary directory, the
# tests in tests/golden/test_downgrade.py run with WIO_OLD_SRC pointing at it,
# and the worktree is removed again on exit.
#
# Run it through the isolated wrapper, `tools/isolated.sh tools/golden-downgrade.sh`,
# never from a bare dev shell. Every test builds its own sandbox home and the
# old build only ever sees that sandbox, but the wrapper is what keeps the
# rest of the run off your real profile and session.
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
