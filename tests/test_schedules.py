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
import multiprocessing
import os
from datetime import datetime
from multiprocessing.connection import Connection
from pathlib import Path
from typing import Any

import pytest

from wall_in_one import file_io
from wall_in_one.library import schedules, state_file
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


def _add_rule_from_stale_process(
    target: str,
    ready: Connection,
    proceed: Connection,
) -> None:
    """Open before the other writer, then append to that stale Store."""
    store = Store.open(Path(target))
    ready.send(True)
    proceed.recv()
    store.add("Child", rule_id="child")


def _disable_rule_from_stale_process(
    target: str,
    rule_id: str,
    ready: Connection,
    proceed: Connection,
) -> None:
    """Apply a narrow same-record edit after another process changed it."""
    store = Store.open(Path(target))
    ready.send(True)
    proceed.recv()
    store.set_enabled(rule_id, False)


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


def test_a_wrapped_window_keeps_the_start_days_calendar_filters() -> None:
    """The Tuesday tail belongs to Monday's authored night, as in the legacy app."""
    rule = Rule(
        id="r",
        playlist="Monday night",
        months=frozenset({8}),
        weekdays=schedules.parse_weekdays(["mon"]),
        start=schedules.parse_time("22:00"),
        end=schedules.parse_time("06:00"),
    )

    assert rule.matches(at("2026-08-03 23:59"))
    assert rule.matches(at("2026-08-04 00:00"))
    assert rule.matches(at("2026-08-04 05:59"))
    assert not rule.matches(at("2026-08-04 06:00"))
    assert not rule.matches(at("2026-08-04 23:00"))


def test_a_wrapped_window_keeps_the_start_month_across_new_year() -> None:
    rule = Rule(
        id="r",
        playlist="New year",
        months=frozenset({12}),
        start=schedules.parse_time("22:00"),
        end=schedules.parse_time("06:00"),
    )

    assert rule.matches(at("2027-01-01 02:00"))
    assert not rule.matches(at("2027-01-02 02:00"))


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


def test_connector_rule_is_dormant_globally_and_exact_for_its_display() -> None:
    targeted = Rule(id="external", playlist="Dock", connector="DP-2")

    assert schedules.resolve((targeted,), FRIDAY_MORNING) == ""
    assert schedules.resolve((targeted,), FRIDAY_MORNING, "DP-2") == "Dock"
    assert schedules.resolve((targeted,), FRIDAY_MORNING, "eDP-1") == ""


def test_global_and_connector_rules_share_one_last_match_wins_order() -> None:
    rules = (
        Rule(id="global-first", playlist="Day"),
        Rule(id="dock-exception", playlist="Dock", connector="DP-2"),
        Rule(id="global-last", playlist="Night", months=frozenset({12})),
    )

    assert schedules.resolve(rules, FRIDAY_MORNING, "DP-2") == "Dock"
    assert schedules.resolve(rules, CHRISTMAS, "DP-2") == "Night"
    assert schedules.resolve_rule(rules, CHRISTMAS, "DP-2") is rules[-1]


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
    first = store.add("First")
    store.add("Second")
    assert [rule.playlist for rule in store.rules] == ["First", "Second"]
    assert store.rules[0] is first


def test_an_explicit_rule_id_cannot_make_the_store_fault_its_own_file(store: Store) -> None:
    store.add("First", rule_id="same")

    with pytest.raises(ScheduleError) as caught:
        store.add("Second", rule_id="same")

    assert caught.value.kind == "identity-conflict"
    assert [rule.playlist for rule in store.rules] == ["First"]


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


def test_a_rule_can_be_edited_without_changing_identity_or_priority(store: Store) -> None:
    first = store.add("Morning", weekdays=["mon"])
    second = store.add("Evening", weekdays=["fri"])

    updated = store.update(
        first.id,
        "Weekend",
        months=[12],
        weekdays=["sat", "sun"],
        start="22:00",
        end="06:00",
    )

    assert updated.id == first.id
    assert [rule.id for rule in store.rules] == [first.id, second.id]
    assert updated.playlist == "Weekend"
    assert updated.describe() == "months 12 sat,sun 22:00-06:00"


def test_a_connector_target_survives_add_edit_and_reload(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    store = Store(path=target)
    rule = store.add("Morning", connector="DP-9", rule_id="dock")

    assert rule.connector == "DP-9"
    updated = store.update(rule.id, "Evening", connector="DP-9")
    reopened = Store.open(target)
    assert updated.connector == "DP-9"
    assert reopened.rules[0].connector == "DP-9"
    assert json.loads(target.read_text(encoding="utf-8"))["version"] == 2


@pytest.mark.parametrize("connector", ["DP-1\x01", "DP 1", " DP-1", "x" * 257])
def test_invalid_connector_target_is_refused(store: Store, connector: str) -> None:
    with pytest.raises(ScheduleError) as caught:
        store.add("Evening", connector=connector)

    assert caught.value.kind == "invalid-connector"


def test_a_rule_can_move_without_changing_identity(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    store = Store(path=target)
    first = store.add("Morning", rule_id="first")
    second = store.add("Evening", rule_id="second")

    moved = store.move(second.id, 0)

    assert moved is second
    assert first.id == "first"
    assert [rule.id for rule in store.rules] == ["second", "first"]
    assert [rule.id for rule in Store.open(target).rules] == ["second", "first"]


def test_moving_a_rule_clamps_and_rejects_an_unknown_id(store: Store) -> None:
    store.add("One", rule_id="one")
    store.add("Two", rule_id="two")
    store.move("one", 100)
    assert [rule.id for rule in store.rules] == ["two", "one"]
    with pytest.raises(ScheduleError, match="no-such-rule"):
        store.move("missing", 0)


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


def test_stale_cross_process_add_preserves_rule_order_and_both_writes(tmp_path: Path) -> None:
    """The later transaction appends to current disk, not its empty snapshot."""
    target = tmp_path / "schedules.json"
    parent = Store.open(target)
    context = multiprocessing.get_context("spawn")
    ready_parent, ready_child = context.Pipe()
    proceed_parent, proceed_child = context.Pipe()
    process = context.Process(
        target=_add_rule_from_stale_process,
        args=(str(target), ready_child, proceed_child),
    )
    process.start()
    try:
        assert ready_parent.poll(5), "child did not open its stale schedule Store"
        assert ready_parent.recv() is True
        parent.add("Parent", rule_id="parent")
        proceed_parent.send(True)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)

    assert [(rule.id, rule.playlist) for rule in Store.open(target).rules] == [
        ("parent", "Parent"),
        ("child", "Child"),
    ]


def test_stale_same_rule_edit_merges_with_latest_cross_process_fields(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    seeded = Store(path=target)
    seeded.add("Original", weekdays=("mon",), rule_id="shared")
    parent = Store.open(target)
    context = multiprocessing.get_context("spawn")
    ready_parent, ready_child = context.Pipe()
    proceed_parent, proceed_child = context.Pipe()
    process = context.Process(
        target=_disable_rule_from_stale_process,
        args=(str(target), "shared", ready_child, proceed_child),
    )
    process.start()
    try:
        assert ready_parent.poll(5), "child did not open its stale schedule Store"
        assert ready_parent.recv() is True
        parent.update("shared", "Parent edit", months=(12,), weekdays=("fri",))
        proceed_parent.send(True)
        process.join(5)
        assert process.exitcode == 0
    finally:
        if process.is_alive():
            process.terminate()
            process.join(5)

    rule = Store.open(target).rules[0]
    assert rule.playlist == "Parent edit"
    assert rule.months == frozenset({12})
    assert rule.weekdays == frozenset({4})
    assert rule.enabled is False


# -- the file -------------------------------------------------------------


def test_a_rule_outlives_the_process(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    Store(path=target).add("Evening", weekdays=["sat", "sun"], start="22:00", end="06:00")
    reopened = Store.open(target).rules
    assert len(reopened) == 1
    assert reopened[0].playlist == "Evening"
    assert reopened[0].weekdays == frozenset({5, 6})
    assert reopened[0].start == schedules.parse_time("22:00")


def test_a_direct_absent_file_seed_is_retained_on_its_first_mutation(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    seeded = Rule(id="seed", playlist="Seed")
    store = Store((seeded,), target)

    assert store.remove("missing") is False
    store.add("Added", rule_id="added")

    assert [(rule.id, rule.playlist) for rule in Store.open(target).rules] == [
        ("seed", "Seed"),
        ("added", "Added"),
    ]


def test_version_one_schedule_migrates_on_the_next_authoring_write(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_text(
        json.dumps(
            {
                "version": 1,
                "rules": [{"id": "old", "playlist": "Evening", "weekdays": ["sat"]}],
            }
        ),
        encoding="utf-8",
    )

    store = Store.open(target)
    assert store.fault is None
    assert store.rules[0].connector == ""
    store.add("Morning", connector="DP-2", rule_id="new")

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["version"] == 2
    assert payload["rules"][0].get("connector") is None
    assert payload["rules"][1]["connector"] == "DP-2"
    assert not target.with_name(target.name + schedules.BROKEN_SUFFIX).exists()


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
                    {"id": "bad-day-type", "playlist": "X", "weekdays": [5]},
                    {"id": "bad-time", "playlist": "X", "start": "99:99", "end": "10:00"},
                    {"id": "also-good", "playlist": "Morning"},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert [rule.id for rule in schedules.load(target)] == ["good", "also-good"]
    assert Store.open(target).fault is not None


def test_surrogate_json_document_is_rejected_without_crashing(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_text(
        '{"version": 2, "rules": ['
        '{"id": "bad", "playlist": "\\ud800"}, '
        '{"id": "good", "playlist": "Evening"}]\n}',
        encoding="utf-8",
    )

    opened = Store.open(target)

    assert opened.rules == ()
    assert opened.fault is not None
    with pytest.raises(ScheduleError) as caught:
        opened.add("\ud800")
    assert caught.value.kind == "no-such-rule"


def test_duplicate_rule_ids_recover_to_the_first_before_an_authoring_write(
    tmp_path: Path,
) -> None:
    target = tmp_path / "schedules.json"
    original = json.dumps(
        {
            "version": schedules.FORMAT_VERSION,
            "rules": [
                {"id": "same", "playlist": "First"},
                {"id": "same", "playlist": "Second"},
            ],
        }
    )
    target.write_text(original, encoding="utf-8")
    store = Store.open(target)

    assert [rule.playlist for rule in store.rules] == ["First"]
    assert store.fault is not None
    store.add("Added", rule_id="added")

    assert (
        target.with_name(target.name + schedules.BROKEN_SUFFIX).read_text(encoding="utf-8")
        == original
    )
    reopened = Store.open(target)
    assert [(rule.id, rule.playlist) for rule in reopened.rules] == [
        ("same", "First"),
        ("added", "Added"),
    ]
    assert reopened.fault is None


def test_half_a_stored_window_is_read_as_no_window(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_text(
        json.dumps({"rules": [{"id": "r", "playlist": "E", "start": "22:00"}]}), encoding="utf-8"
    )
    rule = schedules.load(target)[0]
    assert (rule.start, rule.end) == (None, None)
    assert Store.open(target).fault is not None


def test_a_failed_write_leaves_only_inert_private_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def explode(_source: object, _destination: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(ScheduleError) as caught:
        schedules.save([Rule(id="r", playlist="E")], tmp_path / "schedules.json")
    assert caught.value.kind == "local-io"
    retained = tmp_path / file_io.RETAINED_ENTRY_DIRECTORY
    assert set(tmp_path.iterdir()) == {retained}
    residues = tuple(retained.iterdir())
    assert len(residues) == 2
    assert any(path.is_file() and path.stat().st_size == 0 for path in residues)
    assert any(path.is_dir() and tuple(path.iterdir()) == () for path in residues)


def test_a_store_write_failure_does_not_change_the_in_memory_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "schedules.json"
    store = Store(path=target)
    made = store.add("Evening", rule_id="rule")

    def fail(_rules: object, _path: object, **_options: object) -> None:
        raise ScheduleError("local-io", "injected write failure")

    monkeypatch.setattr(schedules, "save", fail)
    with pytest.raises(ScheduleError):
        store.set_enabled(made.id, False)

    assert store.rules[0].enabled is True
    assert Store.open(target).rules[0].enabled is True


def test_a_broken_file_is_moved_aside_rather_than_overwritten(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_text("not json but somebody's schedule", encoding="utf-8")
    store = Store.open(target)
    assert store.fault is not None
    store.add("Evening")
    kept = target.with_name(target.name + schedules.BROKEN_SUFFIX)
    assert kept.read_text(encoding="utf-8") == "not json but somebody's schedule"


def test_a_fault_which_appears_after_open_is_still_preserved(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    store = Store.open(target)
    original = "a concurrent broken schedule"
    target.write_text(original, encoding="utf-8")

    store.add("Evening", rule_id="new")

    assert (
        target.with_name(target.name + schedules.BROKEN_SUFFIX).read_text(encoding="utf-8")
        == original
    )
    assert Store.open(target).rules[0].id == "new"


def test_a_valid_manual_repair_after_the_fault_read_remains_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "schedules.json"
    target.write_text("not json", encoding="utf-8")
    store = Store.open(target)
    manual = Rule(id="manual", playlist="Manual")
    preserve = state_file.preserve_faulted

    def repair_then_preserve(
        path: Path,
        *,
        observed: state_file.StateFileObservation,
    ) -> Path:
        schedules.save((manual,), path)
        return preserve(path, observed=observed)

    monkeypatch.setattr(state_file, "preserve_faulted", repair_then_preserve)

    with pytest.raises(ScheduleError) as caught:
        store.add("App", rule_id="app")

    assert caught.value.kind == "local-io"
    assert Store.open(target).rules == (manual,)
    assert not target.with_name(target.name + schedules.BROKEN_SUFFIX).exists()


def test_a_valid_manual_repair_before_recovery_publication_remains_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "schedules.json"
    original = "not json"
    target.write_text(original, encoding="utf-8")
    store = Store.open(target)
    manual = Rule(id="manual", playlist="Manual")
    save = schedules.save

    def repair_then_save(
        updated: tuple[Rule, ...],
        path: Path | None = None,
        *,
        replace_existing: bool = True,
        unknown: state_file.Unknown = state_file.NOTHING_UNKNOWN,
        version: int | None = None,
    ) -> Path:
        assert path == target
        assert not replace_existing
        save((manual,), target)
        return save(
            updated, target, replace_existing=replace_existing, unknown=unknown, version=version
        )

    monkeypatch.setattr(schedules, "save", repair_then_save)

    with pytest.raises(ScheduleError) as caught:
        store.add("App", rule_id="app")

    assert caught.value.kind == "local-io"
    assert store.fault is not None
    assert Store.open(target).rules == (manual,)
    assert target.with_name(target.name + schedules.BROKEN_SUFFIX).read_text() == original


def test_a_symlinked_mutation_lock_is_reported_without_touching_its_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "schedules.json"
    sentinel = tmp_path / "outside"
    sentinel.write_text("do not touch", encoding="utf-8")
    target.with_name(f".{target.name}.mutation.lock").symlink_to(sentinel)
    store = Store(path=target)

    with pytest.raises(ScheduleError) as caught:
        store.add("Evening", rule_id="new")

    assert caught.value.kind == "local-io"
    assert sentinel.read_text(encoding="utf-8") == "do not touch"
    assert not target.exists()
    assert len(store) == 0


def test_a_failed_broken_file_relocation_keeps_the_fault_and_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "schedules.json"
    original = "not json but somebody's schedule"
    store = Store.open(target)
    target.write_text(original, encoding="utf-8")

    def fail(_path: Path, *, observed: state_file.StateFileObservation) -> Path:
        assert observed.present
        raise OSError("injected relocation failure")

    monkeypatch.setattr(state_file, "preserve_faulted", fail)
    with pytest.raises(ScheduleError):
        store.add("Evening")

    assert store.fault is not None
    assert target.read_text(encoding="utf-8") == original
    assert len(store) == 0


# -- describing it --------------------------------------------------------


def test_a_listing_marks_the_rule_in_force(store: Store) -> None:
    store.add("Weekdays")
    winner = store.add("Festive", months=[12])
    message = schedules.describe(store.rules, "Everyday", CHRISTMAS)
    rows = [line.split("\t") for line in message.splitlines() if not line.startswith("#")]
    in_force = [row for row in rows if row[4] == "yes"]
    assert len(in_force) == 1
    assert in_force[0][0] == winner.id


def test_a_listing_says_what_the_default_is(store: Store) -> None:
    assert "default Everyday" in schedules.describe((), "Everyday", FRIDAY_MORNING)
    assert "All media" in schedules.describe((), "", FRIDAY_MORNING)


def test_a_rule_describes_itself_in_the_words_it_was_written_in(store: Store) -> None:
    rule = store.add("Night", weekdays=["sat"], start="22:00", end="06:00")
    assert rule.describe() == "sat 22:00-06:00"
    assert store.add("Always").describe() == "always"


# -- rule names: format version 3, written only when used ---------------------------

#: A version-2 file exactly as 0.1.4 and 0.1.5 write it.
V2_RULES: list[dict[str, Any]] = [
    {"id": "quiet", "playlist": "quick-choice", "enabled": False},
    {"id": "night", "playlist": "Night", "start": "21:00", "end": "04:00"},
    {
        "id": "summer",
        "playlist": "Summer",
        "connector": "HDMI-A-1",
        "months": [6, 7, 8],
        "weekdays": ["sat", "sun"],
    },
]


def _as_written(document: dict[str, object]) -> bytes:
    """The serializer every schedules version so far has used, byte for byte."""
    return (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _v2_file(tmp_path: Path, rules: list[dict[str, Any]] | None = None) -> tuple[Path, bytes]:
    target = tmp_path / "schedules.json"
    original = _as_written({"version": 2, "rules": rules if rules is not None else V2_RULES})
    target.write_bytes(original)
    target.chmod(0o644)
    return target, original


def _backups(target: Path) -> list[str]:
    return sorted(path.name for path in target.parent.glob(f"{target.name}.v*-backup"))


def _document(target: Path) -> dict[str, object]:
    loaded = json.loads(target.read_bytes())
    assert isinstance(loaded, dict)
    return loaded


@pytest.mark.parametrize(
    ("raw", "stored"),
    (
        ("Frog day", "Frog day"),
        ("  Frog \t day\n", "Frog day"),
        ("", None),
        ("   \t\n", None),
        (None, None),
        ("x" * schedules.MAX_NAME_LENGTH, "x" * schedules.MAX_NAME_LENGTH),
    ),
)
def test_a_name_is_trimmed_and_an_empty_one_is_none(raw: str | None, stored: str | None) -> None:
    assert schedules.tidy_name(raw) == stored


def test_a_name_is_bounded_like_a_playlist_name() -> None:
    assert schedules.MAX_NAME_LENGTH == 120
    with pytest.raises(ScheduleError) as caught:
        schedules.tidy_name("x" * (schedules.MAX_NAME_LENGTH + 1))
    assert caught.value.kind == "invalid-name"
    with pytest.raises(ScheduleError):
        schedules.tidy_name("\ud800")


def test_unnamed_edits_keep_version_two_byte_for_byte(tmp_path: Path) -> None:
    """Guard 1: without a name the file is exactly what 0.1.4 and 0.1.5 write."""
    target, _original = _v2_file(tmp_path)
    store = Store.open(target)

    store.add("Evening", weekdays=["fri"], start="18:00", end="23:00", rule_id="evening")
    store.set_enabled("quiet", True)
    store.update("night", "Night", start="22:00", end="05:00")
    store.move("summer", 0)
    store.set_name("night", "   ")

    expected = [
        V2_RULES[2],
        {"id": "quiet", "playlist": "quick-choice"},
        {"id": "night", "playlist": "Night", "start": "22:00", "end": "05:00"},
        {
            "id": "evening",
            "playlist": "Evening",
            "weekdays": ["fri"],
            "start": "18:00",
            "end": "23:00",
        },
    ]
    assert target.read_bytes() == _as_written({"version": 2, "rules": expected})
    assert _backups(target) == []


def test_a_new_unnamed_schedule_is_version_two(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    Store.open(target).add("Evening", rule_id="evening")
    assert target.read_bytes() == _as_written(
        {"version": 2, "rules": [{"id": "evening", "playlist": "Evening"}]}
    )


def test_naming_a_rule_bumps_to_version_three_after_one_exact_backup(tmp_path: Path) -> None:
    target, original = _v2_file(tmp_path)
    store = Store.open(target)

    named = store.set_name("night", "  Frog   night ")

    assert named.name == "Frog night"
    assert store.rules[1] == named
    document = _document(target)
    assert document["version"] == 3
    assert document["rules"] == [
        V2_RULES[0],
        {**V2_RULES[1], "name": "Frog night"},
        V2_RULES[2],
    ]
    backup = target.with_name("schedules.json.v2-backup")
    assert _backups(target) == [backup.name]
    assert backup.read_bytes() == original
    assert os.stat(backup).st_mode & 0o777 == 0o644

    # Further edits at version 3 never touch the backup again.
    before = backup.stat()
    store.set_name("quiet", "Your pick, parked")
    store.add("Evening", rule_id="evening", name="Evening")
    assert backup.read_bytes() == original
    assert file_io.file_fingerprint(backup.stat()) == file_io.file_fingerprint(before)
    assert _backups(target) == [backup.name]


def test_a_second_bump_never_overwrites_the_first_backup(tmp_path: Path) -> None:
    """0.1.4 narrows a version-3 file back to 2; naming again bumps again."""
    target, original = _v2_file(tmp_path)
    Store.open(target).set_name("night", "Frog night")
    narrowed = _as_written({"version": 2, "rules": [{"id": "night", "playlist": "Night"}]})
    target.write_bytes(narrowed)

    store = Store.open(target)
    assert store.rules[0].name is None
    store.set_name("night", "Frog night again")

    assert target.with_name("schedules.json.v2-backup").read_bytes() == original
    assert _document(target)["version"] == 3
    assert _backups(target) == ["schedules.json.v2-backup"]


def test_clearing_the_last_name_stays_on_version_three(tmp_path: Path) -> None:
    """Decided: a bumped file never flips back, though the data would fit v2."""
    target, original = _v2_file(tmp_path)
    store = Store.open(target)
    store.set_name("night", "Frog night")

    store.set_name("night", None)
    assert _document(target) == {"version": 3, "rules": V2_RULES}
    store.set_name("quiet", "")
    store.set_enabled("quiet", True)

    assert _document(target)["version"] == 3
    assert all(rule.name is None for rule in Store.open(target).rules)
    assert target.with_name("schedules.json.v2-backup").read_bytes() == original
    assert _backups(target) == ["schedules.json.v2-backup"]


def test_a_named_rule_keeps_its_name_through_every_other_edit(tmp_path: Path) -> None:
    target, _original = _v2_file(tmp_path)
    store = Store.open(target)
    store.set_name("night", "Frog night")

    store.update("night", "Evening", weekdays=["sat"])
    store.set_enabled("night", False)
    store.move("night", 0)
    store.move_relative("night", 1)

    (night,) = [rule for rule in Store.open(target).rules if rule.id == "night"]
    assert night.name == "Frog night"
    assert night.playlist == "Evening"
    assert night.weekdays == frozenset({5})
    assert not night.enabled


def test_a_version_three_file_round_trips_its_names(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_bytes(
        _as_written(
            {
                "version": 3,
                "rules": [
                    {"id": "a", "playlist": "X", "name": "Frog day"},
                    {"id": "b", "playlist": "Y"},
                ],
            }
        )
    )
    store = Store.open(target)
    assert store.fault is None
    assert [(rule.id, rule.name) for rule in store.rules] == [("a", "Frog day"), ("b", None)]

    store.set_enabled("b", False)

    assert _document(target) == {
        "version": 3,
        "rules": [
            {"id": "a", "playlist": "X", "name": "Frog day"},
            {"id": "b", "playlist": "Y", "enabled": False},
        ],
    }
    assert _backups(target) == [], "already version 3: nothing to back up"


def test_a_named_rule_in_a_new_file_needs_no_backup(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    Store.open(target).add("Evening", rule_id="evening", name="Evening")
    assert _document(target) == {
        "version": 3,
        "rules": [{"id": "evening", "playlist": "Evening", "name": "Evening"}],
    }
    assert _backups(target) == []


def test_a_version_one_file_named_is_backed_up_as_version_one(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    original = _as_written({"version": 1, "rules": [{"id": "a", "playlist": "X"}]})
    target.write_bytes(original)

    Store.open(target).set_name("a", "First")

    assert _document(target)["version"] == 3
    assert target.with_name("schedules.json.v1-backup").read_bytes() == original
    assert _backups(target) == ["schedules.json.v1-backup"]


@pytest.mark.parametrize("occupant", ("directory", "symlink"))
def test_a_refused_backup_means_no_bump(tmp_path: Path, occupant: str) -> None:
    """Guard 2: no backup, no save. The file and the live schedule stay put."""
    target, original = _v2_file(tmp_path)
    backup = target.with_name("schedules.json.v2-backup")
    if occupant == "directory":
        backup.mkdir()
    else:
        backup.symlink_to(tmp_path / "nowhere")
    store = Store.open(target)
    before = store.rules

    with pytest.raises(ScheduleError) as caught:
        store.set_name("night", "Frog night")

    assert caught.value.kind == "no-backup"
    assert "schedules.json.v2-backup" in str(caught.value)
    assert str(caught.value).endswith("Nothing was changed.")
    assert target.read_bytes() == original
    assert store.rules == before
    assert store.fault is None
    assert not list(tmp_path.glob("*.broken*"))
    assert not list(tmp_path.glob(".schedules.json*.tmp"))

    # An unnamed edit needs no bump, so it still works meanwhile.
    store.set_enabled("quiet", True)
    assert _document(target)["version"] == 2


def test_a_failed_backup_write_means_no_bump(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, original = _v2_file(tmp_path)
    real = state_file.write_atomic_bytes

    def full_disk(
        path: Path, contents: bytes, *, replace_existing: bool = True, mode: int | None = None
    ) -> None:
        if path.name.endswith("-backup"):
            raise OSError(28, "No space left on device")
        real(path, contents, replace_existing=replace_existing, mode=mode)

    monkeypatch.setattr(state_file, "write_atomic_bytes", full_disk)
    store = Store.open(target)

    with pytest.raises(ScheduleError) as caught:
        store.add("Evening", name="Evening")

    assert caught.value.kind == "no-backup"
    assert "No space left on device" in str(caught.value)
    assert target.read_bytes() == original
    assert len(store) == len(V2_RULES)
    assert not target.with_name("schedules.json.v2-backup").exists()


def test_a_faulted_file_is_backed_up_before_it_is_moved_aside(tmp_path: Path) -> None:
    """The bump's backup and the fault's ``.broken`` copy both hold the original."""
    target, original = _v2_file(tmp_path, [*V2_RULES, {"id": "bad", "playlist": 7}])
    store = Store.open(target)
    assert store.fault is not None

    store.set_name("night", "Frog night")

    assert target.with_name("schedules.json.v2-backup").read_bytes() == original
    assert target.with_name("schedules.json.broken").read_bytes() == original
    assert _document(target)["version"] == 3
    assert Store.open(target).fault is None


@pytest.mark.parametrize("name", (7, None, ["Frog"], "x" * (schedules.MAX_NAME_LENGTH + 1)))
def test_a_malformed_stored_name_is_a_fault(tmp_path: Path, name: object) -> None:
    target = tmp_path / "schedules.json"
    target.write_bytes(
        _as_written({"version": 3, "rules": [{"id": "a", "playlist": "X", "name": name}]})
    )
    store = Store.open(target)
    assert store.fault is not None
    assert "malformed" in store.fault
    assert [(rule.id, rule.name) for rule in store.rules] == [("a", None)]


def test_a_blank_stored_name_is_no_name(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    target.write_bytes(
        _as_written({"version": 3, "rules": [{"id": "a", "playlist": "X", "name": "  "}]})
    )
    store = Store.open(target)
    assert store.fault is None
    assert store.rules[0].name is None


def test_naming_an_unknown_rule_says_so(store: Store) -> None:
    with pytest.raises(ScheduleError) as caught:
        store.set_name("nope", "Frog")
    assert caught.value.kind == "no-such-rule"
    with pytest.raises(ScheduleError) as caught:
        store.add("Evening", name="x" * 500)
    assert caught.value.kind == "invalid-name"


def test_save_writes_the_oldest_version_that_holds_the_rules(tmp_path: Path) -> None:
    target = tmp_path / "schedules.json"
    schedules.save([Rule(id="a", playlist="X")], target)
    assert _document(target)["version"] == 2
    schedules.save([Rule(id="a", playlist="X", name="Frog")], target)
    assert _document(target)["version"] == 3
    schedules.save([Rule(id="a", playlist="X")], target, version=3)
    assert _document(target) == {"version": 3, "rules": [{"id": "a", "playlist": "X"}]}
    with pytest.raises(ValueError):
        schedules.save([Rule(id="a", playlist="X", name="Frog")], target, version=2)
    with pytest.raises(ValueError):
        schedules.save([Rule(id="a", playlist="X")], target, version=4)


def test_a_rule_is_called_by_its_name_else_its_playlist() -> None:
    names = {"x": "Evening"}
    assert schedules.rule_label(Rule(id="a", playlist="x", name="Frog day"), names) == "Frog day"
    assert schedules.rule_label(Rule(id="a", playlist="x"), names) == "Evening"
    assert schedules.rule_label(Rule(id="a", playlist="gone"), names) == "gone"


def test_a_listing_shows_each_rules_name_last(store: Store) -> None:
    named = store.add("Weekdays", name="Work days")
    plain = store.add("Festive", months=[12])
    message = schedules.describe(store.rules, "Everyday", CHRISTMAS)
    assert "# fields: rule, playlist, when, enabled, in-force, name" in message
    rows = {
        row[0]: row
        for row in (line.split("\t") for line in message.splitlines())
        if not row[0].startswith("#")
    }
    assert rows[named.id][5] == "Work days"
    assert rows[plain.id][5] == "-"
    assert all(len(row) == 6 for row in rows.values())
