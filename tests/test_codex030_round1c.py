"""After Codex 0.3.0 round 1, the last gaps, without a display.

Library folders and Wallpaper Engine scanning decide what is in the library,
so changing them can take away what a playlist plays. Which wallpapers a
change takes away is worked out from the accepted library, by the scan's own
rules (`scan.would_find`), and refused with the same rule as removing them
(`runtime_config.settings_refusal`). The windows' cases are in
``test_ui_codex030_round1c``; each fails on b478b0a, the merge this was made on.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, playlists, scan, schedules
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.session import Session


def _item(path: Path, kind: Kind = Kind.STILL, provider: str = "local") -> MediaItem:
    return MediaItem(path, kind, 5, 1, provider=provider)


# -- what a scan of other sources would find again ------------------------------------------


def test_a_root_holds_what_is_under_it_by_whole_components() -> None:
    walls = Path("/home/u/Pictures/walls")
    inside = _item(walls / "dawn.png")
    sibling = _item(Path("/home/u/Pictures/walls2/dawn.png"))
    assert scan.would_find(inside, (walls,), False)
    assert not scan.would_find(sibling, (walls,), False), "/walls is not a prefix of /walls2"
    assert scan.would_find(sibling, (walls, Path("/home/u/Pictures/walls2")), False)
    assert scan.would_find(inside, (Path("/home/u/Pictures"),), False), "a root above holds it"
    assert not scan.would_find(inside, (), False), "no roots, no library"


def test_what_the_walk_never_reaches_is_not_found() -> None:
    root = Path("/w")
    assert not scan.would_find(_item(root / ".hidden" / "a.png"), (root,), False)
    assert not scan.would_find(_item(root / ".a.png"), (root,), False)
    deep = root.joinpath(*(f"d{n}" for n in range(scan.MAX_DEPTH)), "a.png")
    deeper = root.joinpath(*(f"d{n}" for n in range(scan.MAX_DEPTH + 1)), "a.png")
    assert scan.would_find(_item(deep), (root,), False)
    assert not scan.would_find(_item(deeper), (root,), False)
    assert not scan.would_find(_item(root), (root,), False), "a root is not a wallpaper"


def test_workshop_items_come_back_only_from_the_workshop_scan() -> None:
    steam = Path("/steam/steamapps/workshop/content/431960/123")
    video = _item(steam / "clip.mp4", Kind.VIDEO, scan.WORKSHOP_PROVIDER)
    scene = _item(steam, Kind.SCENE, scan.WORKSHOP_PROVIDER)
    root = Path("/w")
    assert scan.would_find(video, (root,), True) and scan.would_find(scene, (root,), True)
    assert not scan.would_find(video, (root,), False)
    assert not scan.would_find(scene, (root,), False)
    # A root over the Steam folder finds the video as a file; a scene never is one.
    assert scan.would_find(video, (Path("/steam"),), False)
    assert not scan.would_find(scene, (Path("/steam"),), False)


# -- the refusal ---------------------------------------------------------------------------


@pytest.fixture
def library() -> tuple[MediaItem, ...]:
    return (
        _item(Path("/w/walls/dawn.png")),
        _item(Path("/w/walls2/rain.png")),
        _item(Path("/w/walls/clip.mp4"), Kind.VIDEO),  # no still: the compiler drops it
        _item(Path("/steam/123/loop.mp4"), Kind.VIDEO, scan.WORKSHOP_PROVIDER),
    )


@pytest.fixture
def session(tmp_path: Path, library: tuple[MediaItem, ...]) -> Iterator[Session]:
    roots = (Path("/w/walls"), Path("/w/walls2"))
    made = Session(
        config.Settings(roots=roots, scan_workshop=True).validated(),
        scanner=lambda _roots: Library(roots=roots, items=library),
        playlist_store=playlists.Store(path=tmp_path / "playlists.json"),
        schedule_store=schedules.Store(path=tmp_path / "schedules.json"),
        display_store=displays.Store(path=tmp_path / "displays.json"),
    )
    made.refresh()
    try:
        yield made
    finally:
        made.shutdown()


def _playlist(session: Session, name: str, items: list[MediaItem]) -> playlists.Playlist:
    made = session.playlists.create(name)
    for item in items:
        session.playlists.add(made.id, item.path)
    return session.playlists.find(made.id)


def test_a_folder_change_is_refused_when_a_used_playlist_loses_what_it_plays(
    session: Session, library: tuple[MediaItem, ...]
) -> None:
    dawn, rain, _clip, _loop = library
    evening = _playlist(session, "Evening", [rain])
    session.schedules.add(evening.id)

    refusal = runtime_config.settings_refusal({"roots": (Path("/w/walls"),)}, session)
    assert refusal == (
        "that change to the library folders would take wallpapers out of the library: "
        "\u201cEvening\u201d (used by 1 schedule rule) would have nothing left to play"
    )
    # Removing the other folder takes nothing it needs: /w/walls is not /w/walls2.
    assert runtime_config.settings_refusal({"roots": (Path("/w/walls2"),)}, session) == ""
    # Nor does a change that keeps it, nor one replacing a folder by one above it.
    assert runtime_config.settings_refusal({"roots": (Path("/w"),)}, session) == ""
    assert dawn.path.is_relative_to("/w")


def test_a_folder_change_is_refused_when_any_playlist_would_hold_only_what_cant_play(
    session: Session, library: tuple[MediaItem, ...]
) -> None:
    _dawn, rain, clip, _loop = library
    _playlist(session, "Mixed", [rain, clip])  # nothing uses it

    refusal = runtime_config.settings_refusal({"roots": (Path("/w/walls"),)}, session)
    assert "\u201cMixed\u201d would hold only wallpapers playback can\u2019t use" in refusal


def test_turning_off_workshop_scanning_is_refused_when_a_used_playlist_needs_it(
    session: Session, library: tuple[MediaItem, ...]
) -> None:
    *_rest, loop = library
    session.pairings.choose_still(loop, library[0].path)  # so the loop plays
    scenes = _playlist(session, "Scenes", [loop])
    session.update_settings(replace(session.settings, active_playlist=scenes.id).validated())
    assert runtime_config.use_refusal(scenes.id, session) == ""

    refusal = runtime_config.settings_refusal({"scan_workshop": False}, session)
    assert refusal.startswith("turning off Wallpaper Engine scanning would take wallpapers")
    assert "\u201cScenes\u201d (used by the default) would have nothing left to play" in refusal
    # Asked together with a new default elsewhere, the uses are counted as they would be.
    assert (
        runtime_config.settings_refusal({"scan_workshop": False, "active_playlist": ""}, session)
        == ""
    )
