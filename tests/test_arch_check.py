"""packaging/arch/PKGBUILD's package() installs the service binary build() built.

check()'s ``cargo test --release`` turns on the service's ``test-support``
feature, and in build()'s target directory it relinks
``target/release/wall-in-one-service`` with it (review r13g R-L4). This runs
the PKGBUILD's own build() and check() with a cargo that writes, into the
release binary of whichever target directory it is given, the subcommand that
wrote it; and python as a no-op.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

PKGBUILD = Path(__file__).resolve().parents[1] / "packaging" / "arch" / "PKGBUILD"

pytestmark = pytest.mark.skipif(
    not PKGBUILD.is_file() or shutil.which("bash") is None,
    reason="not running from a source tree",
)

_RUN = r"""
cargo() {
  local command=$1 dir=${CARGO_TARGET_DIR:-target}
  shift
  while (($#)); do
    case $1 in
      --target-dir) dir=$2; shift ;;
      --target-dir=*) dir=${1#--target-dir=} ;;
    esac
    shift
  done
  mkdir -p "$dir/release"
  printf '%s' "$command" > "$dir/release/wall-in-one-service"
}
python() { :; }
source "$1"
mkdir "$pkgname-$pkgver"
(build) && (check) || exit 1
declare -f package
"""


def test_check_leaves_the_binary_package_installs_as_build_built_it(tmp_path: Path) -> None:
    run = subprocess.run(
        ["bash", "-c", _RUN, "bash", str(PKGBUILD)],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        check=True,
        capture_output=True,
        text=True,
    )
    installed = "target/release/wall-in-one-service"
    assert f"install -Dm755 {installed} " in run.stdout, run.stdout
    (source,) = tmp_path.glob("*-*")
    assert (source / installed).read_text() == "build", "package() installs what check() built"
    # check() did build: in its own directory.
    assert {path.read_text() for path in source.glob("*/release/wall-in-one-service")} == {
        "build",
        "test",
    }
