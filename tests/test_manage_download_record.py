"""What becomes of a wallpaper's download record when removing or trashing it fails.

The record (``<media>.wallhaven.json``, ``<media>.motionbgs.json``) is deletion
authority, so it leaves its name before the media can. It is only withdrawn:
whole, into a private claim. Every way out of `manage.remove` and `manage.trash`
after that point is one of three for it:

``back``
    the media never left its name, or came back: the record is at its name
    again, byte for byte;
``kept``
    the pending removal record is retained for recovery: the record is whole in
    its claim, and the error names where;
``consumed``
    the media left its name, and the record went with it.

What can't possibly succeed is refused before the withdrawal, with nothing
touched at all. The exits are reached by the fault that causes each; where one
can only be injected, its entry says so.
"""

from __future__ import annotations

import errno
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests import released
from tests.released import released_source  # noqa: F401
from tests.test_download_legacy_staging import _folder, _produce
from tests.test_manage import (
    REMOVAL_TOKEN,
    _downloaded_read_only,
    _expected,
    _fingerprint,
    _remove,
    _trash,
    downloaded,
)
from wall_in_one import file_io, paths
from wall_in_one.library import manage, scan
from wall_in_one.library.manage import ManageError
from wall_in_one.library.model import MediaItem
from wall_in_one.providers import download

#: Undoes whatever permissions a fault took away, before anything is looked at.
Undo = Callable[[], None]
Arm = Callable[[pytest.MonkeyPatch, MediaItem], Undo | None]


@pytest.fixture(autouse=True)
def data_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A trash of our own, so no test can reach the real one."""
    home = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(home))
    return home


@pytest.fixture
def root(tmp_path: Path) -> Path:
    directory = tmp_path / "wallpapers"
    directory.mkdir()
    return directory


def _record(item: MediaItem) -> Path:
    return item.path.with_name(item.path.name + ".wallhaven.json")


def _whole_copies(item: MediaItem, written: bytes) -> list[Path]:
    """Every claim entry beside ``item`` that still holds the record's bytes.

    A removal names the record's claim from its own token; a trash move given
    no token uses a nameless one in the retained folder.
    """
    folder = item.path.parent
    claims = [
        *folder.glob(f"{file_io.DELETION_CLAIM_PREFIX}*/entry"),
        *folder.glob(f"{file_io.RETAINED_ENTRY_DIRECTORY}/*/entry"),
    ]
    return [entry for entry in claims if entry.read_bytes() == written]


def _record_claim(item: MediaItem, token: str = REMOVAL_TOKEN) -> Path:
    """Where a removal with ``token`` keeps ``item``'s record while it is withdrawn."""
    named = manage._record_claim_token(token, ".wallhaven.json")
    return file_io.deletion_claim_directory(_record(item), named)


def _fate(item: MediaItem, written: bytes, said: str) -> str:
    """``back``, ``kept`` or ``consumed`` (see this module's docstring)."""
    record = _record(item)
    whole = _whole_copies(item, written)
    if record.exists():
        assert record.read_bytes() == written, "put back byte for byte"
        assert whole == [], "and nowhere else"
        return "back"
    if whole:
        (kept,) = whole
        assert f"its download record was kept at {kept}" in said, said
        return "kept"
    return "consumed"


def _tree(top: Path) -> list[str]:
    return sorted(str(path.relative_to(top)) for path in top.rglob("*"))


# -- refused before anything is withdrawn -------------------------------------


def test_a_read_only_download_is_refused_before_anything_is_touched(root: Path) -> None:
    """The ordinary case. A downloaded wallpaper that can't be opened for writing
    (mode 0444: a native EACCES) can't be destroyed, only moved into a claim and
    back, which would change the generation its record binds. It is refused
    first: file, record and download status are exactly as they were."""
    item = _downloaded_read_only(root)
    written = _record(item).read_bytes()
    generation = _fingerprint(item.path)
    before = _tree(root)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "local-io"
    assert not caught.value.committed and not caught.value.retain_intent
    said = str(caught.value)
    assert said.endswith(
        f"could not remove {item.path}: it can't be opened for writing "
        "(Permission denied); nothing was changed"
    ), said
    # Failed on ff1f967: the record was destroyed (a 0-byte tombstone), and the
    # error said only "the original file was restored".
    assert _record(item).read_bytes() == written
    assert _fingerprint(item.path) == generation, "not even moved"
    assert scan.has_download_sidecar(item.path), "still a download"
    assert _tree(root) == before, "nothing left behind"


def test_a_download_is_not_trashed_into_a_trash_that_cannot_be_prepared(
    root: Path, data_home: Path
) -> None:
    """The trash can't be made (its place is taken by a file: a native ENOTDIR).
    That is refused before the record is withdrawn: nothing is touched."""
    item = downloaded(root)
    written = _record(item).read_bytes()
    before = _tree(root)
    data_home.mkdir(parents=True, exist_ok=True)
    (data_home / "Trash").write_text("not a folder", encoding="utf-8")

    with pytest.raises(ManageError) as caught:
        _trash(item, (root,))

    assert caught.value.kind == "local-io" and not caught.value.committed
    assert "could not prepare the trash" in str(caught.value)
    # Failed on ff1f967: the record was destroyed, with the wallpaper untouched.
    assert _record(item).read_bytes() == written
    assert scan.has_download_sidecar(item.path), "still a download"
    assert _tree(root) == before, "nothing left behind"


def _another_filesystem(here: Path) -> Path | None:
    """A scratch folder on another filesystem than ``here``, or None where there is none."""
    shared = Path("/dev/shm")
    try:
        if shared.stat().st_dev == here.stat().st_dev or not os.access(shared, os.W_OK):
            return None
    except OSError:
        return None
    return shared


def test_a_download_on_another_filesystem_than_the_trash_is_refused_first(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real second filesystem (``/dev/shm``, where there is one): the move to
    the trash could never be a rename, so it is refused before the record is
    withdrawn."""
    elsewhere = _another_filesystem(root)
    if elsewhere is None:
        pytest.skip("no second writable filesystem here; the injected EXDEV exit covers it")
    item = downloaded(root)
    written = _record(item).read_bytes()
    before = _tree(root)
    with tempfile.TemporaryDirectory(prefix="wall-in-one-test-", dir=elsewhere) as scratch:
        monkeypatch.setenv("XDG_DATA_HOME", scratch)
        with pytest.raises(ManageError) as caught:
            _trash(item, (root,))
        assert list((Path(scratch) / "Trash" / "files").iterdir()) == []

    assert caught.value.kind == "cross-device" and not caught.value.committed
    assert "is on another filesystem, so it cannot be moved to the trash" in str(caught.value)
    # Failed on ff1f967: the record was destroyed, with the wallpaper untouched.
    assert _record(item).read_bytes() == written
    assert scan.has_download_sidecar(item.path), "still a download"
    assert _tree(root) == before, "nothing left behind"


# -- every exit of a removal after the withdrawal ------------------------------


def _claim_folder(item: MediaItem) -> Path:
    return file_io.deletion_claim_directory(item.path, REMOVAL_TOKEN)


def _media_claims(
    monkeypatch: pytest.MonkeyPatch,
    *,
    before: Callable[[], object] | None = None,
    after: Callable[[file_io.ClaimedPath], None] | None = None,
) -> None:
    """Run ``before``/``after`` around the claim of the media (not the record's)."""
    claim_for_deletion = file_io.claim_for_deletion

    def claim(path: Path, **options: object) -> file_io.ClaimedPath:
        media = options.get("operation_token") == REMOVAL_TOKEN
        if media and before is not None:
            before()
        claimed = claim_for_deletion(path, **options)  # type: ignore[arg-type]
        if media and after is not None:
            after(claimed)
        return claimed

    monkeypatch.setattr(file_io, "claim_for_deletion", claim)


def _read_only_once_claimed(claimed: file_io.ClaimedPath) -> None:
    """So the claimed media can't be opened to be destroyed: a discard's native
    EACCES. Before the claim it could be, so the pre-flight passed."""
    claimed.path.chmod(0o444)


def _claim_unverified(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> Undo:
    """The claim folder can't be searched once the media is in it: the claim is
    neither verified nor undone, and the pending removal record is retained."""
    folder = _claim_folder(item)
    rename = file_io._rename_noreplace
    done: list[bool] = []

    def move(old: Path, new: Path) -> None:
        rename(old, new)
        if not done and old.name == item.path.name and new.name == "entry":
            done.append(True)
            folder.chmod(0o000)

    monkeypatch.setattr(file_io, "_rename_noreplace", move)
    return lambda: folder.chmod(0o700)


def _replaced_before_the_claim(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    def replace() -> None:
        item.path.rename(item.path.with_name("moved-aside.jpg"))
        item.path.write_bytes(b"something else")

    _media_claims(monkeypatch, before=replace)


def _gone_before_the_claim(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Its name is empty by the time it is claimed (the pinned file is elsewhere)."""
    _media_claims(
        monkeypatch, before=lambda: item.path.rename(item.path.with_name("moved-aside.jpg"))
    )


def _claim_folder_taken(_monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """The removal's claim folder already exists: it can't be made (a native EEXIST)."""
    _claim_folder(item).mkdir()


def _restored(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    _media_claims(monkeypatch, after=_read_only_once_claimed)


def _moved_back_unseen(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> Undo:
    """Not destroyed, moved back, and then its own folder can't be searched."""
    public = item.path.parent
    rename = file_io._rename_noreplace
    done: list[bool] = []

    def move(old: Path, new: Path) -> None:
        rename(old, new)
        if not done and old.name == "entry" and new.name == item.path.name:
            done.append(True)
            public.chmod(0o000)

    _media_claims(monkeypatch, after=_read_only_once_claimed)
    monkeypatch.setattr(file_io, "_rename_noreplace", move)
    return lambda: public.chmod(0o755)


def _restore_blocked(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Not destroyed, and something else took its name before it could go back."""
    restore = file_io.ClaimedPath.restore

    def blocked(claimed: file_io.ClaimedPath) -> bool:
        if claimed.original == item.path:
            item.path.write_bytes(b"something else")
        return restore(claimed)

    _media_claims(monkeypatch, after=_read_only_once_claimed)
    monkeypatch.setattr(file_io.ClaimedPath, "restore", blocked)


def _claim_unseen(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> Undo:
    folder = _claim_folder(item)
    _media_claims(monkeypatch, after=lambda _claimed: folder.chmod(0o600))
    return lambda: folder.chmod(0o700)


def _own_path_unseen(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> Undo:
    """Not destroyed; something else moves it back first, and then its own
    folder can't be searched: it left its claim and may be at its path."""
    public = item.path.parent
    entry = _claim_folder(item) / "entry"
    restore = file_io.ClaimedPath.restore

    def somebody_first(claimed: file_io.ClaimedPath) -> bool:
        if claimed.original == item.path:
            entry.rename(item.path)
            public.chmod(0o000)
        return restore(claimed)

    _media_claims(monkeypatch, after=_read_only_once_claimed)
    monkeypatch.setattr(file_io.ClaimedPath, "restore", somebody_first)
    return lambda: public.chmod(0o755)


def _claim_emptied(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Something else removes the claimed media before it is destroyed: it is
    neither in its claim nor at its path, so the removal is committed."""
    entry = _claim_folder(item) / "entry"
    discard = file_io.ClaimedPath.discard

    def emptied(claimed: file_io.ClaimedPath) -> None:
        if claimed.original == item.path:
            entry.unlink()
        discard(claimed)

    monkeypatch.setattr(file_io.ClaimedPath, "discard", emptied)


def _not_persisted(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Injected: the folder's sync fails once the media is gone from it."""
    sync = paths.fsync_directory

    def fail_once_gone(directory: Path) -> None:
        if directory == item.path.parent and not item.path.exists():
            raise OSError(errno.EIO, "injected: the folder could not be synced")
        sync(directory)

    monkeypatch.setattr(paths, "fsync_directory", fail_once_gone)


def _claim_not_durable(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Injected: no room at the sync right after the media moves into its claim."""
    sync = file_io._fsync_directory_capability
    folder = _claim_folder(item)

    def no_room(descriptor: int, **options: Any) -> None:
        if options.get("logical_path") == folder and not item.path.exists():
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        sync(descriptor, **options)

    monkeypatch.setattr(file_io, "_fsync_directory_capability", no_room)


def _record_claim_not_durable(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Injected: no room at the sync right after the *record* moves into its
    claim. The media has not been touched yet."""
    sync = file_io._fsync_directory_capability
    record = _record(item)
    done: list[bool] = []

    def no_room(descriptor: int, **options: Any) -> None:
        if not done and not record.exists() and item.path.exists():
            done.append(True)
            raise OSError(errno.ENOSPC, os.strerror(errno.ENOSPC))
        sync(descriptor, **options)

    monkeypatch.setattr(file_io, "_fsync_directory_capability", no_room)


def _record_claim_unverified(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> Undo:
    """The record's claim folder can't be searched once the record is in it: its
    claim is neither verified nor undone."""
    folder = _record_claim(item)
    record = _record(item)
    rename = file_io._rename_noreplace
    done: list[bool] = []

    def move(old: Path, new: Path) -> None:
        rename(old, new)
        if not done and old.name == record.name and new.name == "entry":
            done.append(True)
            folder.chmod(0o000)

    monkeypatch.setattr(file_io, "_rename_noreplace", move)
    return lambda: folder.chmod(0o700)


def _nothing(_monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    return None


#: exit, its fault, the token used, (kind, committed, retained), the record's fate.
_REMOVE_EXITS: list[tuple[str, Arm, str, tuple[str | None, bool, bool], str]] = [
    ("claim-unverified", _claim_unverified, REMOVAL_TOKEN, ("changed", False, True), "kept"),
    ("claim-not-durable", _claim_not_durable, REMOVAL_TOKEN, ("local-io", False, True), "kept"),
    (
        "record-claim-not-durable",
        _record_claim_not_durable,
        REMOVAL_TOKEN,
        ("local-io", False, True),
        "kept",
    ),
    ("replaced", _replaced_before_the_claim, REMOVAL_TOKEN, ("changed", False, False), "back"),
    ("gone", _gone_before_the_claim, REMOVAL_TOKEN, ("changed", False, False), "back"),
    ("bad-token", _nothing, "not-a-token", ("unrecorded", False, False), "back"),
    ("claim-refused", _claim_folder_taken, REMOVAL_TOKEN, ("local-io", False, False), "back"),
    ("moved-back-unseen", _moved_back_unseen, REMOVAL_TOKEN, ("local-io", False, True), "kept"),
    ("restored", _restored, REMOVAL_TOKEN, ("local-io", False, False), "back"),
    ("restore-blocked", _restore_blocked, REMOVAL_TOKEN, ("local-io", False, True), "kept"),
    ("claim-unseen", _claim_unseen, REMOVAL_TOKEN, ("local-io", False, True), "kept"),
    ("own-path-unseen", _own_path_unseen, REMOVAL_TOKEN, ("local-io", False, True), "kept"),
    ("claim-emptied", _claim_emptied, REMOVAL_TOKEN, ("local-io", True, False), "consumed"),
    ("not-persisted", _not_persisted, REMOVAL_TOKEN, ("local-io", True, False), "consumed"),
    ("removed", _nothing, REMOVAL_TOKEN, (None, True, False), "consumed"),
]


@pytest.mark.parametrize(
    ("arm", "token", "outcome", "fate"),
    [case[1:] for case in _REMOVE_EXITS],
    ids=[case[0] for case in _REMOVE_EXITS],
)
def test_the_download_record_at_every_exit_of_a_removal(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    arm: Arm,
    token: str,
    outcome: tuple[str | None, bool, bool],
    fate: str,
) -> None:
    item = downloaded(root)
    written = _record(item).read_bytes()
    untouched = _fingerprint(item.path)
    undo = arm(monkeypatch, item)
    caught: ManageError | None = None
    try:
        manage.remove(
            item,
            (root,),
            expected_source=_expected(item.path),
            expected_fingerprint=_fingerprint(item.path),
            operation_token=token,
        )
    except ManageError as error:
        caught = error
    finally:
        if undo is not None:
            undo()

    said = "" if caught is None else str(caught)
    kind, committed, _retained = outcome
    if caught is None:
        assert kind is None
    else:
        assert (caught.kind, caught.committed, caught.retain_intent) == outcome, said
    # Failed on ff1f967 for every "back" and "kept": the record was destroyed.
    assert _fate(item, written, said) == fate, said
    assert committed == (fate == "consumed")
    assert "/proc/" not in said, said
    # Kept, it is where the removal's token leads; otherwise that folder is gone.
    claim = _record_claim(item, token)
    assert (claim / "entry").exists() == (fate == "kept")
    assert claim.exists() == (fate == "kept")
    if fate == "back" and item.path.exists() and _fingerprint(item.path) == untouched:
        assert scan.has_download_sidecar(item.path), "untouched, so still a download"


def test_a_record_whose_folder_went_away_is_put_back_where_the_folder_went(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The wallpaper's whole folder is moved away before the media is claimed, so
    the media "is no longer there" (the removal's "missing" exit). The record's
    claim holds the folder itself, not its name: the record is put back beside
    its media, wherever the folder went."""
    item = downloaded(root)
    written = _record(item).read_bytes()
    folder = item.path.parent
    aside = folder.with_name("moved-aside")
    _media_claims(monkeypatch, before=lambda: folder.rename(aside))

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "missing" and not caught.value.committed
    assert str(caught.value).endswith(f"{item.path} is no longer there")
    # Failed on ff1f967: destroyed.
    assert (aside / _record(item).name).read_bytes() == written
    assert (aside / item.path.name).is_file()
    assert list((aside / file_io.RETAINED_ENTRY_DIRECTORY).glob("*/entry")) == []


def test_a_restored_download_is_said_to_count_as_a_local_file_now(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rare residue of the pre-flight: the media could be opened for writing,
    then could not once claimed (it turned read-only: a native EACCES), so it
    was moved back. That round trip changed its change time, which its record
    binds. The record is put back as it was, never re-bound, and the error says
    what that means: the wallpaper counts as a local file, and nothing was
    deleted."""
    item = downloaded(root)
    written = _record(item).read_bytes()
    original = item.path.read_bytes()
    _restored(monkeypatch, item)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    said = str(caught.value)
    assert item.path.read_bytes() == original
    # Failed on ff1f967: the record was destroyed and nothing was said of it.
    assert _record(item).read_bytes() == written
    assert not scan.has_download_sidecar(item.path), "its change time moved on"
    assert said.endswith(
        f"could not remove {item.path}; the original file was restored: Permission denied; "
        "its download record was put back, but no longer matches the file (moving the file "
        "changed its change time), so the wallpaper now counts as a local file; nothing was "
        "deleted"
    ), said


def test_a_record_that_cannot_be_put_back_is_kept_whole_and_named(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The removal's claim folder can't be made, so nothing was removed; and by
    then something else has taken the record's name (a native EEXIST on the
    put-back). The record is not destroyed and not forced over the other file:
    it stays whole in its claim, and the error names where."""
    item = downloaded(root)
    written = _record(item).read_bytes()

    def take_the_name() -> None:
        _claim_folder(item).mkdir()
        _record(item).write_text("somebody else's", encoding="utf-8")

    _media_claims(monkeypatch, before=take_the_name)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    said = str(caught.value)
    (kept,) = _whole_copies(item, written)
    assert _record(item).read_text(encoding="utf-8") == "somebody else's"
    assert caught.value.kind == "local-io" and not caught.value.committed
    # Failed on 651e5f1: the pending removal record was cancelled, and nothing
    # would ever look in that claim again.
    assert caught.value.retain_intent
    assert said.endswith(
        f"something else is now at {_record(item)}, so its download record was kept at {kept}; "
        "its pending removal record was retained, so the next library scan or start puts the "
        "download record back"
    ), said


def test_a_claim_that_could_not_be_made_durable_is_said_as_that(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The disk is full at the sync right after the media moved into its claim.
    Nothing changed: the file is whole, in the claim. It is said as a move that
    could not be made durable, with the system's reason, and not as a file that
    changed while it was claimed."""
    item = downloaded(root)
    original = item.path.read_bytes()
    _claim_not_durable(monkeypatch, item)

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    said = str(caught.value)
    claimed = _claim_folder(item) / "entry"
    assert claimed.read_bytes() == original and not item.path.exists()
    assert (caught.value.kind, caught.value.committed, caught.value.retain_intent) == (
        "local-io",
        False,
        True,
    )
    # Failed on 651e5f1: "changed while removal claimed it; an unverified entry
    # was preserved at ...".
    assert "changed" not in said, said
    assert said.startswith(
        f"local-io: {item.path} was moved into its removal\u2019s claim, but that could not be "
        f"made durable (No space left on device), so it was not deleted. It is at {claimed}, "
        "and its pending removal record was retained: the removal is finished, with what "
        "belongs to the wallpaper, at the next library scan or start"
    ), said


# -- the record's own claim ---------------------------------------------------


@pytest.mark.parametrize("how", ["remove", "trash"])
def test_a_record_whose_own_claim_could_not_be_made_durable_keeps_the_pending_removal(
    root: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    """The disk is full at the sync right after the download record moved into
    its claim, before the media is touched. The record is whole, in the claim
    the removal's token names, and it is not one of the records the removal
    goes on to settle. So the pending removal record is kept, for the next start
    to put the record back; cancelled, nothing would look in that claim again
    and the wallpaper would be a local file from then on. And it is said as a
    move that could not be made durable, not as a record that changed."""
    item = downloaded(root)
    written = _record(item).read_bytes()
    untouched = _fingerprint(item.path)
    _record_claim_not_durable(monkeypatch, item)

    with pytest.raises(ManageError) as caught:
        # As the session calls either: with the pending removal's token.
        (manage.remove if how == "remove" else manage.trash)(
            item,
            (root,),
            expected_source=_expected(item.path),
            expected_fingerprint=_fingerprint(item.path),
            operation_token=REMOVAL_TOKEN,
        )

    said = str(caught.value)
    kept = _record_claim(item) / "entry"
    assert _fingerprint(item.path) == untouched, "the media was never touched"
    assert kept.read_bytes() == written and not _record(item).exists()
    # Failed on 62de5ac: ("changed", False, False), so the session cancelled the
    # pending removal record and the record stayed in its claim for good.
    assert (caught.value.kind, caught.value.committed, caught.value.retain_intent) == (
        "local-io",
        False,
        True,
    ), said
    # Failed on 62de5ac: "... changed before removal; the wallpaper was left
    # untouched; an unverified entry was preserved at ...".
    assert "changed" not in said, said
    assert said == (
        f"local-io: {_record(item)} was moved into its claim, but that could not be made "
        "durable (No space left on device); the wallpaper was left untouched; its download "
        f"record was kept at {kept}; its pending removal record was retained, so the next "
        "library scan or start puts the download record back"
    )
    assert not _trash_folder("files").exists() or list(_trash_folder("files").iterdir()) == []


def test_a_record_whose_own_claim_could_not_be_verified_keeps_the_pending_removal(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The record moved into its claim and could then be neither verified nor
    moved back (its claim folder can't be searched). What is there may be the
    record: the pending removal record is kept, so the next start looks again,
    and nothing promises more than that."""
    item = downloaded(root)
    written = _record(item).read_bytes()
    untouched = _fingerprint(item.path)
    undo = _record_claim_unverified(monkeypatch, item)
    try:
        with pytest.raises(ManageError) as caught:
            _remove(item, (root,))
    finally:
        undo()

    said = str(caught.value)
    kept = _record_claim(item) / "entry"
    assert _fingerprint(item.path) == untouched, "the media was never touched"
    assert kept.read_bytes() == written and not _record(item).exists()
    # Failed on 62de5ac: retain_intent was False.
    assert (caught.value.kind, caught.value.committed, caught.value.retain_intent) == (
        "changed",
        False,
        True,
    ), said
    assert said == (
        f"changed: {_record(item)} changed before removal; the wallpaper was left untouched; "
        f"an unverified entry was preserved at {kept}; its pending removal record was "
        "retained, so the next library scan or start looks at what is there again"
    )


# -- every exit of a trash move after the withdrawal ---------------------------


def _trash_folder(name: str) -> Path:
    return manage.trash_directory() / name


def _before_the_move(
    monkeypatch: pytest.MonkeyPatch, act: Callable[[Path], object], *, at_call: int = 1
) -> None:
    """Run ``act(record)`` at the ``at_call``-th check of the reserved trash
    record: the first is at its reservation, the second just before the media
    is moved, the third just after."""
    require = manage._require_exact_trash_record
    calls: list[Path] = []

    def checked(record: Path, pin: file_io.PinnedPath, payload: bytes) -> None:
        calls.append(record)
        if len(calls) == at_call:
            act(record)
        require(record, pin, payload)

    monkeypatch.setattr(manage, "_require_exact_trash_record", checked)


def _record_folder_read_only(_monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> Undo:
    """The trash's info folder can't be written to (a native EACCES)."""
    info = _trash_folder("info")
    info.mkdir(parents=True)
    _trash_folder("files").mkdir()
    info.chmod(0o500)
    return lambda: info.chmod(0o700)


def _record_not_pinned(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    """Injected: the new trash record can't be pinned."""
    pin_regular_path = file_io.pin_regular_path

    def refuse(path: Path, **expected: object) -> file_io.PinnedPath:
        if path.name.startswith(".wall-in-one-trashinfo-"):
            raise OSError(errno.EIO, "injected: the trash record could not be pinned")
        return pin_regular_path(path, **expected)  # type: ignore[arg-type]

    monkeypatch.setattr(file_io, "pin_regular_path", refuse)


def _record_not_written(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    """Injected: the new trash record can't be synced."""
    fsync = os.fsync
    info = _trash_folder("info")

    def refuse(descriptor: int) -> None:
        if Path(os.readlink(f"/proc/self/fd/{descriptor}")).parent == info:
            raise OSError(errno.EIO, "injected: the trash record could not be written")
        fsync(descriptor)

    monkeypatch.setattr(os, "fsync", refuse)


def _record_not_reserved(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    """Injected: the trash record's name can't be linked."""

    def refuse(*_arguments: object, **_options: object) -> None:
        raise OSError(errno.EIO, "injected: the trash record could not be reserved")

    monkeypatch.setattr(os, "link", refuse)


def _edit(record: Path) -> None:
    record.write_text("edited", encoding="utf-8")


def _record_changed_at_its_reservation(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    _before_the_move(monkeypatch, _edit, at_call=1)


def _record_changed_before_the_move(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    _before_the_move(monkeypatch, _edit, at_call=2)


def _replaced_before_the_move(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    def replace(_record: Path) -> None:
        item.path.rename(item.path.with_name("moved-aside.jpg"))
        item.path.write_bytes(b"something else")

    _before_the_move(monkeypatch, replace, at_call=2)


def _trash_gone_before_the_move(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    """The trash's files folder is gone by the move (a native ENOENT on the rename)."""
    files = _trash_folder("files")
    _before_the_move(
        monkeypatch, lambda _record: files.rename(files.with_name("moved-aside")), at_call=2
    )


def _rename_says_another_filesystem(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Injected at the rename: EXDEV, as between two mounts of one filesystem,
    which the pre-flight can't tell apart."""
    rename = file_io._rename_noreplace

    def move(old: Path, new: Path) -> None:
        if old.name == item.path.name and new.parent.name == "files":
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        rename(old, new)

    monkeypatch.setattr(file_io, "_rename_noreplace", move)


def _trash_read_only(_monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> Undo:
    """The trash's files folder can't be written to (a native EACCES on the move)."""
    files = _trash_folder("files")
    files.mkdir(parents=True)
    files.chmod(0o500)
    return lambda: files.chmod(0o700)


def _names_exhausted(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """Every name the trash would try is already recorded."""
    monkeypatch.setattr(manage, "MAX_TRASH_ATTEMPTS", 2)
    info = _trash_folder("info")
    info.mkdir(parents=True)
    for name in manage._trash_names(item.path):
        (info / f"{name}.trashinfo").write_text("taken", encoding="utf-8")


def _moved_unseen(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> Undo:
    """The trash's files folder can't be searched right after the move: the
    media is in the trash, unverified, and the move is committed."""
    files = _trash_folder("files")
    rename = file_io._rename_noreplace
    done: list[bool] = []

    def move(old: Path, new: Path) -> None:
        rename(old, new)
        if not done and old.name == item.path.name and new.parent.name == "files":
            done.append(True)
            files.chmod(0o000)

    monkeypatch.setattr(file_io, "_rename_noreplace", move)
    return lambda: files.chmod(0o700)


def _stuck(monkeypatch: pytest.MonkeyPatch, which: Callable[[Path], bool]) -> None:
    """Injected: withdrawing the trash-metadata entry ``which`` accepts reports
    that it had to be left where it is."""
    unlink_if_same = manage._unlink_if_same

    def unlink(path: Path, *arguments: object, **options: object) -> Path | None:
        left = unlink_if_same(path, *arguments, **options)  # type: ignore[arg-type]
        return path if which(path) else left

    monkeypatch.setattr(manage, "_unlink_if_same", unlink)


def _name_taken_and_record_stuck(monkeypatch: pytest.MonkeyPatch, item: MediaItem) -> None:
    """The trash already holds a file so named (a native EEXIST on the move), and
    the record reserved for that name can't be withdrawn."""
    files = _trash_folder("files")
    files.mkdir(parents=True)
    (files / item.path.name).write_bytes(b"an earlier one")
    _stuck(monkeypatch, lambda path: path.name == f"{item.path.name}.trashinfo")


def _temporary_stuck(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    """The move went through; the temporary trash record can't be withdrawn."""
    _stuck(monkeypatch, lambda path: path.name.startswith(".wall-in-one-trashinfo-"))


def _record_changed_after_the_move(monkeypatch: pytest.MonkeyPatch, _item: MediaItem) -> None:
    _before_the_move(monkeypatch, _edit, at_call=3)


#: exit, its fault, (kind, committed), the record's fate.
_TRASH_EXITS: list[tuple[str, Arm, tuple[str | None, bool], str]] = [
    # Given no token (as here), the record's claim is a nameless one no later
    # start would find: there is no pending record to keep, and the reply says
    # where the record is without promising a put-back.
    ("record-claim-not-durable", _record_claim_not_durable, ("local-io", False), "kept"),
    ("record-folder-read-only", _record_folder_read_only, ("local-io", False), "back"),
    ("record-not-pinned", _record_not_pinned, ("local-io", False), "back"),
    ("record-not-written", _record_not_written, ("local-io", False), "back"),
    ("record-not-reserved", _record_not_reserved, ("local-io", False), "back"),
    ("record-changed-early", _record_changed_at_its_reservation, ("changed", False), "back"),
    ("record-changed", _record_changed_before_the_move, ("changed", False), "back"),
    ("replaced", _replaced_before_the_move, ("changed", False), "back"),
    ("trash-gone", _trash_gone_before_the_move, ("missing", False), "back"),
    ("another-filesystem", _rename_says_another_filesystem, ("cross-device", False), "back"),
    ("trash-read-only", _trash_read_only, ("local-io", False), "back"),
    ("names-exhausted", _names_exhausted, ("local-io", False), "back"),
    ("record-stuck", _name_taken_and_record_stuck, ("local-io", False), "back"),
    ("moved-unseen", _moved_unseen, ("changed", True), "consumed"),
    ("not-persisted", _not_persisted, ("local-io", True), "consumed"),
    ("record-changed-after", _record_changed_after_the_move, ("changed", True), "consumed"),
    ("temporary-stuck", _temporary_stuck, ("local-io", True), "consumed"),
    ("trashed", _nothing, (None, True), "consumed"),
]


@pytest.mark.parametrize(
    ("arm", "outcome", "fate"),
    [case[1:] for case in _TRASH_EXITS],
    ids=[case[0] for case in _TRASH_EXITS],
)
def test_the_download_record_at_every_exit_of_a_trash_move(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    arm: Arm,
    outcome: tuple[str | None, bool],
    fate: str,
) -> None:
    item = downloaded(root)
    written = _record(item).read_bytes()
    untouched = _fingerprint(item.path)
    undo = arm(monkeypatch, item)
    caught: ManageError | None = None
    try:
        _trash(item, (root,))
    except ManageError as error:
        caught = error
    finally:
        if undo is not None:
            undo()

    said = "" if caught is None else str(caught)
    kind, committed = outcome
    if caught is None:
        assert kind is None
    else:
        assert (caught.kind, caught.committed) == outcome, said
        assert not caught.retain_intent
    # Failed on ff1f967 for every "back": the record was destroyed, though the
    # wallpaper never left its folder.
    assert _fate(item, written, said) == fate, said
    assert committed == (fate == "consumed")
    assert "pending removal record" not in said, "no token: nothing later is promised"
    if fate == "back" and item.path.exists() and _fingerprint(item.path) == untouched:
        assert scan.has_download_sidecar(item.path), "untouched, so still a download"


@pytest.mark.parametrize("interrupted", [False, True], ids=["refused", "interrupted"])
def test_a_trash_move_given_its_token_names_the_record_claim_from_it(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, interrupted: bool
) -> None:
    """The session passes a trash move its pending-removal token, and the record
    is then claimed where that token leads, as a removal's is. Refused at the
    rename (EXDEV, injected), the media never moved: the record is put back and
    the claim folder is gone. Interrupted during the move, the media may have
    moved: the record is kept there, for the replay to settle."""
    item = downloaded(root)
    written = _record(item).read_bytes()
    rename = file_io._rename_noreplace

    def move(old: Path, new: Path) -> None:
        if old.name == item.path.name and new.parent.name == "files":
            if interrupted:
                raise KeyboardInterrupt
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        rename(old, new)

    monkeypatch.setattr(file_io, "_rename_noreplace", move)

    with pytest.raises(KeyboardInterrupt if interrupted else ManageError):
        manage.trash(
            item,
            (root,),
            expected_source=_expected(item.path),
            expected_fingerprint=_fingerprint(item.path),
            operation_token=REMOVAL_TOKEN,
        )

    claim = _record_claim(item)
    if interrupted:
        assert not _record(item).exists()
        assert (claim / "entry").read_bytes() == written
    else:
        assert _record(item).read_bytes() == written
        assert not claim.exists()
        assert scan.has_download_sidecar(item.path), "untouched, so its record still binds it"


# -- what the released version left --------------------------------------------


def _scanned(root: Path, media: Path) -> MediaItem:
    """``media`` as this checkout's scan labels it: that decides remove or trash."""
    (item,) = (found for found in scan.scan((root,)).items if found.path == media)
    return item


def test_a_record_the_released_version_wrote_comes_back_after_a_failed_removal(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    """Rule 4. The download and its record are made by 0.2.2 itself. This
    checkout's scan calls it a managed download, so it is removed, not trashed;
    the removal's claim folder can't be made (a native EEXIST), so nothing is
    removed. The record 0.2.2 wrote is at its name again, byte for byte, and the
    wallpaper is still the download it was."""
    root = tmp_path / "wallpapers"
    _produce(released_source, root, "install")
    media = _folder(root) / "aurora.mp4"
    record = Path(str(media) + ".motionbgs.json")
    written = record.read_bytes()
    item = _scanned(root, media)
    assert item.deletable and item.provider == "MotionBGS", "0.2.2's record is authority here"
    file_io.deletion_claim_directory(media, REMOVAL_TOKEN).mkdir()

    with pytest.raises(ManageError) as caught:
        _remove(item, (root,))

    assert caught.value.kind == "local-io" and not caught.value.committed
    assert "could not claim" in str(caught.value)
    # Failed on ff1f967: destroyed.
    assert record.read_bytes() == written
    assert _scanned(root, media).deletable, "still a download"
    assert released.VERSION == "0.2.2"


def test_a_record_the_released_version_wrote_comes_back_after_a_failed_trash(
    released_source: Path,  # noqa: F811
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rule 4, for the trash. A download is only ever trashed when the scan
    calls it a local file although its record is exact: here its folder's marker
    is gone. The trash move then fails (EXDEV, injected at the rename), and the
    record 0.2.2 wrote is at its name again, byte for byte."""
    root = tmp_path / "wallpapers"
    _produce(released_source, root, "install")
    media = _folder(root) / "aurora.mp4"
    record = Path(str(media) + ".motionbgs.json")
    written = record.read_bytes()
    (_folder(root) / download.MOTIONBGS_LOCATION.marker_name).unlink()
    item = _scanned(root, media)
    assert not item.deletable and item.provider == "local"
    withdrawn: list[bool] = []
    rename = file_io._rename_noreplace

    def move(old: Path, new: Path) -> None:
        if old.name == media.name and new.parent.name == "files":
            withdrawn.append(not record.exists())
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        rename(old, new)

    monkeypatch.setattr(file_io, "_rename_noreplace", move)

    with pytest.raises(ManageError) as caught:
        _trash(item, (root,))

    assert caught.value.kind == "cross-device" and not caught.value.committed
    assert withdrawn == [True], "it was withdrawn, so this is the put-back"
    # Failed on ff1f967: destroyed.
    assert record.read_bytes() == written
    assert scan.has_download_sidecar(media), "its record still binds it"
