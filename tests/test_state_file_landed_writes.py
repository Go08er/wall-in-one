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
    # And `backup_before_bump` says it raises this, not only "no backup kept".
    assert "BackupNotConfirmedError" in (state_file.backup_before_bump.__doc__ or "")


@contextlib.contextmanager
def put_back_refused(
    monkeypatch: pytest.MonkeyPatch, target: Path, refuse: Callable[[Path], object]
) -> Iterator[list[Path]]:
    """The publication's verification read fails once; the candidate is moved back
    to its temporary, then ``refuse(target)`` runs, so that putting it back fails.

    Yields the temporaries it was moved back to.
    """
    rename = file_io._rename_noreplace
    lstat = Path.lstat
    moved_back: list[Path] = []
    failing = False

    def publish(source: Path, destination: Path) -> None:
        nonlocal failing
        rename(source, destination)
        if source.name.endswith(".tmp") and destination == target and not moved_back:
            failing = True
        elif source == target and destination.name.endswith(".tmp"):
            moved_back.append(destination)
            refuse(target)

    def verify(path: Path) -> os.stat_result:
        nonlocal failing
        if failing and path == target:
            failing = False
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        return lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", publish)
    monkeypatch.setattr(Path, "lstat", verify)
    yield moved_back


def test_a_candidate_that_cannot_be_put_back_is_said_as_discarded(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual repair takes the name before the candidate can be put back: the
    repair wins, and the writer's cleanup discards the candidate at its temporary.
    Said as discarded; it used to say "remains preserved at <temporary>", which
    the cleanup had just removed."""
    target = folder / "state.json"
    repair = "manual repair\n"

    with (
        put_back_refused(monkeypatch, target, lambda name: name.write_text(repair)) as moved,
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert len(moved) == 1 and target.read_text(encoding="utf-8") == repair
    assert not moved[0].exists(), "the cleanup discarded the candidate"
    # No stray file: besides the repair, only what every discard leaves, its
    # emptied inode in the retained container (`file_io.RETAINED_ENTRY_DIRECTORY`).
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY
    assert sorted(entry.name for entry in folder.iterdir()) == sorted([target.name, retained.name])
    assert all(entry.stat().st_size == 0 for entry in retained.iterdir()), "emptied"
    assert f"a newer concurrent entry remains at {target}" in said, said
    # Failed on d2260e9: "...; the publication candidate remains preserved at <gone>".
    assert said.endswith("; the publication candidate was discarded"), said
    assert wrote == []


def test_a_candidate_the_cleanup_cannot_discard_is_said_where_it_is(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the folder turns read-only (a native EACCES) right after the
    candidate is moved back, so it can neither be put back nor discarded. It stays
    at its temporary, and that is what is said."""
    target = folder / "state.json"

    with (
        put_back_refused(monkeypatch, target, lambda name: name.parent.chmod(0o500)) as moved,
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)
    folder.chmod(0o700)

    said = str(raised.value)
    assert len(moved) == 1 and not target.exists()
    assert moved[0].read_text(encoding="utf-8") == CANDIDATE, "kept, where it is said to be"
    assert "the publication candidate could not be put back" in said, said
    assert said.endswith(f"; the publication candidate remains preserved at {moved[0]}"), said
    assert wrote == []


def test_an_entry_replacing_the_candidate_before_the_cleanup_is_left_and_not_claimed(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A repair takes the name, then something else replaces the temporary before
    the cleanup proves it ours again: not ours, so not discarded, and not said to be
    the publication candidate either."""
    target = folder / "state.json"
    holds = state_file._holds_publication_candidate
    proofs: list[Path] = []
    stranger = "somebody else's\n"

    def proof(
        entry: Path,
        candidate: file_io.PinnedPath,
        fingerprint: file_io.FileFingerprint,
        document: bytes,
    ) -> tuple[os.stat_result, bool]:
        proofs.append(entry)
        if len(proofs) == 2:  # the cleanup's proof, after putting it back failed
            replacement = folder / "replacement"
            replacement.write_text(stranger, encoding="utf-8")
            os.replace(replacement, entry)
        return holds(entry, candidate, fingerprint, document)

    monkeypatch.setattr(state_file, "_holds_publication_candidate", proof)
    with (
        put_back_refused(monkeypatch, target, lambda name: name.write_text("repair\n")) as moved,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert proofs == [moved[0], moved[0]]
    assert moved[0].read_text(encoding="utf-8") == stranger, "left as it is"
    assert said.endswith(
        f"; the entry now at {moved[0]} is not provably the publication candidate, "
        "so it was left as it is"
    ), said


@contextlib.contextmanager
def moved_back_uninspectable(
    monkeypatch: pytest.MonkeyPatch, target: Path, *, failures: int
) -> Iterator[list[Path]]:
    """The publication's verification read fails once, so the candidate is moved
    back to its temporary; then the next ``failures`` reads of that temporary fail
    too (a real EIO each), so it can't be inspected there.

    Yields the temporaries it was moved back to.
    """
    rename = file_io._rename_noreplace
    lstat = Path.lstat
    moved_back: list[Path] = []
    failing = False
    remaining = 0

    def publish(source: Path, destination: Path) -> None:
        nonlocal failing, remaining
        rename(source, destination)
        if source.name.endswith(".tmp") and destination == target and not moved_back:
            failing = True
        elif source == target and destination.name.endswith(".tmp"):
            moved_back.append(destination)
            remaining = failures

    def verify(path: Path) -> os.stat_result:
        nonlocal failing, remaining
        if failing and path == target:
            failing = False
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        if remaining and moved_back and path == moved_back[0]:
            remaining -= 1
            raise OSError(errno.EIO, "injected read failure of the temporary")
        return lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", publish)
    monkeypatch.setattr(Path, "lstat", verify)
    yield moved_back


def test_a_candidate_that_could_not_be_inspected_is_proven_then_discarded(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moved back to its temporary, the candidate can't be inspected there once.
    The cleanup proves it ours then, discards it, and says so. It used to say only
    "could not inspect a relocated concurrent state entry" (ours, not concurrent),
    and to leave it in the folder: the cleanup's fingerprint predated the renames."""
    target = folder / "state.json"

    with (
        moved_back_uninspectable(monkeypatch, target, failures=1) as moved,
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert len(moved) == 1 and not target.exists()
    assert not moved[0].exists(), "discarded, not left in the folder"
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY
    assert sorted(entry.name for entry in folder.iterdir()) == [retained.name]
    assert all(entry.stat().st_size == 0 for entry in retained.iterdir()), "emptied"
    assert f"could not inspect the entry moved back to {moved[0]}" in said, said
    assert said.endswith("; the publication candidate was discarded"), said
    assert wrote == []


def test_a_candidate_that_can_never_be_inspected_is_said_as_it_ends(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the temporary can't be read at all, so the candidate can't be
    proven ours. The cleanup may then discard only the exact generation it wrote; on
    a filesystem where the renames changed its ctime that isn't it, so it stays.
    Whichever happens, the message says so, and names the temporary."""
    target = folder / "state.json"

    with (
        moved_back_uninspectable(monkeypatch, target, failures=1_000) as moved,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert len(moved) == 1 and not target.exists()
    assert f"could not inspect the entry moved back to {moved[0]}" in said, said
    if os.path.lexists(moved[0]):
        assert moved[0].read_text(encoding="utf-8") == CANDIDATE, "left as it is"
        assert said.endswith(
            f"; the entry now at {moved[0]} is not provably the publication candidate, "
            "so it was left as it is"
        ), said
    else:
        assert said.endswith("; the publication candidate was discarded"), said
