"""A no-replace publication that landed is recorded, even when it ends in an error (Z-3).

`state_file.write_atomic_bytes(..., replace_existing=False)` renames its
temporary into place without replacing anything, then verifies what is
there. When that verification can't be done (a one-shot read failure), the
entry is moved back and, if it is provably this writer's candidate, put back
at its public name, synced, and the call raises `PathChangedError`. The
complete file is public, but `record_write` sat below that branch, so the
authoring lane's collector (`collecting_writes`) never heard of it.

Recorded now: only a candidate *proven* to be this writer's bytes, restored to
its name or left there. A concurrent entry put back, or one left at the name
that isn't provably ours, is never recorded. Real faults: a one-shot failure
of the verification read, and a native EACCES on the private folder.
"""

from __future__ import annotations

import contextlib
import errno
import os
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path

import pytest

from tests.test_palettes import _document
from tests.test_published_not_durable import _sync_fails_for
from wall_in_one import file_io
from wall_in_one.library import playlists, state_file
from wall_in_one.theme import palettes

CANDIDATE = "recovered mutation\n"


@contextlib.contextmanager
def after_the_publication(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    then: Callable[[Path], None] = lambda _target: None,
) -> Iterator[list[bool]]:
    """Fail the one verification read after the candidate is renamed to ``target``.

    ``then`` runs right after that rename, before the verification. Yields a
    flag list that holds True once the publication rename happened.
    """
    rename = file_io._rename_noreplace
    lstat = Path.lstat
    published: list[bool] = []
    failing = False

    def publish(source: Path, destination: Path) -> None:
        nonlocal failing
        rename(source, destination)
        if not published and source.name.endswith(".tmp") and destination == target:
            published.append(True)
            then(target)
            failing = True

    def verify(path: Path) -> os.stat_result:
        nonlocal failing
        if failing and path == target:
            failing = False
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", publish)
    monkeypatch.setattr(Path, "lstat", verify)
    yield published


@pytest.fixture
def folder(tmp_path: Path) -> Iterator[Path]:
    private = tmp_path / "state"
    private.mkdir(mode=0o700)
    try:
        yield private
    finally:
        private.chmod(0o700)


def test_a_candidate_put_back_at_its_name_is_recorded(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = folder / "state.json"
    with (
        after_the_publication(monkeypatch, target) as published,
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError, match="publication candidate was restored"),
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    assert published
    assert target.read_text(encoding="utf-8") == CANDIDATE, "the complete file is public"
    # Before: [] although the write had landed.
    assert wrote == [target]
    assert list(folder.glob(".*.tmp")) == []


def test_a_candidate_left_at_its_name_is_recorded(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The move back is refused by the folder itself (EACCES): the candidate stays, proven ours."""
    target = folder / "state.json"
    with (
        after_the_publication(monkeypatch, target, lambda t: t.parent.chmod(0o500)) as published,
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError, match="publication candidate remains at"),
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    folder.chmod(0o700)
    assert published
    assert target.read_text(encoding="utf-8") == CANDIDATE
    assert wrote == [target]


def test_a_concurrent_entry_put_back_is_not_recorded(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Someone edits the file in the gap: what goes back is theirs, not this writer's."""
    target = folder / "state.json"

    def repair(path: Path) -> None:
        path.write_text("manual in-place repair\n", encoding="utf-8")

    with (
        after_the_publication(monkeypatch, target, repair),
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError, match="concurrent entry was restored"),
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    assert target.read_text(encoding="utf-8") == "manual in-place repair\n"
    assert wrote == []


def test_an_unproven_entry_left_at_the_name_is_not_recorded(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Edited in the gap and then stuck there (EACCES): not provably ours, so not recorded."""
    target = folder / "state.json"

    def repair_then_lock(path: Path) -> None:
        path.write_text("manual in-place repair\n", encoding="utf-8")
        path.parent.chmod(0o500)

    with (
        after_the_publication(monkeypatch, target, repair_then_lock),
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError) as refused,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    folder.chmod(0o700)
    assert "publication candidate" not in str(refused.value)
    assert refused.value.preserved_path == target
    assert target.read_text(encoding="utf-8") == "manual in-place repair\n"
    assert wrote == []


def test_a_new_palette_restored_by_the_rename_fallback_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`palettes.write_custom` without ``O_TMPFILE`` publishes through the same writer."""
    custom = tmp_path / "colorschemes"
    target = palettes.custom_path("Mine", custom)
    opened = os.open

    def no_tmpfile(path: object, flags: int, *arguments: object, **keywords: object) -> int:
        if flags & os.O_TMPFILE == os.O_TMPFILE:
            raise OSError(errno.EOPNOTSUPP, "no O_TMPFILE on this filesystem")
        return opened(path, flags, *arguments, **keywords)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", no_tmpfile)
    with (
        after_the_publication(monkeypatch, target) as published,
        state_file.collecting_writes() as wrote,
        pytest.raises(palettes.PaletteWriteError, match="publication candidate was restored"),
    ):
        palettes.write_custom("Mine", _document(), custom)

    assert published
    assert palettes.read_document(target) is not None, "the complete palette is public"
    assert wrote == [target]


def test_a_candidate_landing_on_the_way_back_is_recorded(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cold review's double fault: putting the proven candidate back lands, but
    that move's own verification read fails too and its move back is refused by the
    folder (EACCES). The candidate is at its name: recorded, and said to be there."""
    target = folder / "state.json"
    rename = file_io._rename_noreplace
    lstat = Path.lstat
    publications = 0
    failing = False

    def publish(source: Path, destination: Path) -> None:
        nonlocal publications, failing
        rename(source, destination)
        if source.name.endswith(".tmp") and destination == target:
            publications += 1
            failing = True  # each landing's verification read fails once
            if publications == 2:
                target.parent.chmod(0o500)  # and the second one can't be moved back

    def verify(path: Path) -> os.stat_result:
        nonlocal failing
        if failing and path == target:
            failing = False
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", publish)
    monkeypatch.setattr(Path, "lstat", verify)
    with (
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)
    folder.chmod(0o700)

    assert publications == 2
    assert target.read_text(encoding="utf-8") == CANDIDATE, "the complete file is public"
    said = str(raised.value)
    # Before: ([], True), "remains preserved at <temporary>".
    assert (wrote, "remains preserved at" in said) == ([target], False), said
    assert f"the publication candidate remains at {target}" in said


@contextlib.contextmanager
def _backup_fault(
    fault: str, monkeypatch: pytest.MonkeyPatch, backup: Path
) -> Iterator[Sequence[object]]:
    """The copy lands, then its write fails: its verification read, or its folder sync.

    Yields a list that is non-empty once the fault was injected.
    """
    if fault == "verification":
        with after_the_publication(monkeypatch, backup) as published:
            yield published
            return
    yield _sync_fails_for(monkeypatch, backup.name)


@pytest.mark.parametrize("fault", ["verification", "folder-sync"])
def test_a_backup_written_but_not_finished_cleanly_is_said_as_kept(
    fault: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A store's first bump keeps a copy first (`backup_before_bump`). When the copy
    lands but its write then fails, the bump is still refused, and the refusal says
    the copy was written and stays, not "could not keep a copy ... Nothing was changed"."""
    picture = tmp_path / "library" / "evening.png"
    picture.parent.mkdir()
    picture.write_bytes(b"fixture")
    store = playlists.Store.open()
    made = store.create("Evening")
    store.add(made.id, picture)
    target = playlists.state_path()
    before = target.read_bytes()
    backup = state_file.version_backup_path(target, 1)

    with (
        _backup_fault(fault, monkeypatch, backup) as happened,
        state_file.collecting_writes() as wrote,
        pytest.raises(playlists.PlaylistError) as refused,
    ):
        store.set_rotation(made.id, cycle_interval=60)  # version 2: the first bump

    message = str(refused.value)
    assert refused.value.kind == "no-backup"
    assert f"was written as {backup.name}" in message, message
    assert "but writing it did not finish cleanly" in message, message
    assert "playlists.json was not changed; the copy stays" in message, message
    assert "could not keep a copy" not in message and "Nothing was changed" not in message
    assert target.read_bytes() == before, "the store file is unchanged"
    assert backup.read_bytes() == before, "the copy is the version-1 file"
    assert wrote == [backup], "the copy landed, and the lane hears it"
    assert happened, "the fault was injected"
