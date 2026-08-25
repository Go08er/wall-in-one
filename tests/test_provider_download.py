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
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from wall_in_one import file_io
from wall_in_one.providers import download, http
from wall_in_one.providers.base import ProviderError


def _staged(directory: Path, name: str = "payload") -> Path:
    suffix = (name + "00000000")[: download.TEMPORARY_SUFFIX_LENGTH]
    path = directory / f"{download.MEDIA_STAGING_PREFIX}{suffix}"
    path.write_bytes(b"validated media bytes")
    path.chmod(0o600)
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
    real_rename = file_io._rename_noreplace

    def moved(source: Path, target: Path) -> None:
        real_rename(source, target)
        events.append("sidecar-move" if str(target).endswith(".motionbgs.json") else "media-move")

    monkeypatch.setattr(file_io, "_rename_noreplace", moved)
    monkeypatch.setattr(
        "wall_in_one.providers.download.paths.fsync_directory",
        lambda _directory: events.append("directory-fsync"),
    )

    download.install(staged, destination, ".motionbgs.json", b"{}\n")

    assert events[:4] == [
        "sidecar-move",
        "directory-fsync",
        "media-move",
        "directory-fsync",
    ]


@pytest.mark.parametrize("cancel_at", (1, 2, 3))
def test_install_cancellation_leaves_only_safe_precommit_states(
    tmp_path: Path, cancel_at: int
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls == cancel_at

    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    payload = _provenance("MotionBGS", destination)
    with (
        pytest.raises(ProviderError) as caught,
        transfer,
    ):
        download.install(
            staged,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            payload,
            cancelled=cancelled,
        )

    assert caught.value.kind == "cancelled"
    expected = {marker, sidecar} if cancel_at == 3 else {marker}
    assert set(directory.iterdir()) == expected
    assert not destination.exists()
    if cancel_at == 3:
        assert sidecar.read_bytes() == payload
    else:
        assert not sidecar.exists()


def test_cancellation_after_irreversible_media_commit_does_not_delete_the_pair(
    tmp_path: Path,
) -> None:
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        # There are exactly three pre-commit observations. A close racing
        # after the media link loses to the already-completed commit.
        return calls == 4

    download.install(
        staged,
        destination,
        download.MOTIONBGS_LOCATION.sidecar_suffix,
        _provenance("MotionBGS", destination),
        cancelled=cancelled,
    )

    assert calls == 3
    assert destination.read_bytes() == b"validated media bytes"
    assert sidecar.is_file()


def test_precommit_failure_never_unlinks_a_replaced_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    sentinel = b"concurrent sidecar replacement"
    syncs = 0

    def fail_first_final_sync(_directory: Path) -> None:
        nonlocal syncs
        syncs += 1
        if syncs == 1:
            assert sidecar.is_file() and not destination.exists()
            sidecar.unlink()
            sidecar.write_bytes(sentinel)
            raise OSError("sidecar directory sync failed")

    monkeypatch.setattr(
        "wall_in_one.providers.download.paths.fsync_directory",
        fail_first_final_sync,
    )
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    with pytest.raises(ProviderError) as caught, transfer:
        download.install(
            staged,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert "irreversible media commit" not in str(caught.value)
    assert set(directory.iterdir()) == {marker, sidecar}
    assert sidecar.read_bytes() == sentinel


def test_precommit_cancellation_never_unlinks_a_replaced_sidecar(tmp_path: Path) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    sentinel = b"replacement installed before cancellation was observed"
    calls = 0

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        if calls == 3:
            assert sidecar.is_file() and not destination.exists()
            sidecar.unlink()
            sidecar.write_bytes(sentinel)
            return True
        return False

    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    with pytest.raises(ProviderError) as caught, transfer:
        download.install(
            staged,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
            cancelled=cancelled,
        )

    assert caught.value.kind == "cancelled"
    assert set(directory.iterdir()) == {marker, sidecar}
    assert sidecar.read_bytes() == sentinel


def test_postcommit_failure_never_unlinks_replaced_final_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    media_sentinel = b"concurrent media replacement"
    sidecar_sentinel = b"concurrent provenance replacement"
    syncs = 0

    def fail_media_sync(_directory: Path) -> None:
        nonlocal syncs
        syncs += 1
        if syncs == 2:
            assert destination.is_file() and sidecar.is_file()
            destination.unlink()
            destination.write_bytes(media_sentinel)
            sidecar.unlink()
            sidecar.write_bytes(sidecar_sentinel)
            raise OSError("media directory sync failed")

    monkeypatch.setattr(
        "wall_in_one.providers.download.paths.fsync_directory",
        fail_media_sync,
    )
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    with pytest.raises(ProviderError) as caught, transfer:
        download.install(
            staged,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert "irreversible media commit" in str(caught.value)
    assert "durability unknown" in str(caught.value)
    assert set(directory.iterdir()) == {marker, destination, sidecar}
    assert destination.read_bytes() == media_sentinel
    assert sidecar.read_bytes() == sidecar_sentinel


def test_recovery_is_old_owned_files_only(tmp_path: Path) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1

    old_stage = _staged(directory, "old")
    recent_stage = _staged(directory, "recent")
    unrelated = directory / ".somebody-elses-temporary"
    unrelated.write_bytes(b"keep")
    prefix_lookalike = directory / f"{download.MEDIA_STAGING_PREFIX}family-video"
    prefix_lookalike.write_bytes(b"keep lookalike")
    prefix_lookalike.chmod(0o600)
    wrong_mode = directory / f"{download.MEDIA_STAGING_PREFIX}abcdefgh"
    wrong_mode.write_bytes(b"keep wrong mode")
    wrong_mode.chmod(0o644)
    linked_stage = directory / f"{download.MEDIA_STAGING_PREFIX}ijklmnop"
    linked_stage_source = directory / "user-hardlink-source"
    linked_stage_source.write_bytes(b"keep linked")
    linked_stage_source.chmod(0o600)
    os.link(linked_stage_source, linked_stage)
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

    for path in (
        old_stage,
        unrelated,
        prefix_lookalike,
        wrong_mode,
        linked_stage,
        linked_stage_source,
        orphan,
        user_json,
        media,
        provenance,
    ):
        os.utime(path, (old, old))
    os.utime(recent_stage, (now, now))

    removed = download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=now)

    assert set(removed) == {old_stage, orphan}
    assert recent_stage.is_file()
    assert unrelated.is_file()
    assert prefix_lookalike.read_bytes() == b"keep lookalike"
    assert wrong_mode.read_bytes() == b"keep wrong mode"
    assert linked_stage.read_bytes() == b"keep linked"
    assert linked_stage_source.read_bytes() == b"keep linked"
    assert user_json.is_file()
    assert media.is_file() and provenance.is_file()
    assert symlink.is_symlink() and sentinel.read_bytes() == b"precious"
    assert marker.is_file()


def test_recovery_restores_a_staging_replacement_that_wins_the_claim_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    staged = _staged(directory, "race")
    os.utime(staged, (old, old))
    original = tmp_path / "prepared-stage"
    real_rename = file_io._rename_noreplace
    raced = False

    def replace_then_rename(source: Path, destination: Path) -> None:
        nonlocal raced
        if source == staged and not raced:
            raced = True
            source.rename(original)
            source.write_bytes(b"late replacement")
        real_rename(source, destination)

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_then_rename)

    assert (
        download.recover_abandoned(
            directory,
            download.MOTIONBGS_LOCATION,
            now=now,
        )
        == ()
    )
    assert staged.read_bytes() == b"late replacement"
    assert original.read_bytes() == b"validated media bytes"


def test_orphan_recovery_revalidates_claimed_contents_before_deletion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, _marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    media = directory / "missing.mp4"
    orphan = Path(str(media) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    orphan.write_bytes(_provenance("MotionBGS", media))
    os.utime(orphan, (old, old))
    real_claim = file_io.claim_for_deletion

    def mutate_after_claim(
        path: Path,
        *,
        expected_identity: file_io.PathIdentity,
        operation_token: str | None = None,
    ) -> file_io.ClaimedPath:
        claim = real_claim(
            path,
            expected_identity=expected_identity,
            operation_token=operation_token,
        )
        if path == orphan:
            claim.path.write_bytes(b'{"notes":"now user-authored"}\n')
            os.utime(claim.path, (old, old))
        return claim

    monkeypatch.setattr(file_io, "claim_for_deletion", mutate_after_claim)

    assert (
        download.recover_abandoned(
            directory,
            download.MOTIONBGS_LOCATION,
            now=now,
        )
        == ()
    )
    assert orphan.read_bytes() == b'{"notes":"now user-authored"}\n'


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


def test_concurrent_first_downloads_converge_on_directories_and_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    parent = root / download.MANAGED_PARENT
    directory = parent / download.MOTIONBGS_LOCATION.directory_name
    marker = directory / download.MOTIONBGS_LOCATION.marker_name
    directory_barriers = {parent: Barrier(2), directory: Barrier(2)}
    marker_barrier = Barrier(2)
    real_mkdir = Path.mkdir
    real_rename = file_io._rename_noreplace

    def racing_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        barrier = directory_barriers.get(path)
        if barrier is not None:
            barrier.wait(timeout=5)
        real_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    def racing_marker_publish(source: Path, destination: Path) -> None:
        if destination == marker:
            marker_barrier.wait(timeout=5)
        real_rename(source, destination)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    monkeypatch.setattr(file_io, "_rename_noreplace", racing_marker_publish)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(
            pool.map(
                lambda _index: download.managed_directory(
                    root,
                    download.MOTIONBGS_LOCATION,
                ),
                range(2),
            )
        )

    assert results == ((directory, marker), (directory, marker))
    assert marker.is_file()


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
real_rename = download.file_io._rename_noreplace
moves = 0

def crashing_rename(source, target):
    global moves
    real_rename(source, target)
    moves += 1
    if moves == fail_after:
        os._exit(77)

download.file_io._rename_noreplace = crashing_rename
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
