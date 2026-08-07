"""Removing a wallpaper, and refusing to.

This is the only code in the program that destroys anything, so most of what
is pinned here is what it declines to do. The scenario worth keeping in mind
throughout: the app downloads into a directory the user also keeps their own
photographs in, and this module is the whole of what stands between a delete
button and those photographs.

Nothing here touches the user's real directories. Every path is under
`tmp_path`, and the trash is redirected with `XDG_DATA_HOME`.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import unquote

import pytest

from wall_in_one.library import manage, pairing
from wall_in_one.library.manage import ManageError
from wall_in_one.library.model import Kind, MediaItem, Ownership

MARKER = ".managed-by-wall-in-one-v1.json"


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


def managed_directory(root: Path, name: str = "Wallhaven") -> Path:
    """A directory carrying the marker that says this app created it."""
    directory = root / "Wall-in-One" / name
    directory.mkdir(parents=True)
    (directory / MARKER).write_text(json.dumps({"kind": "wallhaven"}), encoding="utf-8")
    return directory


def downloaded(root: Path, name: str = "picture.jpg") -> MediaItem:
    """A wallpaper with both halves of ownership: marker and sidecar."""
    directory = managed_directory(root)
    path = directory / name
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    path.with_name(path.name + ".wallhaven.json").write_text("{}", encoding="utf-8")
    return item_for(path, Kind.STILL, Ownership.MANAGED)


def item_for(
    path: Path,
    kind: Kind = Kind.STILL,
    ownership: Ownership = Ownership.USER,
    still: Path | None = None,
) -> MediaItem:
    status = path.stat()
    return MediaItem(
        path=path,
        kind=kind,
        size=status.st_size,
        mtime=int(status.st_mtime),
        ownership=ownership,
        paired_still=still,
    )


# -- what it removes ------------------------------------------------------


def test_a_downloaded_wallpaper_goes_away(root: Path) -> None:
    item = downloaded(root)
    result = manage.remove(item, (root,))
    assert not item.path.exists()
    assert item.path in result.removed


def test_the_sidecar_goes_with_it(root: Path) -> None:
    """Left behind, it would be an orphan claiming a file that is not there."""
    item = downloaded(root)
    sidecar = item.path.with_name(item.path.name + ".wallhaven.json")
    manage.remove(item, (root,))
    assert not sidecar.exists()


def test_the_marker_stays_because_the_directory_is_still_ours(root: Path) -> None:
    item = downloaded(root)
    manage.remove(item, (root,))
    assert (item.path.parent / MARKER).is_file()


def test_a_generated_still_goes_with_its_video(root: Path) -> None:
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text("{}", encoding="utf-8")
    still_directory = pairing.still_directory(root)
    still_directory.mkdir(parents=True)
    still = still_directory / "clip.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\n")
    still_sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    still_sidecar.write_text(json.dumps({"still_path": str(still)}), encoding="utf-8")

    manage.remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, still), (root,))

    assert not video.exists()
    assert not still.exists()
    assert not still_sidecar.exists()


def test_a_still_the_user_made_themselves_stays(root: Path) -> None:
    """`foo-still.png` beside `foo.mp4` is the user's own file and their choice
    to keep, even once it has nothing left to pair with."""
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text("{}", encoding="utf-8")
    sibling = directory / "clip-still.png"
    sibling.write_bytes(b"\x89PNG\r\n\x1a\n")

    manage.remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, sibling), (root,))

    assert not video.exists()
    assert sibling.is_file()


def test_the_report_says_what_went(root: Path) -> None:
    item = downloaded(root)
    assert manage.remove(item, (root,)).describe() == "removed picture.jpg and 1 file beside it"


# -- what it refuses ------------------------------------------------------


def test_a_file_the_user_put_there_is_refused(root: Path) -> None:
    """The whole point. A wallpaper of their own in a directory of their own."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ManageError) as caught:
        manage.remove(item_for(theirs), (root,))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


def test_a_file_in_a_managed_directory_but_without_a_sidecar_is_refused(root: Path) -> None:
    """The user dropping their own picture into our download folder is the
    exact case a marker alone would get wrong."""
    directory = managed_directory(root)
    theirs = directory / "theirs.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ManageError) as caught:
        manage.remove(item_for(theirs, ownership=Ownership.MANAGED), (root,))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


def test_a_sidecar_without_a_marker_is_refused(root: Path) -> None:
    """Ownership needs both halves; either alone is forgeable by accident."""
    directory = root / "elsewhere"
    directory.mkdir()
    path = directory / "picture.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    path.with_name(path.name + ".wallhaven.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ManageError) as caught:
        manage.remove(item_for(path, ownership=Ownership.MANAGED), (root,))
    assert caught.value.kind == "not-ours"
    assert path.is_file()


def test_the_item_claiming_to_be_managed_does_not_make_it_so(root: Path) -> None:
    """A `MediaItem` comes from a scan that may be minutes old, and the marker
    can be removed in between. A stale record must never authorise an unlink."""
    item = downloaded(root)
    (item.path.parent / MARKER).unlink()
    with pytest.raises(ManageError) as caught:
        manage.remove(item, (root,))
    assert caught.value.kind == "not-ours"
    assert item.path.is_file()


def test_a_file_outside_every_root_is_refused(root: Path, tmp_path: Path) -> None:
    outside = tmp_path / "somewhere-else"
    outside.mkdir()
    path = outside / "picture.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    with pytest.raises(ManageError) as caught:
        manage.remove(item_for(path, ownership=Ownership.MANAGED), (root,))
    assert caught.value.kind == "outside-root"
    assert path.is_file()


def test_a_symlink_is_refused_rather_than_followed(root: Path, tmp_path: Path) -> None:
    """Otherwise a link inside the library is a way to delete anything."""
    precious = tmp_path / "precious.png"
    precious.write_bytes(b"\x89PNG\r\n\x1a\n")
    directory = managed_directory(root)
    link = directory / "picture.png"
    link.symlink_to(precious)
    link.with_name(link.name + ".wallhaven.json").write_text("{}", encoding="utf-8")
    through_the_link = MediaItem(
        path=link, kind=Kind.STILL, size=1, mtime=0, ownership=Ownership.MANAGED
    )
    with pytest.raises(ManageError) as caught:
        manage.remove(through_the_link, (root,))
    assert caught.value.kind == "symlink"
    assert precious.is_file()


def test_a_symlinked_still_is_not_followed_either(root: Path, tmp_path: Path) -> None:
    precious = tmp_path / "precious.png"
    precious.write_bytes(b"\x89PNG\r\n\x1a\n")
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text("{}", encoding="utf-8")
    still_directory = pairing.still_directory(root)
    still_directory.mkdir(parents=True)
    link = still_directory / "clip.png"
    link.symlink_to(precious)

    manage.remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, link), (root,))

    assert not video.exists()
    assert precious.is_file()


def test_a_file_already_gone_says_so(root: Path) -> None:
    item = downloaded(root)
    item.path.unlink()
    with pytest.raises(ManageError) as caught:
        manage.remove(item, (root,))
    assert caught.value.kind == "missing"


def test_no_roots_given_means_no_containment_check(root: Path) -> None:
    """The check is a belt for callers that have roots to hand, not the only
    defence -- ownership still has to hold."""
    item = downloaded(root)
    manage.remove(item)
    assert not item.path.exists()


# -- the trash ------------------------------------------------------------


def test_a_users_own_file_can_be_trashed(root: Path, data_home: Path) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = manage.trash(theirs)
    assert not theirs.exists()
    assert landed.is_file()
    assert landed.parent == data_home / "Trash" / "files"


def test_the_trash_record_can_restore_it(root: Path, data_home: Path) -> None:
    """A file with no record is a file the user cannot get back."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = manage.trash(theirs)
    record = data_home / "Trash" / "info" / f"{landed.name}.trashinfo"
    text = record.read_text(encoding="utf-8")
    assert text.startswith("[Trash Info]\n")
    recorded = next(line for line in text.splitlines() if line.startswith("Path="))
    assert unquote(recorded.removeprefix("Path=")) == str(theirs.absolute())
    assert "DeletionDate=" in text


def test_a_path_with_a_space_is_recorded_encoded(root: Path, data_home: Path) -> None:
    """The user's own library really is under a directory with a space in it."""
    awkward = root / "holiday photo.png"
    awkward.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = manage.trash(awkward)
    record = data_home / "Trash" / "info" / f"{landed.name}.trashinfo"
    text = record.read_text(encoding="utf-8")
    assert "%20" in text
    recorded = next(line for line in text.splitlines() if line.startswith("Path="))
    assert unquote(recorded.removeprefix("Path=")) == str(awkward.absolute())


def test_a_second_file_of_the_same_name_keeps_its_extension(root: Path, data_home: Path) -> None:
    """A restored `foo (1).mp4` is still obviously a video; `foo.mp4 (1)` is not."""
    first = root / "a" / "holiday.png"
    second = root / "b" / "holiday.png"
    for path in (first, second):
        path.parent.mkdir()
        path.write_bytes(b"\x89PNG\r\n\x1a\n")
    manage.trash(first)
    landed = manage.trash(second)
    assert landed.name == "holiday (1).png"
    assert (data_home / "Trash" / "info" / "holiday (1).png.trashinfo").is_file()


def test_trashing_something_that_is_not_there_says_so(root: Path) -> None:
    with pytest.raises(ManageError) as caught:
        manage.trash(root / "absent.png")
    assert caught.value.kind == "missing"


def test_a_failed_move_leaves_no_orphan_record(
    root: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record with no file is a stale entry a file manager ignores, but it
    should still not be left behind on a failure we saw happen."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")

    def explode(_source: object, _target: object) -> None:
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr("os.rename", explode)
    with pytest.raises(ManageError) as caught:
        manage.trash(theirs)
    assert caught.value.kind == "cross-device"
    assert theirs.is_file()
    assert list((data_home / "Trash" / "info").iterdir()) == []
