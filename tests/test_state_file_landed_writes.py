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
import time
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
        pytest.raises(file_io.PathChangedError, match="concurrent entry was restored") as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    assert target.read_text(encoding="utf-8") == "manual in-place repair\n"
    assert wrote == []
    assert raised.value.preserved_path is None, "put back at its name: nothing is preserved"


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


def _retired_entries(folder: Path) -> list[Path]:
    """What the retained folder beside the store holds."""
    retained = folder / file_io.RETAINED_ENTRY_DIRECTORY
    return sorted(retained.iterdir()) if retained.is_dir() else []


def test_a_candidate_that_cannot_be_put_back_is_said_as_retired(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual repair takes the name before the candidate can be put back: the
    repair wins, and the writer's cleanup retires the proven candidate intact into
    the retained folder, and says where. It once said "remains preserved at
    <temporary>", which the cleanup had just emptied; then "discarded"."""
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
    assert not moved[0].exists(), "no stray temporary in the store's folder"
    retired = _retired_entries(folder)
    assert len(retired) == 1 and retired[0].read_text(encoding="utf-8") == CANDIDATE, "intact"
    assert f"a newer concurrent entry remains at {target}" in said, said
    assert said.endswith(f"; the publication candidate was retired intact to {retired[0]}"), said
    assert raised.value.preserved_path == retired[0]
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
    # Failed on cc99c61: None, though the candidate remains there.
    assert raised.value.preserved_path == moved[0]
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
    assert raised.value.preserved_path == moved[0]


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


def test_a_candidate_that_could_not_be_inspected_is_proven_then_retired(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Moved back to its temporary, the candidate can't be inspected there once.
    The cleanup proves it ours then, retires it intact, and says where. It used to
    say only "could not inspect a relocated concurrent state entry" (ours, not
    concurrent), and to leave it in the folder: the cleanup's fingerprint predated
    the renames."""
    target = folder / "state.json"

    with (
        moved_back_uninspectable(monkeypatch, target, failures=1) as moved,
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert len(moved) == 1 and not target.exists()
    assert not moved[0].exists(), "retired, not left in the store's folder"
    retired = _retired_entries(folder)
    assert len(retired) == 1 and retired[0].read_text(encoding="utf-8") == CANDIDATE, "intact"
    assert f"could not inspect the entry moved back to {moved[0]}" in said, said
    assert said.endswith(f"; the publication candidate was retired intact to {retired[0]}"), said
    assert raised.value.preserved_path == retired[0]
    assert wrote == []


def test_a_candidate_that_can_never_be_inspected_is_said_as_it_ends(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: the temporary can't be looked at at all, so the candidate can't
    be proven ours, and an unavailable proof forbids discarding it (AA-1): it stays,
    and since it can't be looked at, whether it is still there is said to be
    unchecked (never that it is gone)."""
    target = folder / "state.json"

    with (
        moved_back_uninspectable(monkeypatch, target, failures=1_000) as moved,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert len(moved) == 1 and not target.exists()
    assert f"could not inspect the entry moved back to {moved[0]}" in said, said
    assert moved[0].read_text(encoding="utf-8") == CANDIDATE, "left as it is"
    assert f"; whether the entry moved back is still at {moved[0]} could not be checked (" in (
        said
    ), said
    assert raised.value.preserved_path == moved[0]


@pytest.mark.parametrize("mode", [0o600, 0o400, 0o000], ids=["0600", "0400", "0000"])
def test_a_temporary_that_cannot_be_looked_at_is_not_said_to_be_empty(
    folder: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    """AA-2. The candidate is moved back to its temporary by the real publication
    rollback, then the private folder loses its search permission (a native EACCES,
    at 0600, 0400 and 0000): it can't be inspected, put back, discarded or looked
    at. Whether it is still there is said to be unchecked, never that it is gone;
    with the permissions back, it is there, byte for byte."""
    target = folder / "state.json"

    with (
        put_back_refused(monkeypatch, target, lambda name: name.parent.chmod(mode)) as moved,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)
    folder.chmod(0o700)

    said = str(raised.value)
    assert len(moved) == 1 and moved[0].read_text(encoding="utf-8") == CANDIDATE
    # Failed on 778fbbc: "the entry moved back is no longer at <temporary>".
    assert f"is no longer at {moved[0]}" not in said, said
    assert f"could not inspect the entry moved back to {moved[0]}" in said, said
    unchecked = f"; whether the entry moved back is still at {moved[0]} could not be checked ("
    assert unchecked in said, said
    assert raised.value.preserved_path == moved[0]


def test_a_concurrent_entry_gone_from_the_temporary_is_not_said_to_remain(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A concurrent edit is moved back to the temporary and inspected; something
    outside then deletes it before it can be put back. It isn't said to remain."""
    target = folder / "state.json"
    holds = state_file._holds_publication_candidate
    inspected: list[Path] = []

    def then_deleted(
        entry: Path,
        candidate: file_io.PinnedPath,
        fingerprint: file_io.FileFingerprint,
        document: bytes,
    ) -> tuple[os.stat_result, bool]:
        result = holds(entry, candidate, fingerprint, document)
        if not inspected:
            inspected.append(entry)
            entry.unlink()
        return result

    def repair(path: Path) -> None:
        path.write_text("manual in-place repair\n", encoding="utf-8")

    monkeypatch.setattr(state_file, "_holds_publication_candidate", then_deleted)
    with (
        after_the_publication(monkeypatch, target, repair),
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert len(inspected) == 1 and not inspected[0].exists() and not target.exists()
    # Failed on 778fbbc: "the concurrent entry remains preserved at <deleted temporary>".
    assert f"remains preserved at {inspected[0]}" not in said, said
    assert f"the concurrent entry is no longer at {inspected[0]}" in said, said
    assert raised.value.preserved_path is None


# AA-1: a failed or unavailable proof of the bytes forbids discarding them.
MANUAL = "recovered MUTATION\n"  # a concurrent in-place edit: same inode, same length
REPAIR = "a newer public repair\n"
assert len(MANUAL) == len(CANDIDATE)


def _edit_in_place(path: Path, text: str) -> None:
    """Rewrite ``path``'s bytes in place: same inode, same length."""
    with path.open("r+b") as handle:
        handle.write(text.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def _with_timestamps(monkeypatch: pytest.MonkeyPatch, precision: str) -> None:
    """Fingerprints see mtime and ctime at one-second precision ("coarse"), as on a
    filesystem with coarse timestamps, or natively ("fine"). Identities, writes,
    renames and discards stay native. Starts just after a second begins, so a case
    falls within one second."""
    if precision == "coarse":
        real = file_io.file_fingerprint

        def coarse(status: os.stat_result) -> file_io.FileFingerprint:
            device, inode, size, mtime, ctime = real(status)
            second = 1_000_000_000
            return device, inode, size, mtime - mtime % second, ctime - ctime % second

        monkeypatch.setattr(file_io, "file_fingerprint", coarse)
    time.sleep(1.02 - time.time() % 1)


@contextlib.contextmanager
def relocated_with_a_repair(
    monkeypatch: pytest.MonkeyPatch,
    target: Path,
    *,
    edit: str,
    inspectable: bool = True,
) -> Iterator[list[Path]]:
    """The candidate is published, its verification read fails once (a real EIO)
    and the real rollback moves it back to its temporary; a newer public repair
    then takes the name, so it can't be put back. A concurrent in-place edit of the
    candidate's inode happens ``edit``: "published" (before the rollback) or
    "left" (after it was proven ours and couldn't be put back). With
    ``inspectable`` False the temporary can't be looked at after the rollback.

    Yields the temporaries it was moved back to.
    """
    rename = file_io._rename_noreplace
    lstat = Path.lstat
    moved_back: list[Path] = []
    published = False
    failing = False

    def publish(source: Path, destination: Path) -> None:
        nonlocal published, failing
        if moved_back and source == moved_back[0] and destination == target:
            try:
                rename(source, destination)  # putting it back: the repair holds the name
            finally:
                if edit == "left":
                    _edit_in_place(source, MANUAL)
            return
        rename(source, destination)
        if not published and source.name.endswith(".tmp") and destination == target:
            published = True
            if edit == "published":
                _edit_in_place(target, MANUAL)
            failing = True
        elif source == target and destination.name.endswith(".tmp"):
            moved_back.append(destination)
            target.write_text(REPAIR, encoding="utf-8")

    def verify(path: Path) -> os.stat_result:
        nonlocal failing
        if failing and path == target:
            failing = False
            raise OSError(errno.EIO, "injected one-shot verification read failure")
        if not inspectable and moved_back and path == moved_back[0]:
            raise OSError(errno.EIO, "injected read failure of the temporary")
        return lstat(path)

    monkeypatch.setattr(file_io, "_rename_noreplace", publish)
    monkeypatch.setattr(Path, "lstat", verify)
    yield moved_back


@pytest.mark.parametrize("timestamps", ["coarse", "fine"])
def test_a_concurrent_edit_moved_back_survives_the_cleanup(
    folder: Path, monkeypatch: pytest.MonkeyPatch, timestamps: str
) -> None:
    """AA-1, the restored concurrent-entry route (Astra's counterexample): the
    published candidate is edited in place, moved back by the rollback, and can't be
    put back past a newer repair. Its bytes aren't the candidate's, so it is a
    concurrent entry: it survives, with the repair, and is said to be there."""
    target = folder / "state.json"
    _with_timestamps(monkeypatch, timestamps)

    with (
        relocated_with_a_repair(monkeypatch, target, edit="published") as moved,
        state_file.collecting_writes() as wrote,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert target.read_text(encoding="utf-8") == REPAIR, "the newer repair stays"
    # Failed on d958c43 with coarse timestamps: the cleanup consumed these bytes.
    assert moved[0].read_text(encoding="utf-8") == MANUAL, "the displaced edit survives"
    assert f"the concurrent entry remains preserved at {moved[0]}" in said, said
    assert raised.value.preserved_path == moved[0]
    assert wrote == []


@pytest.mark.parametrize("timestamps", ["coarse", "fine"])
@pytest.mark.parametrize("proof", ["rejected", "unavailable"])
def test_a_left_behind_entry_that_is_not_proven_ours_survives_the_cleanup(
    folder: Path, monkeypatch: pytest.MonkeyPatch, timestamps: str, proof: str
) -> None:
    """AA-1, the left-behind route: the candidate is moved back and can't be put
    back past a newer repair; then its inode is edited in place. The cleanup's proof
    of its bytes fails ("rejected": they differ) or can't be made ("unavailable":
    the temporary can't be looked at). Either way it survives and is said to stay."""
    target = folder / "state.json"
    _with_timestamps(monkeypatch, timestamps)
    edit = "left" if proof == "rejected" else "published"

    with (
        relocated_with_a_repair(
            monkeypatch, target, edit=edit, inspectable=proof == "rejected"
        ) as moved,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert target.read_text(encoding="utf-8") == REPAIR, "the newer repair stays"
    # Failed on d958c43 with coarse timestamps: discarded after the proof failed.
    assert os.lstat(moved[0]) and Path(moved[0]).read_bytes() == MANUAL.encode(), said
    assert "was discarded" not in said, said
    if proof == "rejected":
        assert said.endswith(
            f"; the entry now at {moved[0]} is not provably the publication candidate, "
            "so it was left as it is"
        ), said
    else:
        assert f"whether the entry moved back is still at {moved[0]} could not be checked" in (
            said
        ), said
    assert raised.value.preserved_path == moved[0]


def test_a_concurrent_entry_said_to_remain_is_never_discarded_later(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AA-1, the order of things: what was moved back was not provably ours when it
    was inspected, so it was said to remain at the temporary; by the cleanup it
    proves ours (the bytes came back). What was said stays true: it is not
    discarded."""
    target = folder / "state.json"
    holds = state_file._holds_publication_candidate
    proofs: list[bool] = []

    def first_not_then_ours(
        entry: Path,
        candidate: file_io.PinnedPath,
        fingerprint: file_io.FileFingerprint,
        document: bytes,
    ) -> tuple[os.stat_result, bool]:
        status, proven = holds(entry, candidate, fingerprint, document)
        proven = proven and bool(proofs)  # the first inspection: not provably ours
        proofs.append(proven)
        return status, proven

    monkeypatch.setattr(state_file, "_holds_publication_candidate", first_not_then_ours)
    with (
        relocated_with_a_repair(monkeypatch, target, edit="never") as moved,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert proofs[0] is False
    assert f"the concurrent entry remains preserved at {moved[0]}" in said, said
    # Failed on 7fa7135's first version: proven ours by the cleanup, and discarded.
    assert moved[0].read_text(encoding="utf-8") == CANDIDATE, "still there, as said"
    assert raised.value.preserved_path == moved[0]


@pytest.mark.parametrize("timestamps", ["coarse", "fine"])
def test_a_late_write_after_a_positive_proof_survives(
    folder: Path, monkeypatch: pytest.MonkeyPatch, timestamps: str
) -> None:
    """The cold review of a4fa2f0's residual: the cleanup proves the left-behind
    candidate ours, then a writer still holding the old public file writes
    same-length bytes in place before it is settled. Nothing is truncated: with
    coarse timestamps the entry is retired intact, late bytes and all; with fine
    ones the move refuses the changed entry, which stays, said to have changed."""
    target = folder / "state.json"
    _with_timestamps(monkeypatch, timestamps)
    holds = state_file._holds_publication_candidate
    proofs: list[bool] = []

    def proof_then_late_write(
        entry: Path,
        candidate: file_io.PinnedPath,
        fingerprint: file_io.FileFingerprint,
        document: bytes,
    ) -> tuple[os.stat_result, bool]:
        result = holds(entry, candidate, fingerprint, document)
        proofs.append(result[1])
        if len(proofs) == 2 and result[1]:  # the cleanup's proof was positive
            _edit_in_place(entry, MANUAL)
        return result

    monkeypatch.setattr(state_file, "_holds_publication_candidate", proof_then_late_write)
    with (
        relocated_with_a_repair(monkeypatch, target, edit="never") as moved,
        pytest.raises(file_io.PathChangedError) as raised,
    ):
        state_file.write_atomic_text(target, CANDIDATE, replace_existing=False)

    said = str(raised.value)
    assert proofs == [True, True] and target.read_text(encoding="utf-8") == REPAIR
    assert "discarded" not in said, said
    retired = _retired_entries(folder)
    if timestamps == "coarse":
        # Failed on a4fa2f0: truncated by the discard, the late bytes were lost.
        assert len(retired) == 1 and retired[0].read_text(encoding="utf-8") == MANUAL
        assert not moved[0].exists()
        assert said.endswith(f"; the publication candidate was retired intact to {retired[0]}")
        assert raised.value.preserved_path == retired[0]
    else:
        # Failed on a4fa2f0 for its words: "the publication candidate remains
        # preserved", though it held the late bytes.
        assert retired == [] and moved[0].read_text(encoding="utf-8") == MANUAL
        assert said.endswith(
            f"; the entry at {moved[0]} changed after it was proven to be the publication "
            "candidate, so it was left as it is"
        ), said
        assert raised.value.preserved_path == moved[0]
