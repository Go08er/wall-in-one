#!/usr/bin/env bash
# Build the Arch package from this checkout's working tree (tracked and
# untracked-but-not-ignored files, uncommitted edits included) instead of the
# release tag's tarball. Run as a normal user: makepkg refuses root.
#
#   packaging/arch/local-build.sh [makepkg options]     e.g. --nodeps --nocheck
#
# It derives a PKGBUILD from the release one, swapping only `source` and
# `sha256sums`, so the two cannot drift apart. The package lands in
# $WIO_ARCH_OUT (default: a new directory below ${TMPDIR:-/tmp}), which is
# printed at the end.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
pkgver="$(sed -n 's/^pkgver=//p' "$here/PKGBUILD")"
out="${WIO_ARCH_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/wio-arch.XXXXXX")}"
mkdir -p "$out"
tarball="wall-in-one-$pkgver-local.tar.gz"

cd "$repo"
git ls-files -z --cached --others --exclude-standard \
  | tar --null -T - --transform "s,^,wall-in-one-$pkgver/," -czf "$out/$tarball"

sed -e "s|^source=.*|source=(\"wall-in-one-$pkgver.tar.gz::file://$out/$tarball\")|" \
    -e "s|^sha256sums=.*|sha256sums=('SKIP')|" \
    "$here/PKGBUILD" > "$out/PKGBUILD"
grep -q "file://$out/$tarball" "$out/PKGBUILD"

cd "$out"
makepkg --force "$@"
echo "built in $out"
