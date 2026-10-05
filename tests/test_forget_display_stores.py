"""The store methods behind Forget Display: one transaction, and no guessing over a broken file."""

from __future__ import annotations

import pytest

from wall_in_one.library import displays, schedules


def test_a_displays_rules_go_together_and_are_returned() -> None:
    store = schedules.Store.open()
    first = store.add("p", connector="DP-2", weekdays=("mon",))
    shared = store.add("p")
    second = store.add("p", connector="DP-2", weekdays=("tue",))
    removed = store.remove_for_display("DP-2")
    assert removed == (first, second)
    assert schedules.Store.open().rules == (shared,)
    assert store.remove_for_display("DP-2") == (), "nothing left: no write"


def test_an_unreadable_schedule_refuses_and_is_left_as_it_is() -> None:
    store = schedules.Store.open()
    store.add("p", connector="DP-2")
    broken = b"{ not json"
    schedules.state_path().write_bytes(broken)
    assert "unreadable" in store.durable_problem()
    with pytest.raises(schedules.ScheduleError) as refused:
        store.remove_for_display("DP-2")
    assert refused.value.kind == "unreadable" and "Nothing was changed" in str(refused.value)
    assert schedules.state_path().read_bytes() == broken


def test_a_schedule_from_a_newer_version_is_a_durable_problem() -> None:
    path = schedules.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"version": 99, "rules": []}', encoding="utf-8")
    assert "newer-version" in schedules.Store.open().durable_problem()
    assert schedules.Store.open().durable_problem() == schedules.Store.open().durable_problem()


def test_forgetting_a_display_returns_its_playlist_and_takes_its_opt_in() -> None:
    store = displays.Store.open()
    store.assign("DP-2", "dock")
    store.set_beats_global_rules("DP-2", True)
    assert store.durable_problem() == ""
    assert store.forget_display("DP-2") == "dock"
    reopened = displays.Store.open()
    assert reopened.all() == () and not reopened.beats_global_rules("DP-2")
    assert store.forget_display("DP-2") == ""


def test_an_unreadable_display_store_refuses_and_is_left_as_it_is() -> None:
    store = displays.Store.open()
    store.assign("DP-2", "dock")
    broken = b"[not an object]"
    displays.state_path().write_bytes(broken)
    assert "unreadable" in store.durable_problem()
    with pytest.raises(displays.DisplayError) as refused:
        store.forget_display("DP-2")
    assert refused.value.kind == "unreadable"
    assert displays.state_path().read_bytes() == broken
