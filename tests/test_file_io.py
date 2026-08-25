"""Safe small-file reads never follow or wait on user-writable path types."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from wall_in_one import file_io


def test_bounded_read_and_prefix_use_regular_files(tmp_path: Path) -> None:
    path = tmp_path / "document"
    path.write_bytes(b"0123456789")

    assert file_io.read_regular_bytes(path, 10) == b"0123456789"
    assert file_io.read_regular_prefix(path, 4) == b"0123"
    with pytest.raises(file_io.FileReadError, match="9-byte limit"):
        file_io.read_regular_bytes(path, 9)


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
