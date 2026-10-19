"""packaging/arch/.SRCINFO says what packaging/arch/PKGBUILD says.

``makepkg --printsrcinfo`` writes .SRCINFO, and the Arch CI job diffs it against
a fresh one, but only on release branches. This is the cheap check that runs
everywhere: it sources the PKGBUILD in bash, reads every field it sets, and
compares them, value by value and in order, with the .SRCINFO's. It does not
replace the CI diff (the layout is makepkg's); it makes a forgotten edit of
one file show up in an ordinary test run. docs/packaging.md.
"""

from __future__ import annotations

import shutil
import subprocess
from collections import defaultdict
from pathlib import Path

import pytest

ARCH = Path(__file__).resolve().parents[1] / "packaging" / "arch"
PKGBUILD = ARCH / "PKGBUILD"
SRCINFO = ARCH / ".SRCINFO"

#: The PKGBUILD variables a single-package .SRCINFO repeats, as makepkg names them.
FIELDS = (
    "pkgdesc",
    "pkgver",
    "pkgrel",
    "url",
    "arch",
    "license",
    "checkdepends",
    "makedepends",
    "depends",
    "optdepends",
    "source",
    "sha256sums",
)

pytestmark = pytest.mark.skipif(
    not PKGBUILD.is_file() or shutil.which("bash") is None,
    reason="not running from a source tree",
)


def _pkgbuild() -> dict[str, list[str]]:
    script = (
        'source "$1"; shift\n'
        'for v in "$@"; do eval "vals=(\\"\\${$v[@]}\\")"; '
        'for x in "${vals[@]}"; do printf "%s\\t%s\\n" "$v" "$x"; done; done\n'
        'printf "pkgname\\t%s\\n" "${pkgname[@]}"\n'
    )
    out = subprocess.run(
        ["bash", "-c", script, "bash", str(PKGBUILD), *FIELDS],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    found: dict[str, list[str]] = defaultdict(list)
    for line in out.splitlines():
        key, _, value = line.partition("\t")
        found[key].append(value)
    return found


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
    build, info = _pkgbuild(), _srcinfo()
    assert info.pop("pkgbase") == build["pkgname"]
    assert info.pop("pkgname") == build["pkgname"]
    for field in FIELDS:
        assert info.pop(field, []) == build.get(field, []), field
    assert not info, f".SRCINFO has fields this test does not know: {sorted(info)}"

