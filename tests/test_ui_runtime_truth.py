"""Offline checks for the GUI's view of one atomic runtime status."""

from pathlib import Path

from wall_in_one import runtime_config
from wall_in_one.library.model import Kind, MediaItem
from wall_in_one.library.playlists import Entry, Playlist
from wall_in_one.ui import runtime_truth


def _item(path: str, *, still: str | None = None) -> MediaItem:
    return MediaItem(
        path=Path(path),
        kind=Kind.VIDEO if path.endswith(".mp4") else Kind.STILL,
        size=1,
        mtime=1,
        paired_still=Path(still) if still is not None else None,
    )


def test_manual_bar_override_is_read_from_runtime_snapshot() -> None:
    truth = runtime_truth.from_status(
        {
            "playlist_id": "night",
            "playlist": "Night",
            "source": "manual",
            "schedule": {
                "following": False,
                "playlist_id": "day",
                "playlist": "Day",
                "rule_id": "work-hours",
            },
        }
    )

    assert truth is not None
    assert truth.playlist_id == "night"
    assert truth.is_manual
    assert not truth.follows_schedule
    assert truth.scheduled_playlist_id == "day"
    assert truth.schedule_rule_id == "work-hours"
    assert truth.active_playlist_ids == ("night",)


def test_multi_display_snapshot_keeps_every_active_playlist() -> None:
    truth = runtime_truth.from_status(
        {
            "playlist_id": "",
            "playlist": "Multiple displays",
            "source": "schedule",
            "playlists": [
                {"id": "day", "name": "Day", "entries": 2, "active": True},
                {"id": "night", "name": "Night", "entries": 3, "active": True},
                {"id": "spare", "name": "Spare", "entries": 1, "active": False},
            ],
            "schedule": {
                "following": True,
                "playlist_id": "day",
                "playlist": "Day",
                "rule_id": None,
            },
        }
    )

    assert truth is not None
    assert truth.is_multi_display
    assert truth.active_playlist_ids == ("day", "night")
    assert truth.playlist_is_active("day")
    assert truth.playlist_is_active("night")
    assert not truth.playlist_is_active("spare")


def test_incomplete_status_cannot_displace_authoring_fallback() -> None:
    assert runtime_truth.from_status(None) is None
    assert runtime_truth.from_status({"playlist": "Night", "source": "manual"}) is None
    assert (
        runtime_truth.from_status(
            {
                "playlist_id": "",
                "playlist": "Multiple displays",
                "source": "schedule",
                "playlists": [{"id": "day", "active": True}],
            }
        )
        is None
    )


def test_media_source_comes_from_runtime_entry_not_an_unrelated_local_cursor() -> None:
    first = _item("/library/first.png")
    second = _item("/library/second.png")
    evening = Playlist(
        id="evening",
        name="Evening",
        entries=(Entry(id="second-entry", source=str(second.path)),),
    )

    playback = runtime_truth.media_playback(
        {
            "playlist_id": "evening",
            "playlist": "Evening",
            "source": "manual",
            "entry_id": "second-entry",
            "still": str(second.path),
        },
        (evening,),
        (first, second),
    )

    assert playback == runtime_truth.MediaPlayback("Evening", (second.path,))


def test_media_source_resolves_every_display_including_generated_all_media() -> None:
    first = _item("/library/first.png")
    second = _item("/library/second.png")
    evening = Playlist(
        id="evening",
        name="Evening",
        entries=(Entry(id="first-entry", source=str(first.path)),),
    )

    playback = runtime_truth.media_playback(
        {
            "playlist_id": "",
            "playlist": "Multiple displays",
            "source": "schedule",
            "playlists": [
                {"id": "evening", "active": True},
                {"id": runtime_config.FALLBACK_PLAYLIST_ID, "active": True},
            ],
            "displays": [
                {
                    "connector": "DP-1",
                    "playlist_id": "evening",
                    "entry_id": "first-entry",
                    "still": str(first.path),
                },
                {
                    "connector": "DP-2",
                    "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                    "entry_id": runtime_config.entry_id_for_source(second.path),
                    "still": str(second.path),
                },
            ],
        },
        (evening,),
        (first, second),
    )

    assert playback == runtime_truth.MediaPlayback(
        "Multiple displays",
        (first.path, second.path),
    )


def test_still_fallback_is_used_only_when_it_identifies_one_source() -> None:
    shared = "/library/representative.png"
    first = _item("/library/first.mp4", still=shared)
    unique = _item("/library/unique.mp4", still="/library/unique-still.png")
    base = {
        "playlist_id": "old-playlist",
        "playlist": "Old playlist",
        "source": "manual",
        "entry_id": "entry-no-longer-authored",
    }

    resolved = runtime_truth.media_playback(
        {**base, "still": "/library/unique-still.png"}, (), (first, unique)
    )
    ambiguous = runtime_truth.media_playback(
        {**base, "still": shared},
        (),
        (first, _item("/library/second.mp4", still=shared)),
    )

    assert resolved == runtime_truth.MediaPlayback("Old playlist", (unique.path,))
    assert ambiguous == runtime_truth.MediaPlayback("Old playlist", ())
