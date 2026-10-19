"""The Arch package is this release: packaging/arch's pkgver is the app's version.

package() and the Arch CI smoke test compare the installed programs' --version
with pkgver; this catches a PKGBUILD or .SRCINFO left behind by a version bump
before the tag.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from wall_in_one import __version__

ARCH = Path(__file__).resolve().parents[1] / "packaging" / "arch"


@pytest.mark.skipif(not (ARCH / "PKGBUILD").is_file(), reason="not running from a source tree")
def test_the_pkgbuild_and_srcinfo_are_this_release() -> None:
    source = (ARCH / "PKGBUILD").read_text(encoding="utf-8")
    pkgbuild = re.search(r"^pkgver=(\S+)$", source, re.MULTILINE)
    srcinfo = re.search(
        r"^\tpkgver = (\S+)$", (ARCH / ".SRCINFO").read_text(encoding="utf-8"), re.MULTILINE
    )
    assert pkgbuild and srcinfo
    assert pkgbuild.group(1) == __version__
    assert srcinfo.group(1) == __version__
    assert f"v{__version__}.tar.gz" in source.replace("$pkgver", __version__)
