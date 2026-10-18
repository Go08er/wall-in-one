#!/usr/bin/env bash
# Set the install kind a package is built as, before it builds the Python half:
#   tools/set-install-kind.sh <nix|system|source>
# It rewrites the one INSTALL_KIND line of src/wall_in_one/install_kind.py in
# the tree this script is in, and fails unless exactly that line now says
# <kind>. The service takes the same value from WALL_IN_ONE_INSTALL_KIND when
# cargo builds it; tools/check-install-kind.sh checks that both agree. See
# docs/packaging.md.
set -euo pipefail

[ $# -eq 1 ] || { echo "usage: set-install-kind.sh <nix|system|source>" >&2; exit 2; }
kind=$1
case "$kind" in
  nix | system | source) ;;
  *) echo "set-install-kind.sh: unknown install kind '$kind'; expected nix, system or source" >&2; exit 2 ;;
esac

module="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/src/wall_in_one/install_kind.py"
line='^INSTALL_KIND: Final\[InstallKind\] = "[a-z]*"$'
if [ "$(grep -c "$line" "$module")" != 1 ]; then
  echo "set-install-kind.sh: $module has no single INSTALL_KIND line to rewrite" >&2
  exit 1
fi
sed -i "s/$line/INSTALL_KIND: Final[InstallKind] = \"$kind\"/" "$module"
grep -qx "INSTALL_KIND: Final\\[InstallKind\\] = \"$kind\"" "$module"
