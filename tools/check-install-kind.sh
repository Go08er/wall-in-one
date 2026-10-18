#!/usr/bin/env bash
# The two halves of one package say the same install kind:
#   tools/check-install-kind.sh <nix|system|source> <wall-in-one> <wall-in-one-service>
# Runs both programs' --version and fails unless the second line of each is
# "install kind: <kind>". The flake's desktop check runs it on the installed
# package; a distribution package runs it on what it installed. See
# docs/packaging.md.
set -euo pipefail

[ $# -eq 3 ] || {
  echo "usage: check-install-kind.sh <nix|system|source> <wall-in-one> <wall-in-one-service>" >&2
  exit 2
}
want="install kind: $1"
shift
for program in "$@"; do
  output=$("$program" --version)
  got=$(printf '%s\n' "$output" | sed -n 2p)
  if [ "$got" != "$want" ]; then
    echo "check-install-kind.sh: $program --version says '$got', expected '$want'" >&2
    exit 1
  fi
done
