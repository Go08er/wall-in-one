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
# The output directory is cleaned and written to below: it must be neither
# this packaging directory nor the repository, under any name (compare the
# real paths).
out_real="$(cd "$out" && pwd -P)"
for protected in "$here" "$repo"; do
  if [ "$out_real" = "$(cd "$protected" && pwd -P)" ]; then
    echo "local-build.sh: WIO_ARCH_OUT is $out_real, which is part of the checkout; choose another" >&2
    exit 1
  fi
done
tarball="wall-in-one-$pkgver-local.tar.gz"

# A fixed WIO_ARCH_OUT keeps the last run's files, and makepkg would reuse
# both: a source tarball it already has (the same name, so whatever this
# checkout's last state was) and an extracted src/ tree. Start from neither.
rm -rf -- "${out:?}/wall-in-one-$pkgver.tar.gz" "${out:?}/$tarball" "${out:?}/src" "${out:?}/pkg"

cd "$repo"
# Tracked files that were deleted in the working tree are still in the index;
# tar fails on them, and the build must not see them either.
git ls-files -z --cached --others --exclude-standard \
  | while IFS= read -r -d '' file; do
      if [ -e "$file" ] || [ -L "$file" ]; then printf '%s\0' "$file"; fi
    done \
  | tar --null -T - --transform "s,^,wall-in-one-$pkgver/," -czf "$out/$tarball"

sed -e "s|^source=.*|source=(\"wall-in-one-$pkgver.tar.gz::file://$out/$tarball\")|" \
    -e "s|^sha256sums=.*|sha256sums=('SKIP')|" \
    "$here/PKGBUILD" > "$out/PKGBUILD"
grep -q "file://$out/$tarball" "$out/PKGBUILD"

cd "$out"
makepkg --force "$@"
echo "built in $out"
