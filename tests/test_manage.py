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
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import unquote

import pytest

from wall_in_one.library import manage, pairing, scan
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
    if name == "MotionBGS":
        marker = directory / ".wall-in-one-motionbgs-managed.json"
        payload = {
            "schema": 1,
            "owner": "goober/wall-in-one",
            "provider": "MotionBGS",
        }
    else:
        marker = directory / MARKER
        payload = {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": "Wallhaven",
            "kind": "wallhaven",
            "ownership": "managed",
        }
    marker.write_text(json.dumps(payload), encoding="utf-8")
    return directory


def provenance(path: Path, provider: str) -> str:
    return json.dumps(
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": provider,
            "path": str(path),
        }
    )


def downloaded(root: Path, name: str = "picture.jpg") -> MediaItem:
    """A wallpaper with both halves of ownership: marker and sidecar."""
    directory = managed_directory(root)
    path = directory / name
    path.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    path.with_name(path.name + ".wallhaven.json").write_text(
        provenance(path, "Wallhaven"), encoding="utf-8"
    )
    return item_for(path, Kind.STILL, Ownership.MANAGED)


def item_for(
    path: Path,
    kind: Kind = Kind.STILL,
    ownership: Ownership = Ownership.USER,
    still: Path | None = None,
    provider: str = "local",
) -> MediaItem:
    status = path.stat()
    return MediaItem(
        path=path,
        kind=kind,
        size=status.st_size,
        mtime=int(status.st_mtime),
        ownership=ownership,
        paired_still=still,
        provider=provider,
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


def test_an_unrelated_provider_named_json_is_not_removed_as_a_companion(root: Path) -> None:
    item = downloaded(root)
    unrelated = item.path.with_name(item.path.name + ".motionbgs.json")
    unrelated.write_text("{}", encoding="utf-8")

    manage.remove(item, (root,))

    assert unrelated.read_text(encoding="utf-8") == "{}"


def test_the_marker_stays_because_the_directory_is_still_ours(root: Path) -> None:
    item = downloaded(root)
    manage.remove(item, (root,))
    assert (item.path.parent / MARKER).is_file()


def test_a_generated_still_goes_with_its_video(root: Path) -> None:
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    still_directory = pairing.still_directory(root)
    still_directory.mkdir(parents=True)
    still = still_directory / f"{pairing.automatic_still_stem(video)}.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\n")
    still_sidecar = video.with_name(video.name + pairing.SIDECAR_SUFFIX)
    still_sidecar.write_text(json.dumps({"still_path": str(still)}), encoding="utf-8")

    manage.remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, still), (root,))

    assert not video.exists()
    assert not still.exists()
    assert not still_sidecar.exists()


def test_a_legacy_shared_still_is_not_deleted_with_one_video(root: Path) -> None:
    """Old releases keyed generated stills by basename, so two videos could
    share one. Leaving that legacy file is safer than breaking the survivor."""
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
    still_directory = pairing.still_directory(root)
    still_directory.mkdir(parents=True)
    legacy = still_directory / "clip.png"
    legacy.write_bytes(b"\x89PNG\r\n\x1a\n")
    video.with_name(video.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({"still_path": str(legacy)}), encoding="utf-8"
    )

    manage.remove(item_for(video, Kind.VIDEO, Ownership.MANAGED, legacy), (root,))

    assert legacy.is_file()


def test_a_still_the_user_made_themselves_stays(root: Path) -> None:
    """`foo-still.png` beside `foo.mp4` is the user's own file and their choice
    to keep, even once it has nothing left to pair with."""
    directory = managed_directory(root, "MotionBGS")
    video = directory / "clip.mp4"
    video.write_bytes(b"0" * 64)
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
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


@pytest.mark.parametrize(
    "document",
    (
        {},
        {
            "schema": 1,
            "plugin": "goober/wall-in-one",
            "provider": "Wallhaven",
            "path": "/some/other/picture.jpg",
        },
        {
            "schema": 1,
            "plugin": "somebody-else",
            "provider": "Wallhaven",
        },
    ),
)
def test_a_forged_or_copied_sidecar_cannot_authorise_deletion(
    root: Path, document: dict[str, object]
) -> None:
    directory = managed_directory(root)
    theirs = directory / "family.jpg"
    theirs.write_bytes(b"precious")
    payload = dict(document)
    payload.setdefault("path", str(theirs))
    theirs.with_name(theirs.name + ".wallhaven.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )

    with pytest.raises(ManageError) as caught:
        manage.remove(item_for(theirs, ownership=Ownership.MANAGED), (root,))

    assert caught.value.kind == "not-ours"
    assert theirs.read_bytes() == b"precious"


def test_pairing_metadata_never_grants_download_ownership(root: Path) -> None:
    """Customising a user video must not turn the download folder marker into
    authority to unlink it. Pairing and provenance are separate facts."""
    directory = managed_directory(root, "MotionBGS")
    theirs = directory / "family-video.mp4"
    theirs.write_bytes(b"0" * 64)
    theirs.with_name(theirs.name + pairing.SIDECAR_SUFFIX).write_text(
        json.dumps({"still_path": "chosen.png"}), encoding="utf-8"
    )

    library = scan.scan((root,))
    item = next(entry for entry in library.items if entry.path == theirs)
    assert item.ownership is Ownership.USER

    with pytest.raises(ManageError) as caught:
        manage.remove(item, (root,))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


def test_a_sidecar_without_a_marker_is_refused(root: Path) -> None:
    """Ownership needs both halves; either alone is forgeable by accident."""
    directory = root / "elsewhere"
    directory.mkdir()
    path = directory / "picture.jpg"
    path.write_bytes(b"\xff\xd8\xff")
    path.with_name(path.name + ".wallhaven.json").write_text(
        provenance(path, "Wallhaven"), encoding="utf-8"
    )
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
    link.with_name(link.name + ".wallhaven.json").write_text(
        provenance(link, "Wallhaven"), encoding="utf-8"
    )
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
    video.with_name(video.name + ".motionbgs.json").write_text(
        provenance(video, "MotionBGS"), encoding="utf-8"
    )
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


def test_no_roots_given_refuses_to_delete(root: Path) -> None:
    """An absent containment boundary can never authorise an unlink."""
    item = downloaded(root)
    with pytest.raises(ManageError) as caught:
        manage.remove(item)
    assert caught.value.kind == "outside-root"
    assert item.path.is_file()


# -- the trash ------------------------------------------------------------


def test_a_users_own_file_can_be_trashed(root: Path, data_home: Path) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = manage.trash(item_for(theirs), (root,))
    assert not theirs.exists()
    assert landed.is_file()
    assert landed.parent == data_home / "Trash" / "files"


def test_the_trash_record_can_restore_it(root: Path, data_home: Path) -> None:
    """A file with no record is a file the user cannot get back."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = manage.trash(item_for(theirs), (root,))
    record = data_home / "Trash" / "info" / f"{landed.name}.trashinfo"
    text = record.read_text(encoding="utf-8")
    assert text.startswith("[Trash Info]\n")
    recorded = next(line for line in text.splitlines() if line.startswith("Path="))
    assert unquote(recorded.removeprefix("Path=")) == str(theirs.absolute())
    assert "DeletionDate=" in text


def test_trash_persists_record_destination_and_source_directories(
    root: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    synced: list[Path] = []
    monkeypatch.setattr("wall_in_one.library.manage.paths.fsync_directory", synced.append)

    manage.trash(item_for(theirs), (root,))

    assert data_home / "Trash" / "info" in synced
    assert data_home / "Trash" / "files" in synced
    assert root in synced


def test_a_path_with_a_space_is_recorded_encoded(root: Path, data_home: Path) -> None:
    """The user's own library really is under a directory with a space in it."""
    awkward = root / "holiday photo.png"
    awkward.write_bytes(b"\x89PNG\r\n\x1a\n")
    landed = manage.trash(item_for(awkward), (root,))
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
    manage.trash(item_for(first), (root,))
    landed = manage.trash(item_for(second), (root,))
    assert landed.name == "holiday (1).png"
    assert (data_home / "Trash" / "info" / "holiday (1).png.trashinfo").is_file()


def test_trashing_something_that_is_not_there_says_so(root: Path) -> None:
    absent = MediaItem(
        path=root / "absent.png",
        kind=Kind.STILL,
        size=0,
        mtime=0,
        ownership=Ownership.USER,
    )
    with pytest.raises(ManageError) as caught:
        manage.trash(absent, (root,))
    assert caught.value.kind == "missing"


def test_a_failed_move_leaves_no_orphan_record(
    root: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A record with no file is a stale entry a file manager ignores, but it
    should still not be left behind on a failure we saw happen."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")

    real_link = os.link

    def explode(
        source: str | os.PathLike[str],
        target: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
        follow_symlinks: bool = True,
    ) -> None:
        if Path(source) == theirs:
            raise OSError(18, "Invalid cross-device link")
        real_link(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            follow_symlinks=follow_symlinks,
        )

    monkeypatch.setattr("os.link", explode)
    with pytest.raises(ManageError) as caught:
        manage.trash(item_for(theirs), (root,))
    assert caught.value.kind == "cross-device"
    assert theirs.is_file()
    assert list((data_home / "Trash" / "info").iterdir()) == []


def test_concurrent_same_name_trash_never_replaces_an_earlier_file(
    root: Path, data_home: Path
) -> None:
    sources: list[MediaItem] = []
    expected: set[bytes] = set()
    for index in range(16):
        path = root / f"source-{index}" / "same.png"
        path.parent.mkdir()
        payload = f"unique-{index}".encode()
        path.write_bytes(payload)
        sources.append(item_for(path))
        expected.add(payload)

    with ThreadPoolExecutor(max_workers=8) as pool:
        destinations = tuple(pool.map(lambda item: manage.trash(item, (root,)), sources))

    assert len(set(destinations)) == len(sources)
    assert {path.read_bytes() for path in destinations} == expected
    records = data_home / "Trash" / "info"
    assert {path.name for path in records.iterdir()} == {
        f"{path.name}.trashinfo" for path in destinations
    }


# -- somebody else's collection -------------------------------------------
#
# The case that cost a 129 MB file. A Wallpaper Engine wallpaper is scanned
# into the library and is `Ownership.USER`, so `remove` correctly refuses to
# delete it -- and then the caller fell through to `trash`, which moved it.
# "Not ours to delete" and "not ours to move" are different claims.


def test_workshop_media_is_not_offered_or_trashed_even_inside_a_root(root: Path) -> None:
    steam = root / "Steam" / "workshop" / "431960" / "12345"
    steam.mkdir(parents=True)
    wallpaper = steam / "someone elses.mp4"
    wallpaper.write_bytes(b"0" * 64)
    workshop = item_for(wallpaper, Kind.VIDEO, provider="Wallpaper Engine")

    assert not manage.is_removable(workshop, (root,))

    with pytest.raises(ManageError) as caught:
        manage.trash(workshop, (root,))

    assert caught.value.kind == "not-ours"
    assert wallpaper.is_file()


def test_workshop_identity_overrides_managed_markers(root: Path) -> None:
    """Even accidental provider sidecars may not turn Steam media deletable."""
    directory = managed_directory(root, "Steam-shaped")
    wallpaper = directory / "scene-video.mp4"
    wallpaper.write_bytes(b"0" * 64)
    wallpaper.with_name(wallpaper.name + ".motionbgs.json").write_text(
        provenance(wallpaper, "MotionBGS"), encoding="utf-8"
    )
    workshop = item_for(
        wallpaper,
        Kind.VIDEO,
        Ownership.MANAGED,
        provider="Wallpaper Engine",
    )

    with pytest.raises(ManageError) as caught:
        manage.remove(workshop, (root,))
    assert caught.value.kind == "not-ours"
    assert wallpaper.is_file()


def test_a_file_inside_the_roots_is_still_trashed(root: Path) -> None:
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    assert manage.trash(item_for(theirs), (root,)).is_file()
    assert not theirs.exists()


def test_with_no_roots_given_trash_fails_closed(root: Path) -> None:
    """Without a configured boundary, no path is safe to offer or move."""
    theirs = root / "holiday.png"
    theirs.write_bytes(b"\x89PNG\r\n\x1a\n")
    with pytest.raises(ManageError) as caught:
        manage.trash(item_for(theirs))
    assert caught.value.kind == "not-ours"
    assert theirs.is_file()


def test_removability_is_about_the_roots_not_the_ownership(root: Path, tmp_path: Path) -> None:
    inside = root / "a.png"
    inside.write_bytes(b"\x89PNG\r\n\x1a\n")
    outside = tmp_path / "elsewhere" / "b.png"
    outside.parent.mkdir()
    outside.write_bytes(b"\x89PNG\r\n\x1a\n")

    assert manage.is_removable(item_for(inside), (root,))
    assert not manage.is_removable(item_for(inside, provider="external"), (root,))
    assert not manage.is_removable(item_for(outside), (root,))
    assert not manage.is_removable(item_for(inside))
