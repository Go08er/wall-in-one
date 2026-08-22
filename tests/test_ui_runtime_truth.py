"""Offline checks for the GUI's view of one atomic runtime status."""

from pathlib import Path

from wall_in_one import runtime_config, runtime_health
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


def _display_status(
    connector: str,
    playlist_id: str,
    playlist: str,
    *,
    route_source: str,
    manual: bool,
    rule_id: str | None = None,
    connected: bool = True,
) -> dict[str, object]:
    return {
        "connector": connector,
        "connected": connected,
        "assignment_source": "explicit",
        "assigned_playlist_id": playlist_id,
        "assigned_playlist": playlist,
        "playlist_id": playlist_id,
        "playlist": playlist,
        "entry_id": f"entry-{connector}",
        "kind": "still",
        "still": f"/library/{connector}.png",
        "motion_active": False,
        "route_source": route_source,
        "manual_override": manual,
        "schedule_rule_id": rule_id,
        "playback_state": "playing",
        "paused": False,
        "stopped": False,
        "shuffle": False,
        "shuffle_default": False,
        "shuffle_source": "config",
        "cycle_enabled": True,
        "cycle_default": True,
        "cycle_source": "config",
        "renderer_failed": False,
        "last_error": "",
        "automatic_retry": None,
    }


def test_status_v2_preserves_mixed_per_display_truth_and_palette_fallback() -> None:
    truth = runtime_truth.from_status(
        {
            "status_version": 2,
            "display_mode": "independent",
            "theme_source": {
                "configured": "DP-9",
                "effective": "eDP-1",
                "fallback": True,
            },
            "playlist_id": "",
            "playlist": "Multiple displays",
            "source": "mixed",
            "playlists": [
                {"id": "day", "active": True},
                {"id": "night", "active": True},
            ],
            "schedules": [
                {
                    "id": "work-hours",
                    "playlist_id": "day",
                    "playlist": "Day",
                    "connector": "eDP-1",
                    "months": [],
                    "weekdays": [0, 1, 2, 3, 4],
                    "start": "09:00",
                    "end": "17:00",
                    "enabled": True,
                    "selected": True,
                    "in_force": True,
                },
                {
                    "id": "global-night",
                    "playlist_id": "night",
                    "playlist": "Night",
                    "connector": None,
                    "months": [],
                    "weekdays": [],
                    "start": None,
                    "end": None,
                    "enabled": True,
                    "selected": False,
                    "in_force": False,
                },
            ],
            "displays": [
                _display_status(
                    "eDP-1",
                    "day",
                    "Day",
                    route_source="schedule",
                    manual=False,
                    rule_id="work-hours",
                ),
                _display_status("DP-2", "night", "Night", route_source="manual", manual=True),
            ],
        }
    )

    assert truth is not None
    assert truth.status_version == 2
    assert truth.source == "mixed"
    assert truth.is_multi_display
    assert truth.active_playlist_ids == ("day", "night")
    laptop = truth.display("eDP-1")
    external = truth.display("DP-2")
    assert laptop is not None and laptop.schedule_rule_id == "work-hours"
    assert external is not None and external.manual_override is True
    assert truth.theme_source == runtime_truth.ThemeSourceTruth("DP-9", "eDP-1", True)
    assert truth.schedule_rule("work-hours") == runtime_truth.RuntimeScheduleTruth(
        "work-hours",
        "day",
        "Day",
        "eDP-1",
        True,
        True,
        True,
    )
    global_rule = truth.schedule_rule("global-night")
    assert global_rule is not None and global_rule.connector is None


def test_detached_route_is_inventory_not_current_playback() -> None:
    first = _item("/library/first.png")
    second = _item("/library/second.png")
    day = Playlist(
        id="day",
        name="Day",
        entries=(Entry(id="day-entry", source=str(first.path)),),
    )
    night = Playlist(
        id="night",
        name="Night",
        entries=(Entry(id="night-entry", source=str(second.path)),),
    )
    live = _display_status("eDP-1", "day", "Day", route_source="schedule", manual=False)
    live.update({"entry_id": "day-entry", "still": str(first.path)})
    detached = _display_status(
        "DP-9",
        "night",
        "Night",
        route_source="manual",
        manual=True,
        connected=False,
    )
    detached.update({"entry_id": "night-entry", "still": str(second.path)})
    status: dict[str, object] = {
        "status_version": 2,
        "display_mode": "independent",
        "theme_source": {
            "configured": "DP-9",
            "effective": "eDP-1",
            "fallback": True,
        },
        "playlist_id": "",
        "playlist": "Multiple displays",
        "source": "mixed",
        # Inventory flags retain both route states; connector.connected is the
        # authority for what is actually playing now.
        "playlists": [
            {"id": "day", "active": True},
            {"id": "night", "active": True},
        ],
        "displays": [live, detached],
    }

    truth = runtime_truth.from_status(status)
    playback = runtime_truth.media_playback(status, (day, night), (first, second))

    assert truth is not None
    assert truth.active_playlist_ids == ("day",)
    assert not truth.is_multi_display
    assert truth.display("DP-9") is not None, "Schedules retains detached routes"
    assert playback == runtime_truth.MediaPlayback("Day", (first.path,))


def test_incomplete_status_v2_never_falls_back_to_stale_session_shape() -> None:
    display = _display_status("DP-1", "day", "Day", route_source="schedule", manual=False)
    del display["route_source"]

    assert (
        runtime_truth.from_status(
            {
                "status_version": 2,
                "display_mode": "independent",
                "theme_source": {"configured": "DP-1", "effective": "DP-1", "fallback": False},
                "playlist_id": "day",
                "playlist": "Day",
                "source": "schedule",
                "displays": [display],
            }
        )
        is None
    )


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


def test_taboo_inventory_maps_stable_entries_to_media_and_deduplicates_occurrences() -> None:
    first = _item("/library/first.png")
    second = _item("/library/second.png")
    playlist = Playlist(
        id="evening",
        name="Evening",
        entries=(
            Entry(id="first-a", source=str(first.path)),
            Entry(id="first-b", source=str(first.path)),
        ),
    )
    inventory = runtime_health.taboo_inventory(
        {
            "config_generation": "a" * runtime_config.CONFIG_GENERATION_HEX_CHARS,
            "runtime_instance": "b" * runtime_health.RUNTIME_INSTANCE_HEX_CHARS,
            "config_epoch": 7,
            "taboo_entries_omitted": 7,
            "taboo_entries": [
                {
                    "playlist_id": "evening",
                    "entry_id": "first-a",
                    "reason": "decoder rejected it",
                    "source": "automatic-apply",
                    "durable": True,
                    "observed_config_epoch": 7,
                },
                {
                    "playlist_id": "evening",
                    "entry_id": "first-b",
                    "reason": "same wallpaper, another occurrence",
                    "source": "automatic-apply",
                    "durable": False,
                    "observed_config_epoch": 7,
                },
                {
                    "playlist_id": runtime_config.FALLBACK_PLAYLIST_ID,
                    "entry_id": runtime_config.entry_id_for_source(second.path),
                    "reason": "scene crashed",
                    "source": "renderer-crash",
                    "durable": True,
                    "observed_config_epoch": 7,
                },
            ],
        },
        (playlist,),
        (first, second),
    )

    assert inventory.omitted == 7
    assert inventory.unmapped == 0
    assert inventory.stale == 0
    assert [(report.item.path, report.reason) for report in inventory.reports] == [
        (first.path, "decoder rejected it"),
        (second.path, "scene crashed"),
    ]
    assert [report.durable for report in inventory.reports] == [False, True]


def test_missing_taboo_inventory_never_means_that_saved_health_recovered() -> None:
    # The parser reports only observations. Clearing is intentionally absent
    # from this API because a capped or delayed status reply cannot prove it.
    assert runtime_health.taboo_inventory({}, (), ()).reports == ()
    assert runtime_health.taboo_inventory(None, (), ()).reports == ()
