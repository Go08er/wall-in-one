"""Crash durability of provider media installation.

Everything is local and beneath ``tmp_path``. The subprocess tests terminate
the child with ``os._exit`` specifically so Python cleanup cannot make a false
green out of a process-death window.
"""

from __future__ import annotations

import base64
import gc
import hashlib
import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

import pytest

from wall_in_one import file_io
from wall_in_one.library import manage, scan, stills
from wall_in_one.library.manage import ManageError
from wall_in_one.library.model import Ownership
from wall_in_one.providers import download, http
from wall_in_one.providers.base import ProviderError


def _staged(directory: Path, name: str = "payload") -> Path:
    suffix = (name + "00000000")[: download.TEMPORARY_SUFFIX_LENGTH]
    path = directory / f"{download.MEDIA_STAGING_PREFIX}{suffix}"
    path.write_bytes(b"validated media bytes")
    path.chmod(0o600)
    return path


def _provenance(
    provider: str,
    media: Path,
    contents: bytes = b"validated media bytes",
) -> bytes:
    return download.encode_sidecar(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": provider,
            "path": str(media),
            "bytes": len(contents),
            "sha256": hashlib.sha256(contents).hexdigest(),
        }
    )


def _assert_bound_provenance(actual: bytes, original: bytes) -> dict[str, int]:
    parsed_document: object = json.loads(actual)
    parsed_expected: object = json.loads(original)
    assert isinstance(parsed_document, dict)
    assert isinstance(parsed_expected, dict)
    document = parsed_document
    expected = parsed_expected
    generation = document.pop("media_generation")
    assert document == expected
    assert isinstance(generation, dict)
    assert set(generation) == {"device", "inode", "bytes", "mtime_ns", "ctime_ns"}
    device = generation["device"]
    inode = generation["inode"]
    size = generation["bytes"]
    mtime_ns = generation["mtime_ns"]
    ctime_ns = generation["ctime_ns"]
    assert type(device) is int
    assert type(inode) is int
    assert type(size) is int
    assert type(mtime_ns) is int
    assert type(ctime_ns) is int
    return {
        "device": device,
        "inode": inode,
        "bytes": size,
        "mtime_ns": mtime_ns,
        "ctime_ns": ctime_ns,
    }


def _spoof_identity(
    status: os.stat_result,
    identity: file_io.PathIdentity,
) -> os.stat_result:
    """Model immediate same-type inode reuse while retaining other evidence."""
    fields = list(status)
    fields[stat.ST_DEV] = identity[0]
    fields[stat.ST_INO] = identity[1]
    return os.stat_result(fields)


def _open_fd_count() -> int:
    gc.collect()
    return len(os.listdir("/proc/self/fd"))


def _visible_entries(directory: Path) -> set[Path]:
    """Entries other than the exact-inode safe-retention namespace."""
    return {path for path in directory.iterdir() if path.name != file_io.RETAINED_ENTRY_DIRECTORY}


def _assert_zeroed_regular_residue(directory: Path, *, count: int) -> None:
    retained_directory = directory / file_io.RETAINED_ENTRY_DIRECTORY
    retained = tuple(retained_directory.iterdir()) if retained_directory.exists() else ()
    regulars = tuple(path for path in retained if path.is_file())
    assert len(regulars) == count
    assert all(path.stat().st_size == 0 for path in regulars)
    assert all(path.is_file() or path.is_dir() for path in retained)


def test_atomic_write_fdopen_failure_closes_the_owned_creation_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_fds = _open_fd_count()
    destination = tmp_path / "sidecar.json"

    def fail_fdopen(*_arguments: object, **_keywords: object) -> object:
        raise OSError("injected fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)

    with pytest.raises(ProviderError, match="injected fdopen failure"):
        download._atomic_write(
            destination,
            b"{}\n",
            prefix=download.SIDECAR_STAGING_PREFIX,
        )

    assert not destination.exists()
    assert not any(
        path.name.startswith(download.SIDECAR_STAGING_PREFIX) for path in tmp_path.iterdir()
    )
    _assert_zeroed_regular_residue(tmp_path, count=1)
    assert _open_fd_count() == initial_fds


def test_atomic_write_fingerprint_failure_retires_the_exact_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_fds = _open_fd_count()
    destination = tmp_path / "sidecar.json"
    accesses = 0

    def fail_second_access(pin: file_io.PinnedPath) -> file_io.FileFingerprint:
        nonlocal accesses
        if pin.path.name.startswith(download.SIDECAR_STAGING_PREFIX):
            accesses += 1
            if accesses == 2:
                raise OSError("injected staging fingerprint failure")
        return file_io.file_fingerprint(pin.status())

    monkeypatch.setattr(
        file_io.PinnedPath,
        "fingerprint",
        property(fail_second_access),
    )

    with pytest.raises(ProviderError, match="injected staging fingerprint failure"):
        download._atomic_write(
            destination,
            b"{}\n",
            prefix=download.SIDECAR_STAGING_PREFIX,
        )

    assert accesses == 2
    assert not destination.exists()
    assert not any(
        path.name.startswith(download.SIDECAR_STAGING_PREFIX) for path in tmp_path.iterdir()
    )
    _assert_zeroed_regular_residue(tmp_path, count=1)
    assert _open_fd_count() == initial_fds


def test_postcommit_sidecar_fdopen_failure_leaves_media_user_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    initial_fds = _open_fd_count()

    def fail_fdopen(*_arguments: object, **_keywords: object) -> object:
        raise OSError("injected sidecar fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)

    with pytest.raises(ProviderError, match="injected sidecar fdopen failure"):
        download.install(
            staged,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            b"{}\n",
        )

    assert _visible_entries(directory) == {marker, destination}
    assert scan.download_provenance(destination) is None
    _assert_zeroed_regular_residue(directory, count=1)
    assert download._STAGED_FILES == {}
    assert _open_fd_count() == initial_fds


def test_postcommit_sidecar_fingerprint_failure_closes_both_capabilities(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    initial_fds = _open_fd_count()
    sidecar_accesses = 0

    def fail_second_sidecar_access(pin: file_io.PinnedPath) -> file_io.FileFingerprint:
        nonlocal sidecar_accesses
        if pin.path.name.startswith(download.SIDECAR_STAGING_PREFIX):
            sidecar_accesses += 1
            if sidecar_accesses == 2:
                raise OSError("injected sidecar fingerprint failure")
        return file_io.file_fingerprint(pin.status())

    monkeypatch.setattr(
        file_io.PinnedPath,
        "fingerprint",
        property(fail_second_sidecar_access),
    )

    with pytest.raises(ProviderError, match="injected sidecar fingerprint failure"):
        download.install(
            staged,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert sidecar_accesses == 2
    assert _visible_entries(directory) == {marker, destination}
    assert scan.download_provenance(destination) is None
    _assert_zeroed_regular_residue(directory, count=1)
    assert download._STAGED_FILES == {}
    assert _open_fd_count() == initial_fds


def test_install_syncs_media_before_publishing_exact_sidecar(
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
        "media-move",
        "directory-fsync",
        "sidecar-move",
        "directory-fsync",
    ]


def test_install_cannot_publish_inside_a_removal_lifecycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    suffix = download.MOTIONBGS_LOCATION.sidecar_suffix
    sidecar = destination.with_name(destination.name + suffix)
    payload = _provenance("MotionBGS", destination)
    monkeypatch.setattr(stills, "LIFECYCLE_LOCK_TIMEOUT_SECONDS", 0.05)

    with stills.media_path_lifecycle_lock(destination):
        with pytest.raises(ProviderError) as caught:
            download.install(staged, destination, suffix, payload)

        assert caught.value.kind == "local-io"
        assert _visible_entries(directory) == {marker, staged}

    download.install(staged, destination, suffix, payload)

    assert _visible_entries(directory) == {marker, destination, sidecar}
    generation = _assert_bound_provenance(sidecar.read_bytes(), payload)
    status = destination.stat()
    assert tuple(generation.values()) == (
        status.st_dev,
        status.st_ino,
        status.st_size,
        status.st_mtime_ns,
        status.st_ctime_ns,
    )


@pytest.mark.parametrize(
    ("location", "provider"),
    (
        (download.MOTIONBGS_LOCATION, "MotionBGS"),
        (download.WALLHAVEN_LOCATION, "Wallhaven"),
    ),
)
def test_media_conflict_cannot_publish_authority_for_unrelated_same_size_media(
    tmp_path: Path,
    location: download.ManagedLocation,
    provider: str,
) -> None:
    directory, _marker = download.managed_directory(tmp_path, location)
    staged = _staged(directory)
    staged_contents = staged.read_bytes()
    destination = directory / "collision.mp4"
    unrelated = b"x" * len(staged_contents)
    assert unrelated != staged_contents
    destination.write_bytes(unrelated)

    with pytest.raises(ProviderError) as caught:
        download.install(
            staged,
            destination,
            location.sidecar_suffix,
            _provenance(provider, destination, staged_contents),
        )

    assert caught.value.kind == "conflict"
    sidecar = destination.with_name(destination.name + location.sidecar_suffix)
    assert not sidecar.exists()
    assert destination.read_bytes() == unrelated
    assert scan.download_provenance(destination) is None
    found = next(item for item in scan.scan((tmp_path,)).items if item.path == destination)
    assert found.ownership is Ownership.USER
    assert not found.deletable
    fingerprint = file_io.regular_file_fingerprint(destination)
    with pytest.raises(ManageError) as removal:
        manage.remove(
            found,
            (tmp_path,),
            expected_source=fingerprint[:2],
            expected_fingerprint=fingerprint,
            operation_token="0123456789abcdef0123456789abcdef",
        )
    assert removal.value.kind == "not-ours"
    assert destination.read_bytes() == unrelated


@pytest.mark.parametrize("cancel_at", (1, 2))
def test_install_cancellation_leaves_only_safe_precommit_states(
    tmp_path: Path,
    cancel_at: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    calls = 0
    sidecar_pins: list[file_io.PinnedPath] = []
    real_pin_created_temporary = download._pin_created_temporary
    initial_fds = _open_fd_count()

    def capture_sidecar_pin(descriptor: int, path: Path) -> file_io.PinnedPath:
        pin = real_pin_created_temporary(descriptor, path)
        sidecar_pins.append(pin)
        return pin

    monkeypatch.setattr(download, "_pin_created_temporary", capture_sidecar_pin)

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
    retained = transfer._staged_file
    assert retained is not None
    payload = _provenance("MotionBGS", destination)
    with (
        pytest.raises(ProviderError) as caught,
        transfer,
    ):
        assert transfer.path is not None
        download.install(
            transfer.path,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            payload,
            cancelled=cancelled,
        )

    assert caught.value.kind == "cancelled"
    assert _visible_entries(directory) == {marker}
    assert not destination.exists()
    assert not sidecar.exists()
    assert download._STAGED_FILES == {}
    with pytest.raises(OSError):
        retained.pin.status()
    assert len(sidecar_pins) == 0
    for pin in sidecar_pins:
        with pytest.raises(OSError):
            pin.status()
    _assert_zeroed_regular_residue(
        directory,
        count=1,
    )
    assert _open_fd_count() == initial_fds


def test_bare_path_install_failure_releases_its_borrow_and_remains_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    retained_files: list[download._StagedFile] = []
    real_retain = download._retain_staged_file
    calls = 0
    initial_fds = _open_fd_count()

    def capture_borrow(
        path: Path,
        *,
        pinned_source: file_io.PinnedPath | None = None,
        expected_fingerprint: file_io.FileFingerprint | None = None,
    ) -> download._StagedFile:
        retained = real_retain(
            path,
            pinned_source=pinned_source,
            expected_fingerprint=expected_fingerprint,
        )
        retained_files.append(retained)
        return retained

    def cancelled() -> bool:
        nonlocal calls
        calls += 1
        return calls == 2

    monkeypatch.setattr(download, "_retain_staged_file", capture_borrow)

    with pytest.raises(ProviderError) as caught:
        download.install(
            staged,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
            cancelled=cancelled,
        )

    assert caught.value.kind == "cancelled"
    assert staged.read_bytes() == b"validated media bytes"
    assert _visible_entries(directory) == {marker, staged}
    _assert_zeroed_regular_residue(directory, count=0)
    assert not destination.exists() and not sidecar.exists()
    assert len(retained_files) == 1
    retained = retained_files[0]
    assert retained.references == 0 and not retained.registered
    with pytest.raises(OSError):
        retained.pin.status()
    assert download._STAGED_FILES == {}
    assert _open_fd_count() == initial_fds

    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    os.utime(staged, (old, old))
    assert download.recover_abandoned(
        directory,
        download.MOTIONBGS_LOCATION,
        now=now,
    ) == (staged,)
    assert _visible_entries(directory) == {marker}
    _assert_zeroed_regular_residue(directory, count=1)
    assert _open_fd_count() == initial_fds


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
        # Both observations precede the media commit. Once media moves, a
        # cancellation request cannot interrupt exact-authority publication.
        return calls == 3

    download.install(
        staged,
        destination,
        download.MOTIONBGS_LOCATION.sidecar_suffix,
        _provenance("MotionBGS", destination),
        cancelled=cancelled,
    )

    assert calls == 2
    assert destination.read_bytes() == b"validated media bytes"
    assert sidecar.is_file()


def test_postcommit_sync_failure_never_unlinks_a_replaced_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    sentinel = b"concurrent sidecar replacement"
    syncs = 0

    def fail_final_sidecar_sync(_directory: Path) -> None:
        nonlocal syncs
        syncs += 1
        if syncs == 2:
            assert sidecar.is_file() and destination.is_file()
            sidecar.unlink()
            sidecar.write_bytes(sentinel)
            raise OSError("sidecar directory sync failed")

    monkeypatch.setattr(
        "wall_in_one.providers.download.paths.fsync_directory",
        fail_final_sidecar_sync,
    )
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    with pytest.raises(ProviderError) as caught, transfer:
        assert transfer.path is not None
        download.install(
            transfer.path,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert "irreversible media commit" in str(caught.value)
    assert _visible_entries(directory) == {marker, destination, sidecar}
    assert sidecar.read_bytes() == sentinel


def test_late_sidecar_conflict_leaves_committed_media_user_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    sentinel = b"replacement installed before provenance publication"
    real_bind = download._bind_provider_sidecar
    binds = 0

    def bind(*args: object, **kwargs: object) -> bytes:
        nonlocal binds
        binds += 1
        result = real_bind(*args, **kwargs)  # type: ignore[arg-type]
        if binds == 2:
            assert destination.is_file() and not sidecar.exists()
            sidecar.write_bytes(sentinel)
        return result

    monkeypatch.setattr(download, "_bind_provider_sidecar", bind)

    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    with pytest.raises(ProviderError) as caught, transfer:
        assert transfer.path is not None
        download.install(
            transfer.path,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert "irreversible media commit" in str(caught.value)
    assert _visible_entries(directory) == {marker, destination, sidecar}
    _assert_zeroed_regular_residue(directory, count=1)
    assert sidecar.read_bytes() == sentinel
    assert scan.download_provenance(destination) is None


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
        assert transfer.path is not None
        download.install(
            transfer.path,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert "irreversible media commit" in str(caught.value)
    assert "durability unknown" in str(caught.value)
    assert _visible_entries(directory) == {marker, destination, sidecar}
    assert destination.read_bytes() == media_sentinel
    assert sidecar.read_bytes() == sidecar_sentinel


def test_media_publication_rejects_same_type_reuse_with_spoofed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    preserved_original = tmp_path / "original-media-stage"
    replacement = b"a different regular-file generation at the staged name"
    initial_fds = _open_fd_count()
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    retained = transfer._staged_file
    assert retained is not None
    expected_identity = retained.fingerprint[:2]
    real_lstat = Path.lstat
    real_rename = file_io._rename_noreplace
    raced = False

    def spoofed_lstat(path: Path) -> os.stat_result:
        status = real_lstat(path)
        if raced and path.name in {staged.name, destination.name}:
            return _spoof_identity(status, expected_identity)
        return status

    def replace_after_final_check(source: Path, target: Path) -> None:
        nonlocal raced
        if source.name == staged.name and target.name == destination.name and not raced:
            source.rename(preserved_original)
            source.write_bytes(replacement)
            raced = True
        real_rename(source, target)

    monkeypatch.setattr(Path, "lstat", spoofed_lstat)
    monkeypatch.setattr(file_io, "_rename_noreplace", replace_after_final_check)

    with pytest.raises(ProviderError) as caught, transfer:
        assert transfer.path is not None
        download.install(
            transfer.path,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert not destination.exists()
    assert not sidecar.exists()
    assert staged.read_bytes() == replacement
    assert preserved_original.read_bytes() == b"validated media bytes"
    assert _visible_entries(directory) == {marker, staged}
    assert download._STAGED_FILES == {}
    assert _open_fd_count() == initial_fds


def test_sidecar_publication_rejects_same_type_reuse_with_spoofed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = Path(str(destination) + download.MOTIONBGS_LOCATION.sidecar_suffix)
    preserved_original = tmp_path / "original-sidecar-stage"
    replacement = b"concurrent regular sidecar staging replacement"
    real_lstat = Path.lstat
    real_rename = file_io._rename_noreplace
    raced_source: Path | None = None
    expected_identity: file_io.PathIdentity | None = None
    initial_fds = _open_fd_count()

    def spoofed_lstat(path: Path) -> os.stat_result:
        status = real_lstat(path)
        if (
            raced_source is not None
            and expected_identity is not None
            and path.name in {raced_source.name, sidecar.name}
        ):
            return _spoof_identity(status, expected_identity)
        return status

    def replace_after_final_check(source: Path, target: Path) -> None:
        nonlocal expected_identity, raced_source
        if target.name == sidecar.name and raced_source is None:
            expected_identity = file_io.path_identity(source)
            raced_source = source
            source.rename(preserved_original)
            source.write_bytes(replacement)
        real_rename(source, target)

    monkeypatch.setattr(Path, "lstat", spoofed_lstat)
    monkeypatch.setattr(file_io, "_rename_noreplace", replace_after_final_check)
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )

    with pytest.raises(ProviderError) as caught, transfer:
        assert transfer.path is not None
        download.install(
            transfer.path,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert raced_source is not None
    assert destination.read_bytes() == b"validated media bytes"
    assert not sidecar.exists()
    assert (directory / raced_source.name).read_bytes() == replacement
    _assert_bound_provenance(
        preserved_original.read_bytes(),
        _provenance("MotionBGS", destination),
    )
    assert _visible_entries(directory) == {
        marker,
        destination,
        directory / raced_source.name,
    }
    _assert_zeroed_regular_residue(directory, count=0)
    assert download._STAGED_FILES == {}
    assert _open_fd_count() == initial_fds


def test_marker_publication_rejects_same_type_reuse_with_spoofed_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    directory = root / download.MANAGED_PARENT / download.MOTIONBGS_LOCATION.directory_name
    marker = directory / download.MOTIONBGS_LOCATION.marker_name
    preserved_original = tmp_path / "original-marker-stage"
    replacement = b"concurrent regular marker staging replacement"
    real_lstat = Path.lstat
    real_rename = file_io._rename_noreplace
    raced_source: Path | None = None
    expected_identity: file_io.PathIdentity | None = None
    marker_pin: file_io.PinnedPath | None = None
    real_pin_created_temporary = download._pin_created_temporary
    initial_fds = _open_fd_count()

    def capture_marker_pin(descriptor: int, path: Path) -> file_io.PinnedPath:
        nonlocal marker_pin
        marker_pin = real_pin_created_temporary(descriptor, path)
        return marker_pin

    def spoofed_lstat(path: Path) -> os.stat_result:
        status = real_lstat(path)
        if (
            raced_source is not None
            and expected_identity is not None
            and path.name in {raced_source.name, marker.name}
        ):
            return _spoof_identity(status, expected_identity)
        return status

    def replace_after_final_check(source: Path, target: Path) -> None:
        nonlocal expected_identity, raced_source
        if target.name == marker.name and raced_source is None:
            expected_identity = file_io.path_identity(source)
            raced_source = source
            source.rename(preserved_original)
            source.write_bytes(replacement)
        real_rename(source, target)

    monkeypatch.setattr(Path, "lstat", spoofed_lstat)
    monkeypatch.setattr(file_io, "_rename_noreplace", replace_after_final_check)
    monkeypatch.setattr(download, "_pin_created_temporary", capture_marker_pin)

    with pytest.raises(ProviderError) as caught:
        download.managed_directory(root, download.MOTIONBGS_LOCATION)

    assert caught.value.kind == "local-io"
    assert raced_source is not None
    assert not marker.exists()
    assert (directory / raced_source.name).read_bytes() == replacement
    assert preserved_original.read_bytes() == download.encode_sidecar(
        download.MOTIONBGS_LOCATION.marker_payload
    )
    assert _visible_entries(directory) == {directory / raced_source.name}
    assert marker_pin is not None
    with pytest.raises(OSError):
        marker_pin.status()
    assert _open_fd_count() == initial_fds


def test_marker_postpublication_failure_preserves_a_manual_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    directory = root / download.MANAGED_PARENT / download.MOTIONBGS_LOCATION.directory_name
    marker = directory / download.MOTIONBGS_LOCATION.marker_name
    sentinel = b"manual marker replacement after publication"
    marker_pin: file_io.PinnedPath | None = None
    real_pin_created_temporary = download._pin_created_temporary
    initial_fds = _open_fd_count()

    def capture_marker_pin(descriptor: int, path: Path) -> file_io.PinnedPath:
        nonlocal marker_pin
        marker_pin = real_pin_created_temporary(descriptor, path)
        return marker_pin

    def replace_before_failed_sync(_directory: Path) -> None:
        assert marker.is_file()
        marker.unlink()
        marker.write_bytes(sentinel)
        raise OSError("forced marker directory sync failure")

    monkeypatch.setattr(download, "_pin_created_temporary", capture_marker_pin)
    monkeypatch.setattr(
        "wall_in_one.providers.download.paths.fsync_directory",
        replace_before_failed_sync,
    )

    with pytest.raises(ProviderError) as caught:
        download.managed_directory(root, download.MOTIONBGS_LOCATION)

    assert caught.value.kind == "local-io"
    assert marker.read_bytes() == sentinel
    assert _visible_entries(directory) == {marker}
    assert marker_pin is not None
    with pytest.raises(OSError):
        marker_pin.status()
    assert _open_fd_count() == initial_fds


def test_transfer_reconstruction_shares_one_lease_and_commit_unregisters_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(tmp_path)
    initial_fds = _open_fd_count()
    unregister_calls = 0
    real_unregister = download._unregister_staged_file_locked

    def counted_unregister(staged_file: download._StagedFile) -> None:
        nonlocal unregister_calls
        unregister_calls += 1
        real_unregister(staged_file)

    monkeypatch.setattr(download, "_unregister_staged_file_locked", counted_unregister)
    original = http.Transfer(
        url="https://origin.test/media",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    retained = original._staged_file
    assert retained is not None and retained.references == 1
    validation_path = original.path
    assert validation_path is not None
    assert isinstance(validation_path, download._PinnedReadPath)
    assert validation_path == staged and staged == validation_path
    assert validation_path.parent == staged.parent
    sibling = validation_path.with_name("sibling.mp4")
    assert sibling == staged.with_name("sibling.mp4")
    assert sibling.parent == staged.parent
    assert sibling._staged_file is retained
    wrapper = http.Transfer(
        url="https://authorised.test/media",
        status=original.status,
        content_type=original.content_type,
        size=original.size,
        path=original.path,
    )
    assert wrapper._staged_file is retained
    assert wrapper.path is validation_path
    assert retained.references == 2

    del original
    gc.collect()
    assert retained.references == 1
    destination = tmp_path / "published.mp4"
    assert wrapper.path is not None
    download.install(wrapper.path, destination, ".provider.json", b"{}\n")

    assert destination.read_bytes() == b"validated media bytes"
    assert retained.consumed and not retained.registered
    assert retained.references == 1
    # install() resolved the swapped public Path object back to this exact
    # owner: a fresh borrow could not have consumed ``retained``.
    assert download._STAGED_FILES == {}
    assert unregister_calls == 1
    wrapper.discard()
    wrapper.discard()
    assert retained.references == 0
    assert download._STAGED_FILES == {}
    with pytest.raises(OSError):
        retained.pin.status()
    assert _open_fd_count() == initial_fds


def test_abandoned_transfer_destructor_releases_its_pin_and_stage_is_recoverable(
    tmp_path: Path,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    initial_fds = _open_fd_count()
    transfer = http.Transfer(
        url="https://origin.test/media",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    retained = transfer._staged_file
    assert retained is not None and _open_fd_count() == initial_fds + 1

    del transfer
    gc.collect()

    assert staged.is_file()
    assert retained.references == 0 and not retained.registered
    assert download._STAGED_FILES == {}
    with pytest.raises(OSError):
        retained.pin.status()
    assert _open_fd_count() == initial_fds

    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    os.utime(staged, (old, old))
    assert download.recover_abandoned(
        directory,
        download.MOTIONBGS_LOCATION,
        now=now,
    ) == (staged,)
    assert _visible_entries(directory) == {marker}
    _assert_zeroed_regular_residue(directory, count=1)
    assert _open_fd_count() == initial_fds


def test_stale_duplicate_transfer_handoff_is_rejected_without_a_lease_leak(
    tmp_path: Path,
) -> None:
    staged = _staged(tmp_path)
    initial_fds = _open_fd_count()
    original = http.Transfer(
        url="https://origin.test/media",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )
    retained = original._staged_file
    assert retained is not None
    preserved_original = tmp_path / "retained-original"
    staged.rename(preserved_original)
    staged.write_bytes(b"replacement generation")
    replacement_pin = file_io.pin_regular_path(staged)
    replacement_fingerprint = replacement_pin.fingerprint

    with pytest.raises(file_io.PathChangedError):
        http.Transfer(
            url="https://authorised.test/media",
            status=200,
            content_type="video/mp4",
            size=staged.stat().st_size,
            path=staged,
            _staged_pin=replacement_pin,
            _staged_fingerprint=replacement_fingerprint,
        )

    with pytest.raises(OSError):
        replacement_pin.status()
    assert retained.references == 1 and retained.registered
    original.discard()
    assert staged.read_bytes() == b"replacement generation"
    assert preserved_original.read_bytes() == b"validated media bytes"
    assert retained.references == 0
    assert download._STAGED_FILES == {}
    with pytest.raises(OSError):
        retained.pin.status()
    assert _open_fd_count() == initial_fds


def test_concurrent_transfer_references_consume_and_unregister_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged(tmp_path)
    initial_fds = _open_fd_count()
    unregister_calls = 0
    real_unregister = download._unregister_staged_file_locked

    def counted_unregister(staged_file: download._StagedFile) -> None:
        nonlocal unregister_calls
        unregister_calls += 1
        real_unregister(staged_file)

    monkeypatch.setattr(download, "_unregister_staged_file_locked", counted_unregister)

    def retain(_index: int) -> http.Transfer:
        return http.Transfer(
            url="https://origin.test/media",
            status=200,
            content_type="video/mp4",
            size=staged.stat().st_size,
            path=staged,
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        transfers = tuple(pool.map(retain, range(16)))
    retained = transfers[0]._staged_file
    assert retained is not None
    assert all(transfer._staged_file is retained for transfer in transfers)
    assert retained.references == len(transfers)

    with ThreadPoolExecutor(max_workers=8) as pool:
        tuple(pool.map(lambda transfer: transfer.discard(), transfers))

    assert not staged.exists()
    assert retained.consumed and not retained.registered
    assert retained.references == 0
    assert download._STAGED_FILES == {}
    assert unregister_calls == 1
    with pytest.raises(OSError):
        retained.pin.status()
    assert _open_fd_count() == initial_fds


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
        if source.name == staged.name and not raced:
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
        expected_fingerprint: file_io.FileFingerprint | None = None,
        pinned_source: file_io.PinnedPath | None = None,
    ) -> file_io.ClaimedPath:
        claim = real_claim(
            path,
            expected_identity=expected_identity,
            operation_token=operation_token,
            expected_fingerprint=expected_fingerprint,
            pinned_source=pinned_source,
        )
        if path.name == orphan.name:
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


def test_recovery_stays_bound_to_retained_directory_after_a_post_marker_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    abandoned = _staged(directory, "inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _staged(outside, "outside")
    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    os.utime(abandoned, (old, old))
    os.utime(victim, (old, old))
    logical_directory = Path(os.fspath(directory))
    preserved_directory = tmp_path / "retained-provider-directory"
    real_read = file_io.read_regular_bytes
    swapped = False

    def read_marker_then_swap(path: Path, maximum: int) -> bytes | None:
        nonlocal swapped
        payload = real_read(path, maximum)
        if path.name == marker.name and not swapped:
            swapped = True
            logical_directory.rename(preserved_directory)
            logical_directory.symlink_to(outside, target_is_directory=True)
        return payload

    monkeypatch.setattr(file_io, "read_regular_bytes", read_marker_then_swap)

    with pytest.raises(ProviderError) as caught:
        download.recover_abandoned(
            directory,
            download.MOTIONBGS_LOCATION,
            now=now,
        )

    assert caught.value.kind == "invalid-path"
    assert victim.read_bytes() == b"validated media bytes"
    assert not (preserved_directory / abandoned.name).exists()
    assert (preserved_directory / marker.name).is_file()
    assert logical_directory.is_symlink()


def test_install_publication_stays_in_retained_directory_after_a_name_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    directory, marker = download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)
    staged = _staged(directory)
    destination = directory / "aurora.mp4"
    sidecar = destination.with_name(destination.name + download.MOTIONBGS_LOCATION.sidecar_suffix)
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "precious"
    sentinel.write_bytes(b"outside data")
    logical_directory = Path(os.fspath(directory))
    preserved_directory = tmp_path / "retained-provider-directory"
    real_rename = file_io._rename_noreplace
    swapped = False

    def swap_before_sidecar_publication(source: Path, target: Path) -> None:
        nonlocal swapped
        if target.name == sidecar.name and not swapped:
            swapped = True
            logical_directory.rename(preserved_directory)
            logical_directory.symlink_to(outside, target_is_directory=True)
        real_rename(source, target)

    monkeypatch.setattr(file_io, "_rename_noreplace", swap_before_sidecar_publication)
    transfer = http.Transfer(
        url="https://motionbgs.com/media/42/aurora.mp4",
        status=200,
        content_type="video/mp4",
        size=staged.stat().st_size,
        path=staged,
    )

    with pytest.raises(ProviderError, match="irreversible media commit") as caught, transfer:
        assert transfer.path is not None
        download.install(
            transfer.path,
            destination,
            download.MOTIONBGS_LOCATION.sidecar_suffix,
            _provenance("MotionBGS", destination),
        )

    assert caught.value.kind == "local-io"
    assert set(outside.iterdir()) == {sentinel}
    assert (preserved_directory / marker.name).is_file()
    assert (preserved_directory / destination.name).read_bytes() == b"validated media bytes"
    assert (preserved_directory / sidecar.name).is_file()
    assert logical_directory.is_symlink()


def test_managed_directory_refuses_a_non_directory_component(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    blocker = root / download.MANAGED_PARENT
    blocker.write_bytes(b"not a directory")

    with pytest.raises(ProviderError) as caught:
        download.managed_directory(root, download.WALLHAVEN_LOCATION)

    assert caught.value.kind == "invalid-path"
    assert blocker.read_bytes() == b"not a directory"


def test_managed_directory_capability_closes_with_its_logical_paths(tmp_path: Path) -> None:
    initial_fds = _open_fd_count()

    def create_and_release() -> int:
        directory, _marker = download.managed_directory(
            tmp_path,
            download.MOTIONBGS_LOCATION,
        )
        assert isinstance(directory, download._AnchoredPath)
        descriptor = directory._directory_anchor.descriptor
        os.fstat(descriptor)
        return descriptor

    descriptor = create_and_release()
    gc.collect()

    with pytest.raises(OSError):
        os.fstat(descriptor)
    assert _open_fd_count() == initial_fds


def test_managed_directory_closes_every_capability_when_parent_close_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial_fds = _open_fd_count()
    real_close = os.close
    closes = 0

    def close_then_fail_once(descriptor: int) -> None:
        nonlocal closes
        closes += 1
        real_close(descriptor)
        if closes == 1:
            raise OSError("injected parent close failure")

    monkeypatch.setattr(os, "close", close_then_fail_once)

    with pytest.raises(OSError, match="injected parent close failure"):
        download.managed_directory(tmp_path, download.MOTIONBGS_LOCATION)

    assert closes == 3
    assert _open_fd_count() == initial_fds


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
def test_process_death_leaves_only_fail_closed_publication_states(
    tmp_path: Path,
    fail_after: int,
) -> None:
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
    assert destination.is_file()
    assert sidecar.is_file() is (fail_after == 2)
    if fail_after == 1:
        destination.write_bytes(b"x" * len(b"validated media bytes"))

    now = 10_000_000.0
    old = now - download.STAGING_MAX_AGE_SECONDS - 1
    for path in directory.iterdir():
        if path not in {marker, destination}:
            os.utime(path, (old, old), follow_symlinks=False)
    download.recover_abandoned(directory, download.MOTIONBGS_LOCATION, now=now)

    if fail_after == 1:
        assert _visible_entries(directory) == {marker, destination}
        assert destination.read_bytes() == b"x" * len(b"validated media bytes")
        assert scan.download_provenance(destination) is None
    else:
        assert _visible_entries(directory) == {marker, destination, sidecar}
        assert destination.read_bytes() == b"validated media bytes"
        _assert_bound_provenance(sidecar.read_bytes(), payload)
