"""The session log: bounded, in memory, said once per change, paths hidden on request."""

from __future__ import annotations

import datetime
from pathlib import Path

from tests.test_next_status_line import BATTERY, two_display_status
from wall_in_one import paths
from wall_in_one.ui.activity_log import HIDDEN, MAX_TEXT, ActivityLog, as_text, hide_paths
from wall_in_one.ui.status_model import RuntimeStatusModel


def _clock() -> datetime.datetime:
    return datetime.datetime(2026, 10, 4, 9, 30, 0)


def _following() -> tuple[ActivityLog, RuntimeStatusModel]:
    log = ActivityLog(clock=_clock)
    model = RuntimeStatusModel()
    model.subscribe(log.observe)
    return log, model


def _said(log: ActivityLog) -> list[tuple[str, str, str]]:
    return [(entry.source, entry.level, entry.text) for entry in log.entries]


def test_a_repeated_poll_adds_nothing_and_each_change_is_said_once() -> None:
    log, model = _following()
    model.adopt(two_display_status())
    model.adopt(two_display_status())
    assert _said(log) == [
        ("service", "info", "The wallpaper service is running"),
        ("playback", "info", "Playing Evening (schedule)"),
    ]

    crashed = two_display_status(extra={"last_error": "linux-wallpaperengine exited (1)"})
    model.adopt(crashed)
    model.adopt(crashed)
    model.adopt(two_display_status(extra={"output_discovery_error": "niri msg outputs failed"}))
    model.adopt(two_display_status(extra=BATTERY))
    model.adopt(two_display_status())
    assert _said(log)[2:] == [
        ("renderer", "error", "linux-wallpaperengine exited (1)"),
        ("displays", "warning", "Display discovery is degraded: niri msg outputs failed"),
        ("power", "info", "Animations stopped on battery"),
        ("power", "info", "Animations are no longer stopped for power"),
    ]
    assert log.problems == 2


def test_the_service_going_and_coming_back_and_its_bad_replies() -> None:
    log, model = _following()
    model.mark_unavailable(forget=True)
    model.mark_unavailable(forget=True)
    model.mark_delayed()  # the next probe timed out instead
    model.mark_delayed()
    model.mark_invalid("status reply is not JSON")
    model.mark_invalid("status reply is not JSON")
    model.set_busy(True)
    model.adopt(two_display_status())
    model.mark_delayed()
    assert [text for _source, _level, text in _said(log)] == [
        "The wallpaper service isn\u2019t running",
        "A status reply was late; the last one is shown until the next",
        "Invalid status reply: status reply is not JSON",
        "The wallpaper service is running",
        "Playing Evening (schedule)",
        "A status reply was late; the last one is shown until the next",
    ]


def test_it_keeps_only_the_newest_entries_and_bounded_text() -> None:
    log = ActivityLog(clock=_clock, limit=3)
    for number in range(5):
        log.record("app", f"message {number}")
    log.record("app", "   ")  # nothing to say
    assert [entry.text for entry in log.entries] == ["message 2", "message 3", "message 4"]
    log.record("renderer", "x" * (MAX_TEXT + 50), "error")
    assert len(log.entries[-1].text) == MAX_TEXT and log.entries[-1].text.endswith("…")
    log.record("app", "one\n  line")
    assert log.entries[-1].text == "one line"


def test_listeners_hear_each_new_entry_until_they_leave() -> None:
    log = ActivityLog(clock=_clock)
    heard: list[str] = []
    leave = log.subscribe(lambda entry: heard.append(entry.text))
    log.record("app", "first")
    leave()
    leave()
    log.record("app", "second")
    assert heard == ["first"]


def test_paths_are_hidden_for_showing_and_copying_but_kept() -> None:
    home = Path.home()
    assert hide_paths(f"Scanned {home}/Pictures/Wallpapers, 12 files") == (
        f"Scanned {HIDDEN}, 12 files"
    )
    assert hide_paths(f"Using {home} as the library.") == "Using ~ as the library."
    assert hide_paths("Using /mnt/walls as the library.") == f"Using {HIDDEN} as the library."
    assert hide_paths(f"{paths.settings_path()} is read-only") == f"{HIDDEN} is read-only"
    for untouched in (
        "https://wallhaven.cc/w/abc123 is 404",
        "1/2 done, and/or later",
        "Animations stopped on battery",
    ):
        assert hide_paths(untouched) == untouched
    log = ActivityLog(clock=_clock)
    log.record("app", f"Using {home}/walls as the wallpaper library")
    assert log.entries[0].text == f"Using {home}/walls as the wallpaper library"
    assert as_text(log.entries, hide=True) == (
        f"09:30:00  app       Using {HIDDEN} as the wallpaper library"
    )
    assert str(home) in as_text(log.entries, hide=False)
