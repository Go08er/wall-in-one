"""Safe small-file reads never follow or wait on user-writable path types."""

from __future__ import annotations

import errno
import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from wall_in_one import file_io


def _retained_entries(parent: Path) -> tuple[Path, ...]:
    directory = parent / file_io.RETAINED_ENTRY_DIRECTORY
    return tuple(directory.iterdir()) if directory.is_dir() else ()


_CRASHING_DISCARD = """
import os
import sys
from pathlib import Path

from wall_in_one import file_io

source = Path(sys.argv[1])
token = sys.argv[2]
boundary = sys.argv[3]
fingerprint = file_io.regular_file_fingerprint(source)
claim = file_io.claim_for_deletion(
    source,
    expected_identity=fingerprint[:2],
    expected_fingerprint=fingerprint,
    operation_token=token,
)
if boundary == "before-truncate":
    os.ftruncate = lambda _descriptor, _length: os._exit(71)
else:
    file_io.ClaimedPath._retain_discarded_inode = lambda _claim: os._exit(72)
claim.discard()
os._exit(99)
"""


def _crash_during_discard(source: Path, token: str, boundary: str) -> int:
    completed = subprocess.run(
        [sys.executable, "-c", _CRASHING_DISCARD, str(source), token, boundary],
        check=False,
        capture_output=True,
    )
    assert completed.stderr == b""
    return completed.returncode


def test_pinned_path_close_never_retries_a_reused_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"pinned")
    pin = file_io.pin_regular_path(path)
    owned_descriptor = pin.descriptor
    real_close = os.close
    reused_descriptor: int | None = None

    def close_then_reuse(descriptor: int) -> None:
        nonlocal reused_descriptor
        if descriptor == owned_descriptor and reused_descriptor is None:
            real_close(descriptor)
            reused_descriptor = os.open(os.devnull, os.O_RDONLY | os.O_CLOEXEC)
            assert reused_descriptor == owned_descriptor
            raise OSError(errno.EIO, "injected close failure after release")
        real_close(descriptor)

    monkeypatch.setattr(os, "close", close_then_reuse)
    try:
        pin.close()
        pin.close()

        assert pin.descriptor == -1
        assert reused_descriptor is not None
        os.fstat(reused_descriptor)
    finally:
        if reused_descriptor is not None:
            real_close(reused_descriptor)


def test_bounded_read_and_prefix_use_regular_files(tmp_path: Path) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"0123456789")

    assert file_io.read_regular_bytes(path, 10) == b"0123456789"
    assert file_io.read_regular_prefix(path, 4) == b"0123"
    with pytest.raises(file_io.FileReadError, match="9-byte limit"):
        file_io.read_regular_bytes(path, 9)


def test_retained_read_uses_the_pinned_inode_after_its_name_is_replaced(tmp_path: Path) -> None:
    path = tmp_path / "sidecar.json"
    path.write_bytes(b"pinned generation")
    with file_io.pin_regular_path(path) as pin:
        path.rename(tmp_path / "old-sidecar.json")
        path.write_bytes(b"named replacement")

        assert (
            file_io.read_pinned_regular_bytes(
                pin,
                64,
            )
            == b"pinned generation"
        )


def test_retained_read_rejects_in_place_mutation(tmp_path: Path) -> None:
    path = tmp_path / "sidecar.json"
    path.write_bytes(b"original")
    with file_io.pin_regular_path(path) as pin:
        expected = pin.fingerprint
        path.write_bytes(b"mutated contents")

        with pytest.raises(file_io.FileReadError, match="changed"):
            file_io.read_pinned_regular_bytes(
                pin,
                64,
                expected_fingerprint=expected,
            )


def test_retained_hash_stops_at_ceiling_when_inode_grows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "capture.png"
    path.write_bytes(b"four")
    real_read = os.read
    appended = False

    def append_before_read(descriptor: int, count: int) -> bytes:
        nonlocal appended
        if not appended:
            appended = True
            with path.open("ab") as handle:
                handle.write(b"!")
        return real_read(descriptor, count)

    with file_io.pin_regular_path(path) as pin:
        fingerprint = pin.fingerprint
        monkeypatch.setattr(os, "read", append_before_read)
        with pytest.raises(file_io.FileReadError, match="grew beyond its 4-byte hash limit"):
            file_io.hash_pinned_regular(
                pin,
                expected_fingerprint=fingerprint,
                maximum_bytes=4,
            )


@pytest.mark.parametrize("reader", [file_io.read_regular_bytes, file_io.read_regular_prefix])
@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_readers_refuse_links_and_fifos_without_opening_them(
    tmp_path: Path,
    reader: object,
    kind: str,
) -> None:
    target = tmp_path / "candidate"
    if kind == "symlink":
        secret = tmp_path / "secret"
        secret.write_bytes(b"do not follow")
        target.symlink_to(secret)
    else:
        os.mkfifo(target)

    assert callable(reader)
    with pytest.raises(file_io.FileReadError, match="not a regular file"):
        reader(target, 64)


def test_absence_is_distinct_from_an_unsafe_present_path(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert file_io.read_regular_bytes(missing, 64) is None
    assert file_io.read_regular_prefix(missing, 64) is None


def test_root_scoped_pin_rejects_an_existing_symlinked_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "wallpapers"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / "paper.png"
    outside_file.write_bytes(b"outside")
    linked_parent = root / "album"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        file_io.pin_regular_path_beneath(root, linked_parent / outside_file.name)

    assert outside_file.read_bytes() == b"outside"


def test_root_scoped_pin_rejects_an_ancestor_swapped_during_traversal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "wallpapers"
    parent = root / "album"
    parent.mkdir(parents=True)
    source = parent / "paper.png"
    source.write_bytes(b"inside")
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_file = outside / source.name
    outside_file.write_bytes(b"outside")
    archived = tmp_path / "archived-album"
    real_open = os.open
    swapped = False

    def swap_after_parent_open(
        candidate: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal swapped
        descriptor = (
            real_open(candidate, flags, mode)
            if dir_fd is None
            else real_open(candidate, flags, mode, dir_fd=dir_fd)
        )
        if os.fspath(candidate) == parent.name and flags & os.O_DIRECTORY and not swapped:
            swapped = True
            parent.rename(archived)
            parent.symlink_to(outside, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(os, "open", swap_after_parent_open)

    with pytest.raises(file_io.PathChangedError, match="configured root"):
        file_io.pin_regular_path_beneath(root, source)

    assert swapped
    assert (archived / source.name).read_bytes() == b"inside"
    assert outside_file.read_bytes() == b"outside"


def test_atomic_move_restores_a_replacement_that_wins_the_claim_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    original = tmp_path / "original"
    source.write_bytes(b"expected")
    expected = file_io.path_identity(source)
    real_rename = file_io._rename_noreplace
    raced = False

    def replace_then_rename(current: Path, target: Path) -> None:
        nonlocal raced
        if current == source and not raced:
            raced = True
            current.rename(original)
            current.write_bytes(b"replacement")
        real_rename(current, target)

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_then_rename)

    with pytest.raises(file_io.PathChangedError) as caught:
        file_io.atomic_move_no_replace(
            source,
            destination,
            expected_identity=expected,
        )

    assert caught.value.preserved_path is None
    assert source.read_bytes() == b"replacement"
    assert original.read_bytes() == b"expected"
    assert not destination.exists()


def test_atomic_move_preserves_a_mismatch_when_restore_would_replace_a_new_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    original = tmp_path / "original"
    source.write_bytes(b"expected")
    expected = file_io.path_identity(source)
    real_rename = file_io._rename_noreplace
    raced = False

    def replace_move_and_reoccupy(current: Path, target: Path) -> None:
        nonlocal raced
        if current == source and not raced:
            raced = True
            current.rename(original)
            current.write_bytes(b"first replacement")
            real_rename(current, target)
            current.write_bytes(b"newest replacement")
            return
        real_rename(current, target)

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_move_and_reoccupy)

    with pytest.raises(file_io.PathChangedError) as caught:
        file_io.atomic_move_no_replace(
            source,
            destination,
            expected_identity=expected,
        )

    assert caught.value.preserved_path == destination
    assert source.read_bytes() == b"newest replacement"
    assert destination.read_bytes() == b"first replacement"
    assert original.read_bytes() == b"expected"


def test_atomic_move_rejects_a_cross_type_replacement_with_a_reused_inode_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The entry type disambiguates an inode number reused after unlink."""
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(source))
    stale.close()
    expected = file_io.path_identity(source)
    real_rename = file_io._rename_noreplace
    real_lstat = Path.lstat
    raced = False

    def replace_then_rename(current: Path, target: Path) -> None:
        nonlocal raced
        if current == source and not raced:
            current.unlink()
            current.write_bytes(b"replacement")
            raced = True
        real_rename(current, target)

    def reuse_the_expected_inode(candidate: Path) -> os.stat_result:
        status = real_lstat(candidate)
        if candidate == destination and raced and stat.S_ISREG(status.st_mode):
            fields = list(status)
            fields[stat.ST_INO] = expected[1]
            fields[stat.ST_DEV] = expected[0]
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(file_io, "_rename_noreplace", replace_then_rename)
    monkeypatch.setattr(Path, "lstat", reuse_the_expected_inode)

    with pytest.raises(file_io.PathChangedError, match="changed while") as caught:
        file_io.atomic_move_no_replace(
            source,
            destination,
            expected_identity=expected,
            expected_file_type=stat.S_IFSOCK,
        )

    assert caught.value.preserved_path is None
    assert source.read_bytes() == b"replacement"
    assert not destination.exists()


def test_atomic_move_rejects_the_wrong_type_even_when_the_identity_matches(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"replacement")

    with pytest.raises(file_io.PathChangedError, match="expected entry type"):
        file_io.atomic_move_no_replace(
            source,
            destination,
            expected_identity=file_io.path_identity(source),
            expected_file_type=stat.S_IFSOCK,
        )

    assert source.read_bytes() == b"replacement"
    assert not destination.exists()


def test_atomic_move_compares_the_type_bits_not_regular_file_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"expected")
    expected = file_io.path_identity(source)
    real_rename = file_io._rename_noreplace

    def chmod_then_rename(current: Path, target: Path) -> None:
        current.chmod(0o600)
        real_rename(current, target)

    monkeypatch.setattr(file_io, "_rename_noreplace", chmod_then_rename)

    file_io.atomic_move_no_replace(
        source,
        destination,
        expected_identity=expected,
    )

    assert destination.read_bytes() == b"expected"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o600


def test_atomic_move_restores_an_open_file_rewritten_during_the_rename(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.write_bytes(b"expected")
    expected_identity = file_io.path_identity(source)
    expected_fingerprint = file_io.regular_file_fingerprint(source)
    real_rename = file_io._rename_noreplace

    def rewrite_then_rename(current: Path, target: Path) -> None:
        if current == source:
            current.write_bytes(b"rewritten through an already-open handle")
        real_rename(current, target)

    monkeypatch.setattr(file_io, "_rename_noreplace", rewrite_then_rename)

    with pytest.raises(file_io.PathChangedError, match="changed while") as caught:
        file_io.atomic_move_no_replace(
            source,
            destination,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
        )

    assert caught.value.preserved_path is None
    assert source.read_bytes() == b"rewritten through an already-open handle"
    assert not destination.exists()


def test_claim_refuses_to_discard_an_inode_mutated_after_the_atomic_move(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected")
    expected_identity = file_io.path_identity(source)
    expected_fingerprint = file_io.regular_file_fingerprint(source)
    writer = os.open(source, os.O_WRONLY)
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=expected_identity,
        expected_fingerprint=expected_fingerprint,
    )
    try:
        os.write(writer, b"changed through the original open handle")
        os.ftruncate(writer, len(b"changed through the original open handle"))
    finally:
        os.close(writer)

    try:
        with pytest.raises(file_io.PathChangedError, match="changed generation") as caught:
            claim.discard()

        assert caught.value.preserved_path == claim.path
        assert claim.path.read_bytes() == b"changed through the original open handle"
        assert claim.restore()
        assert source.read_bytes() == b"changed through the original open handle"
    finally:
        claim.close()


def test_claim_syncs_both_rename_directories_before_discard_can_truncate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"durable generation")
    fingerprint = file_io.regular_file_fingerprint(source)
    token = "0123456789abcdef0123456789abcdef"
    claim_directory = file_io.deletion_claim_directory(source, token)
    events: list[tuple[str, file_io.PathIdentity | None]] = []
    real_rename = file_io._rename_noreplace
    real_sync = file_io._fsync_directory_capability
    real_truncate = os.ftruncate

    def observe_rename(current: Path, target: Path) -> None:
        events.append(("rename", None))
        real_rename(current, target)

    def observe_sync(
        descriptor: int,
        *,
        expected_identity: file_io.PathIdentity,
        logical_path: Path,
    ) -> None:
        events.append(("sync", expected_identity))
        real_sync(
            descriptor,
            expected_identity=expected_identity,
            logical_path=logical_path,
        )

    def observe_truncate(descriptor: int, length: int) -> None:
        events.append(("truncate", None))
        real_truncate(descriptor, length)

    monkeypatch.setattr(file_io, "_rename_noreplace", observe_rename)
    monkeypatch.setattr(file_io, "_fsync_directory_capability", observe_sync)
    monkeypatch.setattr(os, "ftruncate", observe_truncate)

    claim = file_io.claim_for_deletion(
        source,
        expected_identity=fingerprint[:2],
        expected_fingerprint=fingerprint,
        operation_token=token,
    )
    claim_identity = file_io.path_identity(claim_directory)
    source_parent_identity = file_io.path_identity(tmp_path)
    expected_prefix = [
        ("rename", None),
        ("sync", claim_identity),
        ("sync", source_parent_identity),
    ]
    assert events == expected_prefix

    try:
        claim.discard()
    finally:
        claim.close()

    truncate_index = events.index(("truncate", None))
    assert events[:truncate_index] == expected_prefix


@pytest.mark.parametrize("failed_barrier", ("claim", "source"))
def test_failed_claim_directory_barrier_preserves_bytes_before_discard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_barrier: str,
) -> None:
    source = tmp_path / "source"
    contents = b"must not be truncated"
    source.write_bytes(contents)
    fingerprint = file_io.regular_file_fingerprint(source)
    real_sync = file_io._fsync_directory_capability
    real_rename = file_io._rename_noreplace
    renamed = False
    truncated = False

    def observe_rename(current: Path, target: Path) -> None:
        nonlocal renamed
        real_rename(current, target)
        if current.name == source.name and target.name == "entry":
            renamed = True

    def fail_selected_barrier(
        descriptor: int,
        *,
        expected_identity: file_io.PathIdentity,
        logical_path: Path,
    ) -> None:
        is_claim = logical_path.name.startswith(file_io.RETAINED_ENTRY_PREFIX)
        is_source = logical_path == tmp_path
        if renamed and (
            (failed_barrier == "claim" and is_claim) or (failed_barrier == "source" and is_source)
        ):
            raise OSError(errno.EIO, f"injected {failed_barrier} directory fsync failure")
        real_sync(
            descriptor,
            expected_identity=expected_identity,
            logical_path=logical_path,
        )

    def forbid_truncate(_descriptor: int, _length: int) -> None:
        nonlocal truncated
        truncated = True
        raise AssertionError("claim bytes were truncated before directory durability")

    monkeypatch.setattr(file_io, "_rename_noreplace", observe_rename)
    monkeypatch.setattr(file_io, "_fsync_directory_capability", fail_selected_barrier)
    monkeypatch.setattr(os, "ftruncate", forbid_truncate)

    with pytest.raises(
        file_io.PathChangedError,
        match=rf"injected {failed_barrier} directory fsync failure",
    ) as caught:
        file_io.discard_regular_if_same(
            source,
            expected_identity=fingerprint[:2],
            expected_fingerprint=fingerprint,
        )

    assert renamed
    assert not truncated
    assert caught.value.preserved_path is not None
    assert caught.value.preserved_path.read_bytes() == contents
    assert not source.exists()


def test_restore_syncs_public_parent_before_withdrawing_private_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"restored generation")
    fingerprint = file_io.regular_file_fingerprint(source)
    token = "fedcba9876543210fedcba9876543210"
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=fingerprint[:2],
        expected_fingerprint=fingerprint,
        operation_token=token,
    )
    claim_directory = file_io.deletion_claim_directory(source, token)
    claim_identity = file_io.path_identity(claim_directory)
    public_parent_identity = file_io.path_identity(tmp_path)
    events: list[tuple[str, file_io.PathIdentity | None]] = []
    real_rename = file_io._rename_noreplace
    real_sync = file_io._fsync_directory_capability

    def observe_rename(current: Path, target: Path) -> None:
        events.append(("rename", None))
        real_rename(current, target)

    def observe_sync(
        descriptor: int,
        *,
        expected_identity: file_io.PathIdentity,
        logical_path: Path,
    ) -> None:
        events.append(("sync", expected_identity))
        real_sync(
            descriptor,
            expected_identity=expected_identity,
            logical_path=logical_path,
        )

    monkeypatch.setattr(file_io, "_rename_noreplace", observe_rename)
    monkeypatch.setattr(file_io, "_fsync_directory_capability", observe_sync)

    try:
        assert claim.restore()
    finally:
        claim.close()

    assert events == [
        ("rename", None),
        ("sync", public_parent_identity),
        ("sync", claim_identity),
    ]
    assert source.read_bytes() == b"restored generation"


@pytest.mark.parametrize("failed_barrier", ("destination", "claim"))
def test_failed_restore_barrier_preserves_the_full_public_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_barrier: str,
) -> None:
    source = tmp_path / "source"
    contents = b"restoration must stay discoverable"
    source.write_bytes(contents)
    fingerprint = file_io.regular_file_fingerprint(source)
    token = "00112233445566778899aabbccddeeff"
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=fingerprint[:2],
        expected_fingerprint=fingerprint,
        operation_token=token,
    )
    claim_directory = file_io.deletion_claim_directory(source, token)
    real_sync = file_io._fsync_directory_capability

    def fail_selected_barrier(
        descriptor: int,
        *,
        expected_identity: file_io.PathIdentity,
        logical_path: Path,
    ) -> None:
        if (failed_barrier == "destination" and logical_path == tmp_path) or (
            failed_barrier == "claim" and logical_path == claim_directory
        ):
            raise OSError(errno.EIO, f"injected restore {failed_barrier} fsync failure")
        real_sync(
            descriptor,
            expected_identity=expected_identity,
            logical_path=logical_path,
        )

    monkeypatch.setattr(file_io, "_fsync_directory_capability", fail_selected_barrier)

    try:
        with pytest.raises(
            file_io.PathChangedError,
            match=rf"injected restore {failed_barrier} fsync failure",
        ) as caught:
            claim.restore()

        assert caught.value.preserved_path == source
        assert source.read_bytes() == contents
        assert tuple(claim_directory.iterdir()) == ()
    finally:
        claim.close()


def test_claim_discard_never_truncates_a_post_verify_regular_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected generation")
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=file_io.path_identity(source),
        expected_fingerprint=file_io.regular_file_fingerprint(source),
    )
    preserved = tmp_path / "preserved-expected-generation"
    replacement = b"unrelated regular replacement"
    real_open = os.open
    raced = False

    def replace_before_writable_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal raced
        candidate = Path(os.fsdecode(path))
        if candidate.name == "entry" and flags & os.O_WRONLY and not raced:
            raced = True
            candidate.rename(preserved)
            candidate.write_bytes(replacement)
        return real_open(path, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(os, "open", replace_before_writable_open)

    try:
        with pytest.raises(file_io.PathChangedError, match="changed before discard"):
            claim.discard()

        assert raced
        assert claim.path.read_bytes() == replacement
        assert preserved.read_bytes() == b"expected generation"
    finally:
        claim.close()


def test_claim_restore_never_moves_a_post_verify_regular_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected generation")
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=file_io.path_identity(source),
        expected_fingerprint=file_io.regular_file_fingerprint(source),
    )
    preserved = tmp_path / "preserved-expected-generation"
    replacement = b"unrelated regular replacement"
    real_move = file_io.atomic_move_no_replace
    raced = False

    def replace_before_restore(
        current: Path,
        target: Path,
        **keywords: object,
    ) -> None:
        nonlocal raced
        if current.name == "entry" and not raced:
            raced = True
            current.rename(preserved)
            current.write_bytes(replacement)
        real_move(current, target, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "atomic_move_no_replace", replace_before_restore)

    try:
        with pytest.raises(file_io.PathChangedError):
            claim.restore()

        assert raced
        assert not source.exists()
        assert claim.path.read_bytes() == replacement
        assert preserved.read_bytes() == b"expected generation"
    finally:
        claim.close()


def test_recovery_replays_a_crash_after_the_discard_marker_but_before_truncate(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"journaled generation")
    expected = file_io.regular_file_fingerprint(source)
    token = "0123456789abcdef0123456789abcdef"

    assert _crash_during_discard(source, token, "before-truncate") == 71

    directory = file_io.deletion_claim_directory(source, token)
    claimed = directory / "entry"
    marked = claimed.stat()
    assert marked.st_size == expected[2]
    assert marked.st_mtime_ns == expected[3]
    assert marked.st_atime_ns == expected[4]

    recovered = file_io.recover_deletion_claim(
        source,
        expected_identity=expected[:2],
        expected_fingerprint=expected,
        operation_token=token,
    )
    assert recovered is not None
    try:
        recovered.discard()
    finally:
        recovered.close()

    assert directory.is_dir()
    assert tuple(directory.iterdir()) == ()
    residues = _retained_entries(tmp_path)
    (residue,) = tuple(path for path in residues if path.is_file())
    assert residue.stat().st_size == 0
    assert not any(path.is_dir() for path in residues)


def test_recovery_finalizes_a_crash_after_exact_truncate_and_sync(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"journaled generation")
    expected = file_io.regular_file_fingerprint(source)
    token = "fedcba9876543210fedcba9876543210"

    assert _crash_during_discard(source, token, "after-truncate") == 72

    directory = file_io.deletion_claim_directory(source, token)
    claimed = directory / "entry"
    tombstone = claimed.stat()
    assert tombstone.st_nlink == 1
    assert tombstone.st_size == 0
    assert tombstone.st_atime_ns == expected[4]

    assert (
        file_io.recover_deletion_claim(
            source,
            expected_identity=expected[:2],
            expected_fingerprint=expected,
            operation_token=token,
        )
        is None
    )

    assert directory.is_dir()
    assert tuple(directory.iterdir()) == ()
    residues = _retained_entries(tmp_path)
    (residue,) = tuple(path for path in residues if path.is_file())
    assert residue.stat().st_nlink == 1
    assert residue.stat().st_size == 0
    assert not any(path.is_dir() for path in residues)


def test_read_only_claim_inspection_never_centralises_a_consumed_tombstone(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"journaled generation")
    expected = file_io.regular_file_fingerprint(source)
    token = "1234567890abcdef1234567890abcdef"

    assert _crash_during_discard(source, token, "after-truncate") == 72
    directory = file_io.deletion_claim_directory(source, token)
    claimed = directory / "entry"
    before = claimed.stat()

    assert (
        file_io.recover_deletion_claim(
            source,
            expected_identity=expected[:2],
            expected_fingerprint=expected,
            operation_token=token,
            read_only=True,
        )
        is None
    )

    after = claimed.stat()
    assert (after.st_dev, after.st_ino, after.st_size, after.st_atime_ns) == (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_atime_ns,
    )
    assert tuple(directory.iterdir()) == (claimed,)
    assert not (tmp_path / file_io.RETAINED_ENTRY_DIRECTORY).exists()


def test_atomic_move_rejects_directory_relocation_even_with_a_pin(tmp_path: Path) -> None:
    directory = tmp_path / "private"
    destination = tmp_path / "destination"
    directory.mkdir(mode=0o700)
    pin = file_io.pin_directory_path(directory, require_private=True)
    try:
        with pytest.raises(ValueError, match="directory relocation is unsupported"):
            file_io.atomic_move_no_replace(
                directory,
                destination,
                expected_identity=pin.identity,
                expected_file_type=stat.S_IFDIR,
                pinned_source=pin,
            )
    finally:
        pin.close()

    assert directory.is_dir()
    assert not destination.exists()


@pytest.mark.parametrize(
    "operation_token",
    (None, "0123456789abcdef0123456789abcdef"),
)
def test_claim_never_retires_a_directory_replaced_before_its_first_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_token: str | None,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected")
    fingerprint = file_io.regular_file_fingerprint(source)
    real_pin = file_io._pin_private_claim_directory
    replacement: Path | None = None
    created: Path | None = None

    def replace_before_pin(candidate: Path) -> file_io._PinnedClaimDirectory:
        nonlocal replacement, created
        is_claim = (
            candidate == file_io.deletion_claim_directory(source, operation_token)
            if operation_token is not None
            else candidate.name.startswith(file_io.RETAINED_ENTRY_PREFIX)
        )
        if is_claim and replacement is None:
            public_parent = (
                tmp_path
                if operation_token is not None
                else tmp_path / file_io.RETAINED_ENTRY_DIRECTORY
            )
            replacement = public_parent / candidate.name
            created = replacement.with_name(f"{replacement.name}-created")
            candidate.rename(candidate.with_name(created.name))
            candidate.mkdir(mode=0o700)
            (candidate / "entry").write_bytes(b"unrelated")
        return real_pin(candidate)

    monkeypatch.setattr(file_io, "_pin_private_claim_directory", replace_before_pin)

    with pytest.raises(FileExistsError):
        file_io.claim_for_deletion(
            source,
            expected_identity=fingerprint[:2],
            expected_fingerprint=fingerprint,
            operation_token=operation_token,
        )

    assert replacement is not None
    assert created is not None
    assert source.read_bytes() == b"expected"
    assert (replacement / "entry").read_bytes() == b"unrelated"
    assert created.is_dir()


@pytest.mark.parametrize(
    "operation_token",
    (None, "fedcba9876543210fedcba9876543210"),
)
def test_finished_claim_never_retires_a_directory_adopted_at_first_pin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation_token: str | None,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected")
    fingerprint = file_io.regular_file_fingerprint(source)
    real_pin = file_io._pin_private_claim_directory
    replacement: Path | None = None
    created: Path | None = None

    def replace_before_pin(candidate: Path) -> file_io._PinnedClaimDirectory:
        nonlocal replacement, created
        is_claim = (
            candidate == file_io.deletion_claim_directory(source, operation_token)
            if operation_token is not None
            else candidate.name.startswith(file_io.RETAINED_ENTRY_PREFIX)
        )
        if is_claim and replacement is None:
            public_parent = (
                tmp_path
                if operation_token is not None
                else tmp_path / file_io.RETAINED_ENTRY_DIRECTORY
            )
            replacement = public_parent / candidate.name
            created = replacement.with_name(f"{replacement.name}-created")
            candidate.rename(candidate.with_name(created.name))
            candidate.mkdir(mode=0o700)
            (candidate / "keep").write_bytes(b"replacement")
        return real_pin(candidate)

    monkeypatch.setattr(file_io, "_pin_private_claim_directory", replace_before_pin)

    claim = file_io.claim_for_deletion(
        source,
        expected_identity=fingerprint[:2],
        expected_fingerprint=fingerprint,
        operation_token=operation_token,
    )
    try:
        claim.discard()
    finally:
        claim.close()

    assert replacement is not None
    assert created is not None
    assert (replacement / "keep").read_bytes() == b"replacement"
    assert tuple(path.name for path in replacement.iterdir()) == ("keep",)
    assert created.is_dir()


def test_recovery_never_retires_an_empty_token_directory_replacement(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected")
    fingerprint = file_io.regular_file_fingerprint(source)
    token = "0123456789abcdef0123456789abcdef"
    directory = file_io.deletion_claim_directory(source, token)
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=fingerprint[:2],
        expected_fingerprint=fingerprint,
        operation_token=token,
    )
    claim.close()
    displaced = tmp_path / "displaced-claim"
    directory.rename(displaced)
    directory.mkdir(mode=0o700)
    replacement_identity = file_io.path_identity(directory)

    assert (
        file_io.recover_deletion_claim(
            source,
            expected_identity=fingerprint[:2],
            expected_fingerprint=fingerprint,
            operation_token=token,
        )
        is None
    )

    assert file_io.path_identity(directory) == replacement_identity
    assert tuple(directory.iterdir()) == ()
    assert (displaced / "entry").read_bytes() == b"expected"


def test_discard_routes_file_and_claim_directory_residue_to_requested_parent(
    tmp_path: Path,
) -> None:
    nested = tmp_path / "transaction"
    nested.mkdir()
    source = nested / "record.json"
    source.write_bytes(b"transaction record")
    fingerprint = file_io.regular_file_fingerprint(source)

    assert file_io.discard_regular_if_same(
        source,
        expected_identity=fingerprint[:2],
        expected_fingerprint=fingerprint,
        retained_parent=tmp_path,
        logical_retained_parent=tmp_path,
    )

    assert tuple(nested.iterdir()) == ()
    retained = _retained_entries(tmp_path)
    assert len(retained) == 2
    assert any(path.is_file() and path.stat().st_size == 0 for path in retained)
    assert any(path.is_dir() for path in retained)


def test_pinned_directory_context_rejects_a_public_rebind(tmp_path: Path) -> None:
    root = tmp_path / "root"
    directory = root / "automatic"
    directory.mkdir(parents=True)
    root_status = root.stat()
    directory_status = directory.stat()
    context = file_io.pin_directory_beneath(
        root.absolute(),
        directory.absolute(),
        expected_root_identity=(root_status.st_dev, root_status.st_ino),
        expected_directory_identity=(directory_status.st_dev, directory_status.st_ino),
    )
    moved = root / "saved"
    directory.rename(moved)
    directory.mkdir()
    try:
        with pytest.raises(file_io.PathChangedError):
            context.verify_public()
    finally:
        context.close()

    assert directory.is_dir()
    assert moved.is_dir()


def test_discard_fails_before_truncate_when_the_generation_marker_is_rounded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected generation")
    expected = file_io.regular_file_fingerprint(source)
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=expected[:2],
        expected_fingerprint=expected,
    )
    real_utime = os.utime

    def round_atime(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        *,
        ns: tuple[int, int],
    ) -> None:
        real_utime(path, ns=(max(0, ns[0] - 2_000_000_000), ns[1]))

    monkeypatch.setattr(os, "utime", round_atime)

    try:
        with pytest.raises(file_io.PathChangedError, match="discard marker"):
            claim.discard()

        assert claim.path.read_bytes() == b"expected generation"
    finally:
        claim.close()


def test_live_claim_does_not_follow_a_replaced_private_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected")
    expected_identity = file_io.path_identity(source)
    expected_fingerprint = file_io.regular_file_fingerprint(source)
    token = "0123456789abcdef0123456789abcdef"
    directory = file_io.deletion_claim_directory(source, token)
    relocated = tmp_path / "relocated-claim"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "untouched").write_bytes(b"unrelated")
    real_pin = file_io._pin_private_claim_directory

    def pin_then_replace(candidate: Path) -> file_io._PinnedClaimDirectory:
        pinned = real_pin(candidate)
        if candidate == directory:
            candidate.rename(relocated)
            candidate.symlink_to(outside, target_is_directory=True)
        return pinned

    monkeypatch.setattr(file_io, "_pin_private_claim_directory", pin_then_replace)

    claim = file_io.claim_for_deletion(
        source,
        expected_identity=expected_identity,
        expected_fingerprint=expected_fingerprint,
        operation_token=token,
    )
    try:
        assert (relocated / "entry").read_bytes() == b"expected"
        assert not (outside / "entry").exists()
        claim.discard()
        assert not (relocated / "entry").exists()
        (residue,) = _retained_entries(tmp_path)
        assert residue.read_bytes() == b""
        assert (outside / "untouched").read_bytes() == b"unrelated"
    finally:
        claim.close()


def test_recovery_does_not_follow_a_replaced_private_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"expected")
    expected_identity = file_io.path_identity(source)
    expected_fingerprint = file_io.regular_file_fingerprint(source)
    token = "fedcba9876543210fedcba9876543210"
    directory = file_io.deletion_claim_directory(source, token)
    claim = file_io.claim_for_deletion(
        source,
        expected_identity=expected_identity,
        expected_fingerprint=expected_fingerprint,
        operation_token=token,
    )
    claim.close()
    relocated = tmp_path / "relocated-claim"
    outside = tmp_path / "outside"
    outside.mkdir()
    outside_entry = outside / "entry"
    outside_entry.hardlink_to(directory / "entry")
    real_pin = file_io._pin_private_claim_directory

    def pin_then_replace(candidate: Path) -> file_io._PinnedClaimDirectory:
        pinned = real_pin(candidate)
        if candidate == directory:
            candidate.rename(relocated)
            candidate.symlink_to(outside, target_is_directory=True)
        return pinned

    monkeypatch.setattr(file_io, "_pin_private_claim_directory", pin_then_replace)

    recovered = file_io.recover_deletion_claim(
        source,
        expected_identity=expected_identity,
        expected_fingerprint=expected_fingerprint,
        operation_token=token,
    )
    assert recovered is not None
    try:
        recovered.discard()
        assert not (relocated / "entry").exists()
        (residue,) = _retained_entries(tmp_path)
        assert residue.read_bytes() == b"expected"
        assert outside_entry.read_bytes() == b"expected"
    finally:
        recovered.close()


@pytest.mark.parametrize("replacement", (b"different", b""))
def test_recovery_preserves_a_replacement_with_a_reused_inode_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    replacement: bytes,
) -> None:
    """Crash replay also checks rename-invariant generation evidence."""
    source = tmp_path / "source"
    source.write_bytes(b"journaled generation")
    expected_identity = file_io.path_identity(source)
    expected_fingerprint = file_io.regular_file_fingerprint(source)
    source.unlink()

    token = "0123456789abcdef0123456789abcdef"
    directory = file_io.deletion_claim_directory(source, token)
    directory.mkdir(mode=0o700)
    claimed = directory / "entry"
    claimed.write_bytes(replacement)
    if not replacement:
        replacement_status = claimed.stat()
        os.utime(
            claimed,
            ns=(
                max(0, expected_fingerprint[4] - 2_000_000_000),
                replacement_status.st_mtime_ns,
            ),
        )

    real_lstat = Path.lstat
    real_pin = file_io.pin_regular_path
    real_fstat = os.fstat
    claimed_inspections = 0
    pinned_descriptors: set[int] = set()

    def spoof_initial_claim_identity(candidate: Path) -> os.stat_result:
        nonlocal claimed_inspections
        status = real_lstat(candidate)
        if candidate == claimed:
            claimed_inspections += 1
            if claimed_inspections == 1:
                fields = list(status)
                fields[stat.ST_DEV] = expected_identity[0]
                fields[stat.ST_INO] = expected_identity[1]
                return os.stat_result(fields)
        return status

    def pin_reused_generation(
        path: Path,
        *,
        expected_identity: file_io.PathIdentity | None = None,
        expected_fingerprint: file_io.FileFingerprint | None = None,
    ) -> file_io.PinnedPath:
        del expected_identity, expected_fingerprint
        pin = real_pin(path)
        pinned_descriptors.add(pin.descriptor)
        return pin

    def spoof_pinned_identity(descriptor: int) -> os.stat_result:
        status = real_fstat(descriptor)
        if descriptor in pinned_descriptors:
            fields = list(status)
            fields[stat.ST_DEV] = expected_identity[0]
            fields[stat.ST_INO] = expected_identity[1]
            return os.stat_result(fields)
        return status

    monkeypatch.setattr(Path, "lstat", spoof_initial_claim_identity)
    monkeypatch.setattr(file_io, "pin_regular_path", pin_reused_generation)
    monkeypatch.setattr(os, "fstat", spoof_pinned_identity)

    with pytest.raises(file_io.PathChangedError, match="file generation") as caught:
        file_io.recover_deletion_claim(
            source,
            expected_identity=expected_identity,
            expected_fingerprint=expected_fingerprint,
            operation_token=token,
        )

    assert caught.value.preserved_path == claimed
    assert claimed.read_bytes() == replacement


def test_recovery_preserves_a_v1_claim_without_generation_evidence(tmp_path: Path) -> None:
    source = tmp_path / "source"
    token = "fedcba9876543210fedcba9876543210"
    directory = file_io.deletion_claim_directory(source, token)
    directory.mkdir(mode=0o700)
    claimed = directory / "entry"
    claimed.write_bytes(b"old journal claim")

    with pytest.raises(file_io.PathChangedError, match="predates") as caught:
        file_io.recover_deletion_claim(
            source,
            expected_identity=file_io.path_identity(claimed),
            operation_token=token,
        )

    assert caught.value.preserved_path == claimed
    assert claimed.read_bytes() == b"old journal claim"
