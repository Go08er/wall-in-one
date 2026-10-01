#!/usr/bin/env bash
# mypy: the demo's AppState and thumbnail loader implement the app's Protocols
# (wall_in_one.ui.next.state / .thumbs). Only tools/protocol_check.py is judged.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
cd "$REPO"
nix develop --command env MYPYPATH="$REPO/src:$HERE" \
  mypy --strict --follow-imports=silent --no-incremental --cache-dir=/dev/null \
  --python-version 3.14 "$HERE/tools/protocol_check.py" 2>&1 \
  | grep -v "^warning: Git tree\|dev shell -- run"
