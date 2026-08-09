from __future__ import annotations

import tomllib
from pathlib import Path

from wall_in_one import config, runtime_config
from wall_in_one.library import displays, pairings, playlists, schedules
from wall_in_one.library.model import Kind, Library, MediaItem
from wall_in_one.session import Session


def _session(tmp_path: Path) -> tuple[config.Settings, Session]:
    still = tmp_path / "still.png"
    video = tmp_path / "video.mp4"
    representative = tmp_path / "video-still.png"
    for path in (still, video, representative):
        path.write_bytes(b"fixture")
    items = (
        MediaItem(still, Kind.STILL, 7, 1),
        MediaItem(video, Kind.VIDEO, 7, 1, paired_still=representative),
    )
    named = playlists.Playlist(
        id="evening",
        name="Evening",
        entries=(playlists.Entry(id="entry-video", source=str(video)),),
    )
    palette = pairings.Pairing(
        identity=pairings.Identity.of(items[1]),
        still=representative,
        palette=pairings.PalettePolicy(
            kind="community", name="Catppuccin", mode=pairings.Mode.DARK
        ),
        customized=True,
    )
    settings = config.Settings(
        roots=(tmp_path,), active_playlist="evening", cycle_enabled=True, shuffle=True
    )
    session = Session(
        settings,
        scanner=lambda _roots: Library(roots=(tmp_path,), items=items),
        pairing_store=pairings.Store({palette.identity.key: palette}),
        playlist_store=playlists.Store({named.id: named}),
        schedule_store=schedules.Store(
            (
                schedules.Rule(
                    id="night",
                    playlist="evening",
                    weekdays=frozenset({4, 5}),
                    start=22 * 60,
                    end=6 * 60,
                ),
            )
        ),
        display_store=displays.Store({"DP-1": "evening"}),
    )
    session.refresh()
    return settings, session


def test_compiler_resolves_authoring_identity_away(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    document = tomllib.loads(runtime_config.render(settings, session))
    assert document["schema_version"] == 1
    assert document["default_playlist"] == "evening"
    assert [one["id"] for one in document["playlists"]] == ["all-media", "evening"]
    entry = document["playlists"][1]["entries"][0]
    assert entry == {
        "id": "entry-video",
        "kind": "video",
        "still": str(tmp_path / "video-still.png"),
        "motion": str(tmp_path / "video.mp4"),
        "palette": {
            "kind": "named",
            "source": "community",
            "name": "Catppuccin",
            "mode": "dark",
        },
    }
    text = runtime_config.render(settings, session)
    assert "medium:source" not in text
    assert "pairings.json" not in text
    assert document["schedules"][0]["weekdays"] == [4, 5]
    assert document["displays"] == [{"connector": "DP-1", "playlist": "evening"}]


def test_compiler_write_is_atomic_and_leaves_no_temporary(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    target = tmp_path / "state" / "runtime.toml"
    assert runtime_config.write(settings, session, target) == target
    assert tomllib.loads(target.read_text())["schema_version"] == 1
    assert list(target.parent.glob(".*.tmp")) == []


def test_unresolved_playlist_entries_are_omitted_not_looked_up(tmp_path: Path) -> None:
    settings, session = _session(tmp_path)
    session.playlists.add("evening", tmp_path / "not-in-library.mp4", entry_id="missing")
    document = tomllib.loads(runtime_config.render(settings, session))
    entries = document["playlists"][1]["entries"]
    assert [entry["id"] for entry in entries] == ["entry-video"]
