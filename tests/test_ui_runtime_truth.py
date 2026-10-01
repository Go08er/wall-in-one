"""Offline checks for the GUI's view of one atomic runtime status."""

import datetime
from dataclasses import replace
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
        "entry_taboo": False,
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


def test_explicit_current_taboo_truth_survives_a_capped_diagnostic_inventory() -> None:
    status: dict[str, object] = {
        "entry_taboo": True,
        "renderer_failed": False,
        "playlist_id": "old",
        "entry_id": "omitted",
        "taboo_entries": [],
        "taboo_entries_omitted": 99,
        "displays": [],
    }

    assert runtime_truth.current_renderer_failure_is_taboo(status)

    status["entry_taboo"] = False
    status["displays"] = [
        {
            "connected": True,
            "entry_taboo": True,
            "renderer_failed": False,
            "playlist_id": "same-media",
            "entry_id": "other-occurrence",
        }
    ]
    assert runtime_truth.current_renderer_failure_is_taboo(status)


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


def test_runtime_overrides_status_fields_are_additive_for_this_parser() -> None:
    """A display whose own playlist beats a global rule still reads as today.

    A service that applies ``runtime-overrides.toml`` adds per-row
    ``beats_global_rules`` and ``cycle_interval_seconds``, a top-level
    ``cycle_interval_seconds`` that is ``null`` when routes disagree,
    ``supported_override_schemas`` and ``loaded_overrides_sha256``.
    ``route_source`` keeps its four values, so this parser (and an older
    GUI's) neither rejects nor needs them.
    """
    own = _display_status("DP-1", "evening", "Evening", route_source="assignment", manual=False)
    own.update(shuffle=True, shuffle_default=True)
    ruled = _display_status(
        "HDMI-A-1", "night", "Night", route_source="schedule", manual=False, rule_id="global"
    )

    def status(*rows: dict[str, object], **top: object) -> dict[str, object]:
        return {
            "status_version": 2,
            "display_mode": "independent",
            "theme_source": {"configured": "DP-1", "effective": "DP-1", "fallback": False},
            "playlist_id": "",
            "playlist": "Multiple displays",
            "source": "schedule",
            "displays": list(rows),
            **top,
        }

    before = runtime_truth.from_status(status(own, ruled))
    truth = runtime_truth.from_status(
        status(
            {**own, "beats_global_rules": True, "cycle_interval_seconds": 120},
            {**ruled, "beats_global_rules": False, "cycle_interval_seconds": 900},
            supported_config_schemas=[4, 5],
            supported_override_schemas=[1],
            loaded_overrides_sha256="0" * 64,
            cycle_interval_seconds=None,
        )
    )

    assert truth is not None
    assert truth == before
    beating = truth.display("DP-1")
    assert beating is not None
    assert (beating.route_source, beating.schedule_rule_id) == ("assignment", None)
    assert (beating.shuffle, beating.shuffle_default, beating.shuffle_source) == (
        True,
        True,
        "config",
    )
    following = truth.display("HDMI-A-1")
    assert following is not None and following.schedule_rule_id == "global"
    assert truth.active_playlist_ids == ("evening", "night")


def _timing_status(*rows: dict[str, object]) -> dict[str, object]:
    return {
        "status_version": 2,
        "display_mode": "independent",
        "theme_source": {"configured": "DP-1", "effective": "DP-1", "fallback": False},
        "playlist_id": "",
        "playlist": "Multiple displays",
        "source": "mixed",
        "displays": list(rows),
    }


#: The timing fields a 0.2.0 service appends to every display row.
_TIMING = {
    "route_change_at": "2026-08-03T18:00:00",
    "route_change_in_s": 3600,
    "next_cycle_at": "2026-08-03T17:12:00",
    "next_cycle_in_s": 720,
    "until": "18:00",
    "next_change_in_s": 720,
}


def test_timing_status_fields_are_read_and_their_absence_is_tolerated() -> None:
    ruled = _display_status(
        "DP-1", "frog-day", "Frog day", route_source="schedule", manual=False, rule_id="day"
    )
    picked = _display_status("HDMI-A-1", "night", "Night", route_source="manual", manual=True)
    nothing = {key: None for key in _TIMING}

    older = runtime_truth.from_status(_timing_status(ruled, picked))
    truth = runtime_truth.from_status(
        _timing_status(
            {**ruled, **_TIMING},
            {
                **picked,
                **nothing,
                "next_cycle_in_s": 60,
                "next_cycle_at": "2026-08-03T17:01:00",
                "next_change_in_s": 60,
            },
        )
    )

    assert older is not None and truth is not None
    for display in older.displays:
        assert (display.route_change_at, display.route_change_in_s) == (None, None)
        assert (display.next_cycle_at, display.next_cycle_in_s, display.until) == (
            None,
            None,
            None,
        )
    timed = truth.display("DP-1")
    assert timed is not None
    assert timed.route_change_at == datetime.datetime(2026, 8, 3, 18, 0)
    assert timed.route_change_in_s == 3600
    assert timed.next_cycle_at == datetime.datetime(2026, 8, 3, 17, 12)
    assert timed.next_cycle_in_s == 720
    assert timed.until == "18:00"
    held = truth.display("HDMI-A-1")
    assert held is not None
    assert (held.route_change_at, held.until, held.next_cycle_in_s) == (None, None, 60)
    # Everything else reads exactly as from an older service.
    assert replace(truth, displays=tuple(_untimed(d) for d in truth.displays)) == older


def _untimed(display: runtime_truth.DisplayRuntimeTruth) -> runtime_truth.DisplayRuntimeTruth:
    return replace(
        display,
        route_change_at=None,
        route_change_in_s=None,
        next_cycle_at=None,
        next_cycle_in_s=None,
        until=None,
    )


def test_malformed_timing_fields_are_dropped_without_rejecting_the_snapshot() -> None:
    row = _display_status("DP-1", "day", "Day", route_source="schedule", manual=False)
    for broken in (
        {"route_change_at": "18:00", "until": "18:00"},
        {"route_change_in_s": -5},
        {"route_change_in_s": True},
        {"route_change_at": "2026-08-03T18:00:00+02:00"},
        {"next_cycle_in_s": "720"},
        {"next_cycle_at": 1, "until": "25:00"},
    ):
        truth = runtime_truth.from_status(_timing_status({**row, **_TIMING, **broken}))
        assert truth is not None, broken
        display = truth.display("DP-1")
        assert display is not None
        if set(broken) & {"route_change_at", "route_change_in_s"}:
            assert (display.route_change_at, display.route_change_in_s) == (None, None)
            assert display.until is None
        if set(broken) & {"next_cycle_at", "next_cycle_in_s"}:
            assert (display.next_cycle_at, display.next_cycle_in_s) == (None, None)
        if broken.get("until") == "25:00":
            assert display.until is None


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
