"""Schedules: which playlist the calendar asks for.

Resolution is pure and the clock is an argument, so all of this runs at any
hour of any month without waiting for one. The two rules that carry the
behaviour are that the *last* matching rule wins -- so a later rule carves an
exception out of an earlier one without either being rewritten -- and that a
window whose end is before its start wraps midnight, because "22:00 to 06:00"
is one window to a person.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from wall_in_one.library import schedules
from wall_in_one.library.schedules import Rule, ScheduleError, Store


@pytest.fixture(autouse=True)
def state_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return tmp_path / "state"


@pytest.fixture
def store(tmp_path: Path) -> Store:
    return Store(path=tmp_path / "schedules.json")


def at(text: str) -> datetime:
    """A moment, written as `YYYY-MM-DD HH:MM`."""
    return datetime.strptime(text, "%Y-%m-%d %H:%M")


# 2026-08-07 is a Friday; 2026-08-09 is a Sunday; 2026-12-25 is a Friday.
FRIDAY_MORNING = at("2026-08-07 09:00")
FRIDAY_NIGHT = at("2026-08-07 23:30")
SUNDAY_MORNING = at("2026-08-09 09:00")
CHRISTMAS = at("2026-12-25 09:00")


# -- reading the calendar -------------------------------------------------


def test_a_rule_with_nothing_set_always_matches() -> None:
    """Which is how you say "this playlist, until I say otherwise"."""
    assert Rule(id="r", playlist="Evening").matches(FRIDAY_MORNING)


def test_a_weekday_rule_matches_only_those_days() -> None:
    rule = Rule(id="r", playlist="Weekend", weekdays=schedules.parse_weekdays(["sat", "sun"]))
    assert rule.matches(SUNDAY_MORNING)
    assert not rule.matches(FRIDAY_MORNING)


def test_a_month_rule_matches_only_those_months() -> None:
    rule = Rule(id="r", playlist="Festive", months=frozenset({12}))
    assert rule.matches(CHRISTMAS)
    assert not rule.matches(FRIDAY_MORNING)


def test_a_window_is_inclusive_at_the_start_and_exclusive_at_the_end() -> None:
    """So two adjacent windows do not both match on the boundary minute."""
    rule = Rule(
        id="r",
        playlist="Day",
        start=schedules.parse_time("09:00"),
        end=schedules.parse_time("17:00"),
    )
    assert rule.matches(at("2026-08-07 09:00"))
    assert rule.matches(at("2026-08-07 16:59"))
    assert not rule.matches(at("2026-08-07 17:00"))
    assert not rule.matches(at("2026-08-07 08:59"))


def test_a_window_whose_end_is_before_its_start_wraps_midnight() -> None:
    """ "22:00 to 06:00" is one window to a person, not two rules."""
    rule = Rule(
        id="r",
        playlist="Night",
        start=schedules.parse_time("22:00"),
        end=schedules.parse_time("06:00"),
    )
    assert rule.matches(at("2026-08-07 23:30"))
    assert rule.matches(at("2026-08-07 02:00"))
    assert not rule.matches(at("2026-08-07 12:00"))


def test_a_zero_length_window_reads_as_always() -> None:
    """Almost certainly a mistake, and this is the reading that cannot hide a
    playlist somebody scheduled."""
    same = schedules.parse_time("09:00")
    assert Rule(id="r", playlist="X", start=same, end=same).matches(FRIDAY_NIGHT)


def test_a_disabled_rule_never_matches() -> None:
    assert not Rule(id="r", playlist="X", enabled=False).matches(FRIDAY_MORNING)


def test_the_conditions_are_combined_with_and() -> None:
    rule = Rule(
        id="r",
        playlist="Festive weekend evening",
        months=frozenset({12}),
        weekdays=schedules.parse_weekdays(["fri"]),
        start=schedules.parse_time("08:00"),
        end=schedules.parse_time("10:00"),
    )
    assert rule.matches(CHRISTMAS)
    assert not rule.matches(at("2026-12-25 11:00"))
    assert not rule.matches(at("2026-12-24 09:00"))


# -- which rule wins ------------------------------------------------------


def test_nothing_matching_asks_for_nothing() -> None:
    rules = [Rule(id="r", playlist="Festive", months=frozenset({12}))]
    assert schedules.resolve(rules, FRIDAY_MORNING) == ""


def test_the_last_matching_rule_wins() -> None:
    """A rule added later carves an exception out of an earlier one without
    either being rewritten."""
    rules = [
        Rule(id="broad", playlist="Weekdays", weekdays=schedules.parse_weekdays(["fri"])),
        Rule(id="narrow", playlist="Festive", months=frozenset({12})),
    ]
    assert schedules.resolve(rules, CHRISTMAS) == "Festive"
    assert schedules.resolve(rules, FRIDAY_MORNING) == "Weekdays"


def test_a_disabled_rule_does_not_take_priority() -> None:
    rules = [
        Rule(id="a", playlist="Weekdays"),
        Rule(id="b", playlist="Festive", months=frozenset({12}), enabled=False),
    ]
    assert schedules.resolve(rules, CHRISTMAS) == "Weekdays"


def test_the_pinned_default_is_used_when_no_rule_matches() -> None:
    rules = [Rule(id="r", playlist="Festive", months=frozenset({12}))]
    assert schedules.effective(rules, "Everyday", FRIDAY_MORNING) == "Everyday"
    assert schedules.effective(rules, "Everyday", CHRISTMAS) == "Festive"


# -- writing them ---------------------------------------------------------


def test_a_rule_is_added_at_the_end(store: Store) -> None:
    """Appending is how you override, because the last match wins."""
    store.add("First")
    store.add("Second")
    assert [rule.playlist for rule in store.rules] == ["First", "Second"]


@pytest.mark.parametrize("raw", ["25:00", "09:60", "nine", "0900", ""])
def test_a_time_that_is_not_one_is_refused(raw: str) -> None:
    with pytest.raises(ScheduleError) as caught:
        schedules.parse_time(raw)
    assert caught.value.kind == "invalid-time"


def test_half_a_window_is_refused(store: Store) -> None:
    """Guessing the other half would be inventing a schedule nobody wrote."""
    with pytest.raises(ScheduleError) as caught:
        store.add("Evening", start="22:00")
    assert caught.value.kind == "invalid-time"


def test_a_day_that_is_not_one_is_refused() -> None:
    with pytest.raises(ScheduleError) as caught:
        schedules.parse_weekdays(["funday"])
    assert caught.value.kind == "invalid-day"


@pytest.mark.parametrize("month", [0, 13, "december"])
def test_a_month_that_is_not_one_is_refused(month: object) -> None:
    with pytest.raises(ScheduleError) as caught:
        schedules.parse_months([month])  # type: ignore[list-item]
    assert caught.value.kind == "invalid-month"


def test_days_are_read_however_they_are_written() -> None:
    assert schedules.parse_weekdays(["Monday", " TUE ", "wed"]) == frozenset({0, 1, 2})


def test_a_rule_can_be_disabled_and_enabled(store: Store) -> None:
    rule = store.add("Evening")
    assert store.set_enabled(rule.id, False).enabled is False
    assert store.resolve(FRIDAY_MORNING) == ""
    store.set_enabled(rule.id, True)
    assert store.resolve(FRIDAY_MORNING) == "Evening"


def test_removing_reports_whether_there_was_one(store: Store) -> None:
    rule = store.add("Evening")
    assert store.remove(rule.id) is True
    assert store.remove(rule.id) is False


def test_deleting_a_playlist_takes_its_rules(store: Store) -> None:
    """A rule pointing at nothing looks like the schedule silently not
    working, rather than like a rule that should have gone."""
    store.add("Evening")
    store.add("Morning")
    assert store.forget_playlist("Evening") is True
    assert [rule.playlist for rule in store.rules] == ["Morning"]
    assert store.forget_playlist("Evening") is False


# -- the file -------------------------------------------------------------


def test_a_rule_outlives_the_process(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    Store(path=target).add("Evening", weekdays=["sat", "sun"], start="22:00", end="06:00")
    reopened = Store.open(target).rules
    assert len(reopened) == 1
    assert reopened[0].playlist == "Evening"
    assert reopened[0].weekdays == frozenset({5, 6})
    assert reopened[0].start == schedules.parse_time("22:00")


def test_rules_keep_their_order_across_a_reload(tmp_path: Path) -> None:
    """Order is the priority, so losing it silently changes what is in force."""
    target = tmp_path / "schedules.json"
    store = Store(path=target)
    for name in ("First", "Second", "Third"):
        store.add(name)
    assert [rule.playlist for rule in Store.open(target).rules] == ["First", "Second", "Third"]


@pytest.mark.parametrize(
    "content",
    ["", "not json", "[]", '{"rules": "not a list"}', "null", '{"nope": 1}'],
    ids=["empty", "garbage", "array", "wrong-type", "null", "no-key"],
)
def test_an_unreadable_file_is_no_schedule_rather_than_a_crash(
    tmp_path: Path, content: str
) -> None:
    target = tmp_path / "schedules.json"
    target.write_text(content, encoding="utf-8")
    assert schedules.load(target) == ()


def test_a_symlink_is_not_followed(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"rules": [{"id": "r", "playlist": "E"}]}), encoding="utf-8")
    link = tmp_path / "schedules.json"
    link.symlink_to(real)
    assert schedules.load(link) == ()


def test_one_bad_rule_costs_only_itself(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_text(
        json.dumps(
            {
                "rules": [
                    {"id": "good", "playlist": "Evening"},
                    {"playlist": "no id"},
                    "not even an object",
                    {"id": "bad-time", "playlist": "X", "start": "99:99", "end": "10:00"},
                    {"id": "also-good", "playlist": "Morning"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert [rule.id for rule in schedules.load(target)] == ["good", "also-good"]


def test_half_a_stored_window_is_read_as_no_window(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_text(
        json.dumps({"rules": [{"id": "r", "playlist": "E", "start": "22:00"}]}), encoding="utf-8"
    )
    rule = schedules.load(target)[0]
    assert (rule.start, rule.end) == (None, None)


def test_a_failed_write_leaves_no_debris(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def explode(_source: object, _destination: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(ScheduleError) as caught:
        schedules.save([Rule(id="r", playlist="E")], tmp_path / "schedules.json")
    assert caught.value.kind == "local-io"
    assert list(tmp_path.iterdir()) == []


def test_a_broken_file_is_moved_aside_rather_than_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_text("not json but somebody's schedule", encoding="utf-8")
    store = Store.open(target)
    assert store.fault is not None
    store.add("Evening")
    kept = target.with_name(target.name + schedules.BROKEN_SUFFIX)
    assert kept.read_text(encoding="utf-8") == "not json but somebody's schedule"


# -- describing it --------------------------------------------------------


def test_a_listing_marks_the_rule_in_force(store: Store) -> None:
    store.add("Weekdays")
    winner = store.add("Festive", months=[12])
    message = schedules.describe(store.rules, "Everyday", CHRISTMAS)
    lines = [line for line in message.splitlines() if not line.startswith("#")]
    in_force = [line for line in lines if line.endswith("\tyes")]
    assert len(in_force) == 1
    assert in_force[0].startswith(winner.id)


def test_a_listing_says_what_the_default_is(store: Store) -> None:
    assert "default Everyday" in schedules.describe((), "Everyday", FRIDAY_MORNING)
    assert "the whole library" in schedules.describe((), "", FRIDAY_MORNING)


def test_a_rule_describes_itself_in_the_words_it_was_written_in(store: Store) -> None:
    rule = store.add("Night", weekdays=["sat"], start="22:00", end="06:00")
    assert rule.describe() == "sat 22:00-06:00"
    assert store.add("Always").describe() == "always"
