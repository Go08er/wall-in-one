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
# printed at the end. It must be empty, or one this script made before (it leaves
# a marker file), and outside the checkout; anything else is refused before it
# is touched.
set -euo pipefail

refuse() {
  echo "local-build.sh: $*; choose another" >&2
  exit 1
}

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
pkgver="$(sed -n 's/^pkgver=//p' "$here/PKGBUILD")"
# Resolve the output directory once, to an absolute physical path, and use
# only that from here on: a relative WIO_ARCH_OUT means the caller's cwd, not
# the repository the script cd's into below.
out_arg="${WIO_ARCH_OUT:-$(mktemp -d "${TMPDIR:-/tmp}/wio-arch.XXXXXX")}"
out="$(mkdir -p -- "$out_arg" && cd -- "$out_arg" && pwd -P)"
repo_real="$(cd "$repo" && pwd -P)"
# The output directory is cleaned and written to below, so it must be neither
# inside the checkout (this packaging directory, service/, ...) nor above it.
case "$out/" in
  "$repo_real/"*) refuse "WIO_ARCH_OUT is $out, which is inside the checkout" ;;
esac
case "$repo_real/" in
  "$out/"*) refuse "WIO_ARCH_OUT is $out, which contains the checkout" ;;
esac
# ... and it must be empty or one this script made (it leaves a marker), so a
# mistyped WIO_ARCH_OUT such as ~ is not cleaned or built into.
marker="$out/.wio-local-build"
if [ -z "$(ls -A -- "$out")" ] || [ -e "$marker" ]; then
  : > "$marker"
else
  refuse "WIO_ARCH_OUT is $out, which is not empty and was not made by this script"
fi
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
