"""Crash durability of provider media installation.

Everything is local and beneath ``tmp_path``. The subprocess tests terminate
the child with ``os._exit`` specifically so Python cleanup cannot make a false
green out of a process-death window.
"""

from __future__ import annotations

import base64
import os
import subprocess
import sys
from pathlib import Path

import pytest

from wall_in_one.providers import download
from wall_in_one.providers.base import ProviderError


def _staged(directory: Path, name: str = "payload") -> Path:
    path = directory / f"{download.MEDIA_STAGING_PREFIX}{name}"
    path.write_bytes(b"validated media bytes")
    return path


def _provenance(provider: str, media: Path) -> bytes:
    return download.encode_sidecar(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": provider,
            "path": str(media),
        }
    )


def test_install_publishes_and_syncs_sidecar_before_media(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    events: list[str] = []
    real_link = os.link

    def linked(source: Path, target: Path, *, follow_symlinks: bool = True) -> None:
        real_link(source, target, follow_symlinks=follow_symlinks)
        events.append("sidecar-link" if str(target).endswith(".motionbgs.json") else "media-link")

    monkeypatch.setattr("wall_in_one.providers.download.os.link", linked)
    monkeypatch.setattr(
        "wall_in_one.providers.download.paths.fsync_directory",
        lambda _directory: events.append("directory-fsync"),
    )

    download.install(staged, destination, ".motionbgs.json", b"{}\n")

    assert events[:4] == [
        "sidecar-link",
        "directory-fsync",
        "media-link",
        "directory-fsync",
    ]


def test_recovery_is_old_owned_files_only(tmp_path: Path) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1

    old_stage = _staged(directory, "old")
    recent_stage = _staged(directory, "recent")
    unrelated = directory / ".somebody-elses-temporary"
    unrelated.write_bytes(b"keep")
    missing_media = directory / "missing.mp4"
    orphan = Path(str(missing_media) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    orphan.write_bytes(_provenance("MotionBGS", missing_media))
    user_json = directory / "notes.mp4.motionbgs.json"
    user_json.write_text('{"notes":"keep this"}\n', encoding="utf-8")
    media = directory / "complete.mp4"
    media.write_bytes(b"media")
    provenance = directory / "complete.mp4.motionbgs.json"
    provenance.write_bytes(b"{}")
    sentinel = tmp_path / "sentinel"
    sentinel.write_bytes(b"precious")
    symlink = directory / f"{download.MEDIA_STAGING_PREFIX}link"
    symlink.symlink_to(sentinel)

    for path in (old_stage, unrelated, orphan, user_json, media, provenance):
        os.utime(path, (old, old))
    os.utime(recent_stage, (now, now))

    removed = download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=now)

    assert set(removed) == {old_stage, orphan}
    assert recent_stage.is_file()
    assert unrelated.is_file()
    assert user_json.is_file()
    assert media.is_file() and provenance.is_file()
    assert symlink.is_symlink() and sentinel.read_bytes() == b"precious"
    assert marker.is_file()


@pytest.mark.parametrize("link_component", ("root", "parent", "provider"))
def test_managed_directory_never_follows_a_symlink_outside_the_root(
    tmp_path: Path, link_component: str
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "sentinel"
    sentinel.write_bytes(b"precious")
    real_root = tmp_path / "wallpapers"
    real_root.mkdir()

    if link_component == "root":
        root = tmp_path / "linked-root"
        root.symlink_to(real_root, target_is_directory=True)
    else:
        root = real_root
        parent = root / download.MANAGED_PARENT
        if link_component == "parent":
            parent.symlink_to(outside, target_is_directory=True)
        else:
            parent.mkdir()
            (parent / download.MOTIONBGS_LOCATION.directory_name).symlink_to(
                outside, target_is_directory=True
            )

    with pytest.raises(ProviderError) as caught:
        download.managed_directory(root, download.MOTIONBGS_LOCATION)

    assert caught.value.kind == "invalid-path"
    assert sentinel.read_bytes() == b"precious"
    assert set(outside.iterdir()) == {sentinel}


def test_managed_directory_refuses_a_non_directory_component(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    blocker = root / download.MANAGED_PARENT
    blocker.write_bytes(b"not a directory")

    with pytest.raises(ProviderError) as caught:
        download.managed_directory(root, download.WALLHAVEN_LOCATION)

    assert caught.value.kind == "invalid-path"
    assert blocker.read_bytes() == b"not a directory"


def test_recovery_without_a_valid_marker_cannot_unlink_app_shaped_user_files(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "unmanaged"
    directory.mkdir()
    coincidental = directory / f"{download.MEDIA_STAGING_PREFIX}family-video"
    coincidental.write_bytes(b"precious")

    with pytest.raises(ProviderError) as caught:
        download.recover_abandoned(
            directory,
            download.MOTIONBGS_LOCATION,
            now=download.STAGING_MAX_AGE_SECONDS + 1,
        )

    assert caught.value.kind == "invalid-path"
    assert coincidental.read_bytes() == b"precious"


_CRASH_INSTALL = r"""
import base64
import os
import sys
from pathlib import Path
from wall_in_one.providers import download

staged = Path(sys.argv[1])
destination = Path(sys.argv[2])
suffix = sys.argv[3]
payload = base64.b64decode(sys.argv[4])
fail_after = int(sys.argv[5])
real_link = os.link
links = 0

def crashing_link(source, target, *, follow_symlinks=True):
    global links
    real_link(source, target, follow_symlinks=follow_symlinks)
    links += 1
    if links == fail_after:
        os._exit(77)

download.os.link = crashing_link
download.install(staged, destination, suffix, payload)
"""


@pytest.mark.parametrize("fail_after", (1, 2))
def test_process_death_leaves_only_recoverable_states(tmp_path: Path, fail_after: int) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    suffix = download.MOTIONBGS_LOCATION.sidecar_suffix
    sidecar = Path(str(destination) + suffix)
    payload = _provenance("MotionBGS", destination)
    root = Path(__file__).resolve().parents[1]
    environment = dict(os.environ)
    existing = environment.get("PYTHONPATH", "")
    environment["PYTHONPATH"] = str(root / "src") + (os.pathsep + existing if existing else "")

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            _CRASH_INSTALL,
            str(staged),
            str(destination),
            suffix,
            base64.b64encode(payload).decode("ascii"),
            str(fail_after),
        ],
        cwd=root,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert completed.returncode == 77, completed.stderr
    assert sidecar.is_file()
    assert destination.is_file() is (fail_after == 2)

    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    for path in directory.iterdir():
        if path != marker:
            os.utime(path, (old, old), follow_symlinks=False)
    download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=now)

    if fail_after == 1:
        assert set(directory.iterdir()) == {marker}
    else:
        assert set(directory.iterdir()) == {marker, destination, sidecar}
        assert destination.read_bytes() == b"validated media bytes"
        assert sidecar.read_bytes() == payload
