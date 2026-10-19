#!/usr/bin/env bash
# Turn the checked-in PKGBUILD into the one that gets published, once the
# release tag exists:
#
#   packaging/arch/aur-release.sh v0.4.0 [output-directory]
#
# The checked-in PKGBUILD has sha256sums=('SKIP') (CI builds a local tarball,
# so it never has a real one). This downloads GitHub's tarball for the tag,
# checks it is what the PKGBUILD expects, computes its sha256, and writes a
# PKGBUILD with that sum and a .SRCINFO to match into the output directory
# (default: a new directory below ${TMPDIR:-/tmp}), which is printed. It
# changes nothing in the repository. It refuses if
#   - the tag is not v<pkgver>,
#   - the tarball's top-level directory is not wall-in-one-<pkgver>,
#   - the PKGBUILD or .SRCINFO is not in the shape it rewrites, or a SKIP
#     would remain in the result.
# Needs network at release time. WIO_AUR_TARBALL=<file> uses that file instead
# of downloading, which is how the script is tested (tests/test_aur_release.py).
# docs/packaging.md, "Arch release checklist".
set -euo pipefail

die() {
  echo "aur-release.sh: $*" >&2
  exit 1
}

[ $# -ge 1 ] && [ $# -le 2 ] || { echo "usage: aur-release.sh <tag> [output-directory]" >&2; exit 2; }
tag=$1
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
pkgbuild="$here/PKGBUILD"
srcinfo="$here/.SRCINFO"
[ -f "$pkgbuild" ] && [ -f "$srcinfo" ] || die "no PKGBUILD and .SRCINFO beside this script"

pkgver="$(sed -n 's/^pkgver=//p' "$pkgbuild")"
url="$(sed -n "s/^url='\\(.*\\)'$/\\1/p" "$pkgbuild")"
[ -n "$pkgver" ] && [ -n "$url" ] || die "cannot read pkgver and url from the PKGBUILD"
[ "$tag" = "v$pkgver" ] || die "tag $tag is not v$pkgver (the PKGBUILD's pkgver)"

out="${2:-$(mktemp -d "${TMPDIR:-/tmp}/wio-aur.XXXXXX")}"
mkdir -p "$out"
work="$(mktemp -d "${TMPDIR:-/tmp}/wio-aur-work.XXXXXX")"
trap 'rm -rf -- "$work"' EXIT

tarball="$work/tarball.tar.gz"
if [ -n "${WIO_AUR_TARBALL:-}" ]; then
  cp -- "$WIO_AUR_TARBALL" "$tarball"
else
  curl --fail --silent --show-error --location \
    --output "$tarball" "$url/archive/refs/tags/$tag.tar.gz"
fi

tar -tzf "$tarball" > "$work/list" || die "the download is not a gzip tarball"
[ -s "$work/list" ] || die "the tarball is empty"
prefix="wall-in-one-$pkgver"
if grep -qvE "^(\\./)?$prefix(/|$)" "$work/list"; then
  die "the tarball has entries outside $prefix/ (top level: $(cut -d/ -f1 "$work/list" | sort -u | head -3 | tr '\n' ' '))"
fi
grep -qx "$prefix/" "$work/list" || grep -q "^$prefix/." "$work/list" \
  || die "the tarball has no $prefix/ directory"

sum="$(sha256sum "$tarball" | cut -d' ' -f1)"
[[ $sum =~ ^[0-9a-f]{64}$ ]] || die "unexpected checksum '$sum'"

# Rewrite exactly one line in each file, and nothing else.
[ "$(grep -c "^sha256sums=('SKIP')$" "$pkgbuild")" = 1 ] \
  || die "the PKGBUILD has no single sha256sums=('SKIP') line to replace"
[ "$(grep -c "^	sha256sums = SKIP$" "$srcinfo")" = 1 ] \
  || die "the .SRCINFO has no single 'sha256sums = SKIP' line to replace"
# The TODO about SKIP is for the checked-in file; the published one has a sum.
sed -e "s/^sha256sums=('SKIP')$/sha256sums=('$sum')/" \
    -e '/^# TODO(release)/,/^# package must never keep/d' \
    "$pkgbuild" > "$out/PKGBUILD"
sed -e "s/^	sha256sums = SKIP$/	sha256sums = $sum/" "$srcinfo" > "$out/.SRCINFO"

grep -q "^sha256sums=('$sum')$" "$out/PKGBUILD" || die "the PKGBUILD did not take the sum"
grep -q "^	sha256sums = $sum$" "$out/.SRCINFO" || die ".SRCINFO did not take the sum"
if grep -q 'SKIP' "$out/PKGBUILD" "$out/.SRCINFO"; then
  rm -f -- "$out/PKGBUILD" "$out/.SRCINFO"
  die "a SKIP would remain in the release files; nothing written"
fi
echo "release PKGBUILD and .SRCINFO for $tag (sha256 $sum) in $out"
