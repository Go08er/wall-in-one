"""packaging/arch/aur-release.sh, offline: the tarball comes from WIO_AUR_TARBALL.

The script is what turns the checked-in PKGBUILD (``sha256sums=('SKIP')``)
into the one published to the AUR, so its refusals are the point. Nothing here
touches the network. docs/packaging.md, "Arch release checklist".
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

ARCH = Path(__file__).resolve().parents[1] / "packaging" / "arch"

pytestmark = pytest.mark.skipif(
    not (ARCH / "aur-release.sh").is_file() or shutil.which("bash") is None,
    reason="not running from a source tree",
)


def _pkgver() -> str:
    match = re.search(r"^pkgver=(.+)$", (ARCH / "PKGBUILD").read_text(), re.MULTILINE)
    assert match
    return match.group(1)


def _tarball(path: Path, top: str, extra: tuple[str, ...] = ()) -> bytes:
    with tarfile.open(path, "w:gz") as archive:
        for name in (f"{top}/", f"{top}/LICENSE", *extra):
            info = tarfile.TarInfo(name)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                data = b"x"
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return path.read_bytes()


@pytest.fixture
def release(tmp_path: Path) -> Path:
    """A copy of the script, PKGBUILD and .SRCINFO to run against (and edit)."""
    directory = tmp_path / "arch"
    directory.mkdir()
    for name in ("aur-release.sh", "PKGBUILD", ".SRCINFO"):
        shutil.copy(ARCH / name, directory / name)
    return directory


def _run(
    release: Path, tmp_path: Path, tag: str, tarball: Path
) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "TMPDIR": str(tmp_path), "WIO_AUR_TARBALL": str(tarball)}
    return subprocess.run(
        ["bash", str(release / "aur-release.sh"), tag, str(tmp_path / "out")],
        capture_output=True,
        text=True,
        env=env,
    )


def test_a_good_tarball_gives_a_release_pkgbuild_and_srcinfo_with_its_sum(
    release: Path, tmp_path: Path
) -> None:
    pkgver = _pkgver()
    tarball = tmp_path / "t.tar.gz"
    sum_ = hashlib.sha256(_tarball(tarball, f"wall-in-one-{pkgver}")).hexdigest()
    result = _run(release, tmp_path, f"v{pkgver}", tarball)
    assert result.returncode == 0, result.stderr
    pkgbuild = (tmp_path / "out" / "PKGBUILD").read_text()
    srcinfo = (tmp_path / "out" / ".SRCINFO").read_text()
    assert f"sha256sums=('{sum_}')\n" in pkgbuild
    assert f"\tsha256sums = {sum_}\n" in srcinfo
    assert "SKIP" not in pkgbuild + srcinfo
    # Nothing else changed: the checked-in files with the sum put in and the TODO gone.
    original = (release / "PKGBUILD").read_text()
    assert [line for line in pkgbuild.splitlines() if not line.startswith("#")] == [
        line.replace("'SKIP'", f"'{sum_}'")
        for line in original.splitlines()
        if not line.startswith("#")
    ]
    assert srcinfo == (release / ".SRCINFO").read_text().replace("SKIP", sum_)
    assert not any((release / name).read_text().count(sum_) for name in ("PKGBUILD", ".SRCINFO"))


@pytest.mark.parametrize("tag", ["v0.0.1", "0.4.0", "v0.4.0-rc1"])
def test_a_tag_that_is_not_v_pkgver_is_refused(release: Path, tmp_path: Path, tag: str) -> None:
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, f"wall-in-one-{_pkgver()}")
    if tag == f"v{_pkgver()}":
        pytest.skip("this is the current tag")
    result = _run(release, tmp_path, tag, tarball)
    assert result.returncode == 1
    assert "is not v" in result.stderr
    assert not (tmp_path / "out" / "PKGBUILD").exists()


def test_a_tarball_with_another_top_directory_is_refused(release: Path, tmp_path: Path) -> None:
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, "wall-in-one-9.9.9")
    result = _run(release, tmp_path, f"v{_pkgver()}", tarball)
    assert result.returncode == 1
    assert "outside" in result.stderr
    assert not (tmp_path / "out" / "PKGBUILD").exists()


def test_a_tarball_with_a_stray_top_level_entry_is_refused(release: Path, tmp_path: Path) -> None:
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, f"wall-in-one-{_pkgver()}", extra=("stray",))
    assert _run(release, tmp_path, f"v{_pkgver()}", tarball).returncode == 1


def test_something_that_is_not_a_tarball_is_refused(release: Path, tmp_path: Path) -> None:
    tarball = tmp_path / "t.tar.gz"
    tarball.write_bytes(b"<html>Not Found</html>")
    result = _run(release, tmp_path, f"v{_pkgver()}", tarball)
    assert result.returncode == 1
    assert "not a gzip tarball" in result.stderr


@pytest.mark.parametrize("name", ["PKGBUILD", ".SRCINFO"])
def test_files_not_in_the_expected_shape_are_refused(
    release: Path, tmp_path: Path, name: str
) -> None:
    """If the SKIP line is not there to replace, a SKIP might survive: so it refuses."""
    path = release / name
    path.write_text(path.read_text().replace("SKIP", "SKIP' 'SKIP" if name == "PKGBUILD" else "x"))
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, f"wall-in-one-{_pkgver()}")
    result = _run(release, tmp_path, f"v{_pkgver()}", tarball)
    assert result.returncode == 1
    assert not (tmp_path / "out" / "PKGBUILD").exists()


def test_a_skip_elsewhere_in_the_pkgbuild_stops_the_release(release: Path, tmp_path: Path) -> None:
    path = release / "PKGBUILD"
    path.write_text(path.read_text() + "\nmd5sums=('SKIP')\n")
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, f"wall-in-one-{_pkgver()}")
    result = _run(release, tmp_path, f"v{_pkgver()}", tarball)
    assert result.returncode == 1
    assert "SKIP would remain" in result.stderr
    assert not (tmp_path / "out" / "PKGBUILD").exists()


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}


def _alias(kind: str, source: Path, tmp_path: Path) -> str:
    if kind == "same":
        return str(source)
    if kind == "dot":
        return "."
    if kind == "dotdot":
        return "../arch"
    link = tmp_path / "alias"
    link.symlink_to(source, target_is_directory=True)
    return str(link)


@pytest.mark.parametrize("kind", ["same", "dot", "dotdot", "symlink"])
def test_an_output_directory_that_is_the_packaging_directory_is_refused_and_nothing_is_lost(
    release: Path, tmp_path: Path, kind: str
) -> None:
    """Redirecting into a file being read empties it: R40-4 found both originals at 0 bytes."""
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, f"wall-in-one-{_pkgver()}")
    before = _snapshot(release)
    result = subprocess.run(
        ["bash", str(release / "aur-release.sh"), f"v{_pkgver()}", _alias(kind, release, tmp_path)],
        capture_output=True,
        text=True,
        cwd=release,
        env={**os.environ, "TMPDIR": str(tmp_path), "WIO_AUR_TARBALL": str(tarball)},
    )
    assert result.returncode == 1
    assert "packaging directory" in result.stderr
    assert _snapshot(release) == before


@pytest.mark.parametrize("name", ["PKGBUILD", ".SRCINFO"])
def test_an_output_directory_with_a_file_of_that_name_is_refused(
    release: Path, tmp_path: Path, name: str
) -> None:
    """Including a symlink at that name, which would write through to the original."""
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, f"wall-in-one-{_pkgver()}")
    out = tmp_path / "out"
    out.mkdir()
    (out / name).symlink_to(release / name)
    before = _snapshot(release)
    result = _run(release, tmp_path, f"v{_pkgver()}", tarball)
    assert result.returncode == 1
    assert "already exists" in result.stderr
    assert _snapshot(release) == before


def _checkout(tmp_path: Path) -> Path:
    """A fake checkout: the script, a PKGBUILD, and files a build must never delete."""
    repo = tmp_path / "repo"
    arch = repo / "packaging" / "arch"
    arch.mkdir(parents=True)
    for name in ("local-build.sh", "PKGBUILD", ".SRCINFO"):
        shutil.copy(ARCH / name, arch / name)
    (repo / "keep").write_text("x")
    (repo / "service" / "src").mkdir(parents=True)
    (repo / "service" / "src" / "main.rs").write_text("fn main() {}\n")
    (tmp_path / "link").symlink_to(arch, target_is_directory=True)
    return repo


@pytest.mark.parametrize(
    ("target", "cwd", "said"),
    [
        ("packaging/arch", None, "inside the checkout"),
        (".", None, "inside the checkout"),
        ("service", None, "inside the checkout"),
        ("service/src", None, "inside the checkout"),
        ("../link", "service", "inside the checkout"),
        # A relative path is the caller's, not the repository's.
        ("packaging/arch", "packaging", "inside the checkout"),
        ("../packaging/arch", "service", "inside the checkout"),
        # Above the checkout, and a directory that is not this script's own.
        ("..", None, "contains the checkout"),
        ("../../other", "service", "not empty"),
    ],
    ids=[
        "packaging",
        "repo",
        "service",
        "service-src",
        "symlink",
        "relative-from-repo",
        "relative-from-service",
        "parent",
        "unrelated-non-empty",
    ],
)
def test_local_build_refuses_an_output_in_or_around_the_checkout_and_deletes_nothing(
    tmp_path: Path, target: str, cwd: str | None, said: str
) -> None:
    repo = _checkout(tmp_path)
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "precious").write_text("p")
    start = repo / cwd if cwd else repo
    before = {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    result = subprocess.run(
        ["bash", str(repo / "packaging" / "arch" / "local-build.sh")],
        capture_output=True,
        text=True,
        cwd=start,
        env={**os.environ, "WIO_ARCH_OUT": target, "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 1, result.stderr
    assert said in result.stderr
    assert {p: p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before


@pytest.mark.parametrize("planted", ("file-link", "directory", "directory-link"))
def test_a_release_output_that_a_symlink_appears_in_after_the_check_is_not_written_through(
    release: Path, tmp_path: Path, planted: str
) -> None:
    """The check-then-write window (aur-release.sh made a work dir between the two).

    Whatever appears at the output name meanwhile -- a link to the checked-in
    PKGBUILD, a directory, or a link to a directory elsewhere -- publication
    refuses, and writes nothing through it or into it.
    """
    tarball = tmp_path / "t.tar.gz"
    _tarball(tarball, f"wall-in-one-{_pkgver()}")
    out = tmp_path / "out"
    shim = tmp_path / "bin"
    shim.mkdir()
    real = shutil.which("mktemp")
    assert real
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    plant = {
        "file-link": f'ln -s "{release}/PKGBUILD" "{out}/PKGBUILD"',
        "directory": f'mkdir "{out}/PKGBUILD"',
        "directory-link": f'ln -s "{elsewhere}" "{out}/PKGBUILD"',
    }[planted]
    (shim / "mktemp").write_text(
        "#!/bin/sh\n"
        f'[ -e "{tmp_path}/planted" ] || {{ {plant}; '
        f': > "{tmp_path}/planted"; }}\n'
        f'exec "{real}" "$@"\n'
    )
    (shim / "mktemp").chmod(0o755)
    before = _snapshot(release)
    result = subprocess.run(
        ["bash", str(release / "aur-release.sh"), f"v{_pkgver()}", str(out)],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "PATH": f"{shim}:{os.environ['PATH']}",
            "TMPDIR": str(tmp_path),
            "WIO_AUR_TARBALL": str(tarball),
        },
    )
    assert (tmp_path / "planted").exists(), "the shim never ran"
    assert result.returncode == 1, result.stdout + result.stderr
    assert _snapshot(release) == before
    assert not any(elsewhere.iterdir())
    assert not (out / "PKGBUILD" / "PKGBUILD").exists()
