"""After Codex 0.3.0 round 1, the last gaps, without a display.

Library folders and Wallpaper Engine scanning decide what is in the library,
so changing them can take away what a playlist plays. Since round 3 the
change is checked by doing it for pretend, with the real scan of the
sources asked for (`dry_run.sources`), and refused with the same rule as
removing what it takes away. (Round 1c predicted what the scan would find
from memory, `scan.would_find`; round 3 showed that prediction can't keep
up with the scanner, and the dry run replaced it. That the dry run is what
the scan really finds is ``test_dry_run``.) The windows' cases are in
``test_ui_codex030_round1c``; each fails on b478b0a, the merge this was
made on.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from wall_in_one import config, dry_run, runtime_config
from wall_in_one.library import scan, workshop
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.session import Session


def _file(path: Path, body: bytes = b"placeholder bytes") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class Library(NamedTuple):
    walls: Path
    walls2: Path
    dawn: Path
    rain: Path
    clip: Path
    loop: Path


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Library:
    """Two folders whose names are prefixes of one another, and a Steam library."""
    w = tmp_path / "w"
    steam = tmp_path / "steam"
    content = steam / "steamapps" / "workshop" / "content" / workshop.APP_ID / "123"
    content.mkdir(parents=True)
    (content / "project.json").write_text(
        json.dumps({"type": "video", "title": "Loop", "file": "loop.mp4"}), encoding="utf-8"
    )
    monkeypatch.setattr(workshop, "steam_roots", lambda *_a, **_k: (steam,))
    return Library(
        walls=w / "walls",
        walls2=w / "walls2",
        dawn=_file(w / "walls" / "dawn.png"),
        rain=_file(w / "walls2" / "rain.png"),
        clip=_file(w / "walls" / "clip.mp4"),  # no still: the compiler drops it
        loop=_file(content / "loop.mp4", b"\x00" * 16),
    )


@pytest.fixture
def session(library: Library) -> Iterator[Session]:
    made = Session(
        config.Settings(roots=(library.walls, library.walls2), scan_workshop=True).validated(),
        owns_playback=False,
    )
    made.refresh(mutate_removals=False)
    try:
        yield made
    finally:
        made.shutdown()


def _playlist(session: Session, name: str, sources: list[Path]) -> str:
    made = session.playlists.create(name)
    for source in sources:
        session.playlists.add(made.id, source)
    return made.id


def _refusals(session: Session, **changes: Any) -> tuple[str, ...]:
    return dry_run.sources(
        session.settings,
        replace(session.settings, **changes).validated(),
        library=session.library,
        pairing_store=session.pairings,
        playlist_store=session.playlists,
        favourite_store=session.favourites,
        uses=dry_run.Uses.of(session),
    ).refusals


def test_a_folder_change_is_refused_when_a_used_playlist_loses_what_it_plays(
    session: Session, library: Library
) -> None:
    evening = _playlist(session, "Evening", [library.rain])
    session.schedules.add(evening)

    assert _refusals(session, roots=(library.walls,)) == (
        "\u201cEvening\u201d (used by 1 schedule rule) would have nothing left to play: "
        "rain.png would no longer be in the library",
    )
    # Removing the other folder takes nothing it needs: /w/walls is not /w/walls2.
    assert _refusals(session, roots=(library.walls2,)) == ()
    # Nor does a change replacing both folders by the one above them.
    assert _refusals(session, roots=(library.walls.parent,)) == ()


def test_a_folder_change_is_refused_when_any_playlist_would_hold_only_what_cant_play(
    session: Session, library: Library
) -> None:
    _playlist(session, "Mixed", [library.rain, library.clip])  # nothing uses it

    (refusal,) = _refusals(session, roots=(library.walls,))
    assert refusal.startswith(
        "\u201cMixed\u201d would hold only wallpapers playback can\u2019t use: "
        "rain.png would no longer be in the library"
    )


def test_turning_off_workshop_scanning_is_refused_when_a_used_playlist_needs_it(
    session: Session, library: Library
) -> None:
    loop = session.library.find(library.loop)
    assert loop is not None and loop.provider == scan.WORKSHOP_PROVIDER
    session.pairings.choose_still(MediaItem(library.loop, Kind.VIDEO, 1, 1), library.dawn)
    session.refresh(mutate_removals=False)  # so the loop plays
    scenes = _playlist(session, "Scenes", [library.loop])
    session.update_settings(
        replace(session.settings, active_playlist=scenes).validated(), rescan_library=False
    )
    assert runtime_config.use_refusal(scenes, session) == ""

    assert _refusals(session, scan_workshop=False) == (
        "\u201cScenes\u201d (used by the default) would have nothing left to play: "
        "loop.mp4 would no longer be in the library",
    )
    # Asked together with a new default elsewhere, the uses are counted as they would be:
    # unused then, but its entries stay (a source out of reach isn't a deletion), naming
    # nothing the compiler can use, which would stop every publication.
    assert _refusals(session, scan_workshop=False, active_playlist="") == (
        "\u201cScenes\u201d would hold only wallpapers playback can\u2019t use: "
        "loop.mp4 would no longer be in the library",
    )
