#!/usr/bin/env bash
# The two halves of one package say the same install kind:
#   tools/check-install-kind.sh <nix|system|source> <wall-in-one> <wall-in-one-service>
# Runs both programs' --version and fails unless each prints exactly two
# lines, "<name> <version>" (the same version for both) and
# "install kind: <kind>". The flake's desktop check runs it on the installed
# package; a distribution package runs it on what it installed. See
# docs/packaging.md.
set -euo pipefail

[ $# -eq 3 ] || {
  echo "usage: check-install-kind.sh <nix|system|source> <wall-in-one> <wall-in-one-service>" >&2
  exit 2
}
want="install kind: $1"
fail() {
  echo "check-install-kind.sh: $*" >&2
  exit 1
}

release=
for pair in "wall-in-one:$2" "wall-in-one-service:$3"; do
  name=${pair%%:*}
  program=${pair#*:}
  if ! output=$("$program" --version); then
    fail "$program --version failed"
  fi
  lines=$(printf '%s\n' "$output" | wc -l)
  [ "$lines" -eq 2 ] || fail "$program --version printed $lines lines, expected 2: $output"
  first=$(printf '%s\n' "$output" | sed -n 1p)
  second=$(printf '%s\n' "$output" | sed -n 2p)
  case "$first" in
    "$name "?*) ;;
    *) fail "$program --version says '$first', expected '$name <version>'" ;;
  esac
  version=${first#"$name "}
  if [ -n "$release" ] && [ "$version" != "$release" ]; then
    fail "$program is $version, but $2 is $release"
  fi
  release=$version
  [ "$second" = "$want" ] || fail "$program --version says '$second', expected '$want'"
done
