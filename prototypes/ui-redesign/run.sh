#!/usr/bin/env bash
# Launch the interactive Wall-in-One UI prototype on your desktop.
# It uses dummy data only: no real library, settings, service or Noctalia calls.
#   prototypes/ui-redesign/run.sh            # dark style
#   prototypes/ui-redesign/run.sh --light    # light style
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE/../.."
exec nix develop --command env PYTHONDONTWRITEBYTECODE=1 \
  python prototypes/ui-redesign/demo.py "$@"
