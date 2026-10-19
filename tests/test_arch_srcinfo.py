"""packaging/arch/.SRCINFO says what packaging/arch/PKGBUILD says.

``makepkg --printsrcinfo`` writes .SRCINFO, and the Arch CI job diffs it against
a fresh one, but only on release branches. This is the cheap check that runs
everywhere: it sources the PKGBUILD in bash, reads every field it sets, and
compares them, value by value and in order, with the .SRCINFO's. It does not
replace the CI diff (the layout is makepkg's); it makes a forgotten edit of
one file show up in an ordinary test run. docs/packaging.md.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

ARCH = Path(__file__).resolve().parents[1] / "packaging" / "arch"
PKGBUILD = ARCH / "PKGBUILD"
SRCINFO = ARCH / ".SRCINFO"

#: Every makepkg variable a single-package .SRCINFO can repeat (man PKGBUILD).
SCALARS = ("pkgdesc", "pkgver", "pkgrel", "epoch", "url", "install", "changelog")
ARRAYS = (
    "arch",
    "groups",
    "license",
    "noextract",
    "options",
    "backup",
    "validpgpkeys",
)
#: Arrays makepkg also reads as ``<name>_<arch>``.
ARCH_ARRAYS = (
    "checkdepends",
    "makedepends",
    "depends",
    "optdepends",
    "provides",
    "conflicts",
    "replaces",
    "source",
    "md5sums",
    "sha1sums",
    "sha224sums",
    "sha256sums",
    "sha384sums",
    "sha512sums",
    "b2sums",
)
#: The script's own variables, which are not the PKGBUILD's.
HELPERS = frozenset({"before", "v", "vals", "x"})
ARCHES = ("x86_64", "aarch64")
FIELDS = (
    *SCALARS,
    *ARRAYS,
    *ARCH_ARRAYS,
    *(f"{name}_{arch}" for name in ARCH_ARRAYS for arch in ARCHES),
)

pytestmark = pytest.mark.skipif(
    not PKGBUILD.is_file() or shutil.which("bash") is None,
    reason="not running from a source tree",
)


def _pkgbuild() -> tuple[dict[str, list[str]], list[str]]:
    """The fields the PKGBUILD sets, and the lowercase variables it sets that are not fields."""
    script = (
        'before=" $(compgen -v | tr "\\n" " ") "\n'
        'source "$1"; shift\n'
        'for v in "$@"; do eval "vals=(\\"\\${$v[@]}\\")"; '
        'for x in "${vals[@]}"; do printf "F\\t%s\\t%s\\n" "$v" "$x"; done; done\n'
        'for v in $(compgen -v); do case "$before" in *" $v "*) ;; '
        '*) printf "V\\t%s\\n" "$v" ;; esac; done\n'
        'printf "F\\tpkgname\\t%s\\n" "${pkgname[@]}"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script, "bash", str(PKGBUILD), *FIELDS],
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found: dict[str, list[str]] = defaultdict(list)
    variables: list[str] = []
    for line in out.splitlines():
        kind, _, rest = line.partition("\t")
        if kind == "V":
            # Bash's own variables are uppercase; the loop's are ours.
            if re.fullmatch(r"[a-z][a-z0-9_]*", rest) and rest not in HELPERS:
                variables.append(rest)
            continue
        key, _, value = rest.partition("\t")
        found[key].append(value)
    return found, variables


def _srcinfo() -> dict[str, list[str]]:
    found: dict[str, list[str]] = defaultdict(list)
    for number, line in enumerate(SRCINFO.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        key, separator, value = line.strip().partition(" = ")
        assert separator, f".SRCINFO line {number} is not 'key = value': {line!r}"
        indented = line.startswith("\t")
        assert indented == (key not in ("pkgbase", "pkgname")), f"line {number} indent: {line!r}"
        found[key].append(value)
    return found


def test_srcinfo_repeats_every_field_of_the_pkgbuild() -> None:
    build, _ = _pkgbuild()
    info = _srcinfo()
    assert info.pop("pkgbase") == build["pkgname"]
    assert info.pop("pkgname") == build["pkgname"]
    for field in FIELDS:
        assert info.pop(field, []) == build.get(field, []), field
    assert not info, f".SRCINFO has fields this test does not know: {sorted(info)}"


def test_the_pkgbuild_sets_no_variable_this_test_does_not_compare() -> None:
    """A makepkg variable added to the PKGBUILD must be compared too, not ignored."""
    _, variables = _pkgbuild()
    unknown = sorted(v for v in variables if v not in {*FIELDS, "pkgname", "pkgbase"})
    assert not unknown, (
        f"the PKGBUILD sets {unknown}, which test_arch_srcinfo.py does not compare with .SRCINFO"
    )
