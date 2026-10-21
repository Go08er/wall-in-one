"""packaging/arch/release/ is what paru installs from, so it is a release's files.

paru reads PKGBUILDs straight from this repository (docs/installing.md): the
``[wall-in-one]`` section in paru.conf points at this directory, and paru
installs and updates whatever it finds there. The development PKGBUILD one
level up has ``sha256sums=('SKIP')`` and, between a version bump and its tag,
names a version GitHub does not serve, so paru must never see it. This
directory holds the output of ``packaging/arch/aur-release.sh`` for the last
tag, committed after the tag (docs/packaging.md). Without Arch, these tests
check what can be checked: the checksum, the agreement with .SRCINFO, and that
the directory is not ahead of the app.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.test_arch_srcinfo import check_srcinfo_repeats_the_pkgbuild

ROOT = Path(__file__).resolve().parents[1]
RELEASE = ROOT / "packaging" / "arch" / "release"
PKGBUILD = RELEASE / "PKGBUILD"
SRCINFO = RELEASE / ".SRCINFO"
INIT = ROOT / "src" / "wall_in_one" / "__init__.py"


def _version(pattern: str, text: str) -> str:
    found = re.search(pattern, text, re.MULTILINE)
    assert found is not None, pattern
    return found.group(1)


def _numbers(version: str) -> tuple[int, ...]:
    """A release version as pacman's vercmp orders it, for plain dotted numbers."""
    assert re.fullmatch(r"\d+(\.\d+)*", version), f"not a plain release version: {version}"
    return tuple(int(part) for part in version.split("."))


def test_the_release_pkgbuild_has_a_real_checksum() -> None:
    text = PKGBUILD.read_text(encoding="utf-8")
    assert "SKIP" not in text
    assert re.search(r"^sha256sums=\('[0-9a-f]{64}'\)$", text, re.MULTILINE)
    assert "sha256sums = SKIP" not in SRCINFO.read_text(encoding="utf-8")
    assert re.search(r"^\tsha256sums = [0-9a-f]{64}$", SRCINFO.read_text(), re.MULTILINE)


def test_the_release_srcinfo_matches_the_release_pkgbuild() -> None:
    check_srcinfo_repeats_the_pkgbuild(PKGBUILD, SRCINFO)


def test_the_release_names_a_tagged_source() -> None:
    version = _version(r"^pkgver=(\S+)$", PKGBUILD.read_text(encoding="utf-8"))
    # .SRCINFO holds the source with the variables expanded.
    assert f"::https://github.com/Go08er/wall-in-one/archive/refs/tags/v{version}.tar.gz" in (
        SRCINFO.read_text(encoding="utf-8")
    )


def test_the_release_is_not_ahead_of_the_app() -> None:
    """It trails the version bump until the post-tag commit, and never leads it."""
    released = _version(r"^pkgver=(\S+)$", PKGBUILD.read_text(encoding="utf-8"))
    app = _version(r'^__version__ = "([^"]+)"$', INIT.read_text(encoding="utf-8"))
    assert _numbers(released) <= _numbers(app), (released, app)
