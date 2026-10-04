"""The new Schedule and Displays pages' mappings and their real adapter, without a display.

The page convention (`schedule_state.RuleView`: Monday=0, months 1-12,
minutes) against the schedule store's own (``mon``..``sun``, ``HH:MM``), the
list's top-wins order against the store's last-wins order, the time windows a
calendar drag may create, and `RealScheduling` over a real `Session` with
the recording application from ``test_next_state``: what it writes, what it
refuses, and that "now" is the runtime's answer.
"""

from __future__ import annotations

import datetime as dt
import json
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from tests.test_next_state import FakeApplication, _library, _session
from tests.test_next_status_line import two_display_status
from wall_in_one import config, paths, runtime_config
from wall_in_one.library import displays, playlists, schedules
from wall_in_one.library.model import MediaItem
from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME, Session
from wall_in_one.ui.next import real_schedule
from wall_in_one.ui.next import schedule_model as model
from wall_in_one.ui.next.displays_settings import NEEDS_AN_ATTACHED_DISPLAY, settings_edit
from wall_in_one.ui.next.real_schedule import (
    RealScheduling,
    rule_view,
    store_arguments,
    stored_rule,
)
from wall_in_one.ui.next.schedule_state import RuleView, Scheduling

if TYPE_CHECKING:
    # The conformance proof, checked by mypy --strict; nothing runs.
    def _conforms(adapter: RealScheduling) -> Scheduling:
        return adapter


class Owner:
    """The adapter that owns `RealScheduling`: it records the topics it is told about."""

    def __init__(self) -> None:
        self.topics: list[str] = []

    def emit_changed(self, *topics: str) -> None:
        self.topics.extend(topics)


class Publishing(FakeApplication):
    """The recording application, publishing the runtime configuration like the app."""

    def __init__(self, session: Session) -> None:
        super().__init__(session)
        self.target = paths.app_state_dir() / "runtime.toml"

    def _publish(self) -> None:
        self.published += 1
        runtime_config.write(self._session.settings, self._session, self.target)

    def schedule_edited(self) -> None:
        self._publish()

    def runtime_config_changed(self) -> None:
        self._publish()


@pytest.fixture(autouse=True)
def no_monitors(monkeypatch: pytest.MonkeyPatch) -> None:
    """No GTK monitors: connected displays come from the runtime alone here.

    Without a display there are none anyway; in a process where a GUI test
    opened one (Xvfb's single "screen"), it must not count.
    """
    monkeypatch.setattr(real_schedule, "_monitor_displays", list)


@pytest.fixture
def items(tmp_path: Path) -> tuple[MediaItem, ...]:
    return _library(tmp_path / "home" / "Pictures" / "Wallpapers")


@pytest.fixture
def backend(items: tuple[MediaItem, ...]) -> Iterator[Publishing]:
    session = _session(items)
    try:
        yield Publishing(session)
    finally:
        session.shutdown()


def _scheduling(backend: FakeApplication, runtime_off: str = "") -> tuple[RealScheduling, Owner]:
    owner = Owner()
    return RealScheduling(backend, owner, lambda: runtime_off), owner


def _playlist(session: Session, name: str, *items: MediaItem) -> str:
    made = session.playlists.create(name)
    for item in items:
        session.playlists.add(made.id, item.path)
    return made.id


# -- the rule convention ------------------------------------------------------------------


@pytest.mark.parametrize(
    "view",
    [
        RuleView("", "p", weekdays=frozenset({0, 6}), months=frozenset({1, 12})),
        RuleView("", "p", weekdays=frozenset({4, 5}), start=22 * 60, end=2 * 60),
        RuleView("", "p", months=frozenset({12}), connector="HDMI-A-1", enabled=False),
        RuleView("", "p", start=7 * 60 + 15, end=18 * 60 + 45),
        RuleView("", "p"),
    ],
)
def test_a_rule_survives_the_store_both_ways(view: RuleView) -> None:
    """Monday=0, months 1-12 and minutes go into the store as mon..sun and HH:MM, and back."""
    store = schedules.Store.open()
    arguments = store_arguments(view)
    assert all(day in schedules.WEEKDAY_NAMES for day in arguments["weekdays"])
    saved = store.add(view.playlist, **arguments)
    if not view.enabled:
        saved = store.set_enabled(saved.id, False)
    back = rule_view(schedules.Store.open().rules[-1])
    assert replace(back, id="") == view
    assert back.id == saved.id


def test_the_store_numbers_are_the_pages_numbers() -> None:
    """Sunday is 6 and December 12 on both sides; "sun" is what the file says."""
    view = RuleView("", "p", weekdays=frozenset({6}), months=frozenset({12}), start=0, end=60)
    arguments = store_arguments(view)
    assert arguments == {
        "months": [12],
        "weekdays": ["sun"],
        "start": "00:00",
        "end": "01:00",
        "connector": "",
    }
    sunday_in_december = dt.datetime(2026, 12, 6, 0, 30)
    assert sunday_in_december.weekday() == 6
    assert stored_rule(view).matches(sunday_in_december)
    assert not stored_rule(view).matches(sunday_in_december.replace(month=11, day=29))


def test_an_overnight_tail_belongs_to_the_day_it_started() -> None:
    """Friday 22:00-02:00 still plays at 01:00 on Saturday, and not on Friday at 01:00."""
    friday_night = RuleView("r", "p", weekdays=frozenset({4}), start=22 * 60, end=2 * 60)
    monday = dt.date(2026, 9, 28)

    def applies(rule: RuleView, at: dt.datetime, connector: str) -> bool:
        return stored_rule(rule).matches(at, connector)

    spans = model.week_spans([friday_night], monday, "", applies)
    friday = [(s.start, s.end) for s in spans[4] if s.rule is friday_night]
    saturday = [(s.start, s.end) for s in spans[5] if s.rule is friday_night]
    assert friday == [(22 * 60, 24 * 60)]
    assert saturday == [(0, 2 * 60)]


# -- the list's order ---------------------------------------------------------------------


def test_the_top_of_the_list_is_the_end_of_the_store() -> None:
    assert [model.shown_index(i, 3) for i in range(3)] == [2, 1, 0]
    assert [model.stored_index(model.shown_index(i, 5), 5) for i in range(5)] == list(range(5))
    assert model.stored_step(2, 0) == 2, "two places up the list is two places later"
    assert model.stored_step(0, 1) == -1


def test_moving_a_row_up_the_list_gives_it_priority_in_the_store() -> None:
    store = schedules.Store.open()
    a = store.add("a")
    b = store.add("b")
    c = store.add("c")
    shown = [rule.id for rule in reversed(store.rules)]
    assert shown == [c.id, b.id, a.id]
    # Drag the bottom row (the lowest priority) to the top.
    store.move_relative(a.id, model.stored_step(shown.index(a.id), 0))
    assert [rule.id for rule in reversed(store.rules)] == [a.id, c.id, b.id]
    at = dt.datetime(2026, 10, 3, 12, 0)
    assert schedules.resolve_rule(store.rules, at) is not None
    assert schedules.resolve_rule(store.rules, at).id == a.id  # type: ignore[union-attr]


# -- time windows --------------------------------------------------------------------------


def test_a_window_that_starts_and_ends_together_is_refused() -> None:
    assert model.window_problem(600, 600)
    assert model.window_problem(600, None)
    assert model.window_problem(None, None) == ""
    assert model.window_problem(22 * 60, 6 * 60) == ""


def test_a_calendar_drag_never_makes_an_always_window() -> None:
    assert model.window_from_drag(600, 600) is None, "no width: no rule"
    assert model.window_from_drag(600, 540) is None
    assert model.window_from_drag(0, 24 * 60) == (None, None), "the whole day is all day"
    assert model.window_from_drag(23 * 60, 24 * 60) == (23 * 60, 0), "until midnight wraps"
    assert model.window_from_drag(9 * 60, 17 * 60) == (9 * 60, 17 * 60)


# -- words -----------------------------------------------------------------------------------


def test_words_for_days_months_and_times() -> None:
    assert model.days_text({0, 1, 2, 3, 4}) == "Mon\u2013Fri"
    assert model.days_text(set()) == "Every day"
    assert model.days_text({5, 6, 0}) == "Sat\u2013Mon"
    assert model.months_text({12}) == "December"
    assert model.months_text({11, 12, 1, 2}) == "Nov\u2013Feb"
    assert model.months_text({1, 2}) == "Jan, Feb"
    rule = RuleView("", "p", weekdays=frozenset({0, 1, 2, 3, 4}), start=540, end=1020)
    assert model.summary(replace(rule, connector="HDMI-A-1")) == (
        "Mon\u2013Fri · 09:00\u201317:00 · HDMI-A-1"
    )
    assert model.summary(RuleView("", "p", start=18 * 60, end=7 * 60)).endswith("(overnight)")
    assert model.summary(RuleView("", "p")) == "Always"


def test_the_reason_takes_the_winner_it_is_given() -> None:
    """The page passes the runtime's rule id; the words never pick another winner."""
    at = dt.datetime(2026, 10, 2, 12, 0)  # a Friday

    def applies(rule: RuleView, when: dt.datetime, connector: str) -> bool:
        return stored_rule(rule).matches(when, connector)

    later = RuleView("later", "p")  # would win locally: it is last and always applies
    chosen = RuleView("chosen", "q", weekdays=frozenset({6}))  # doesn't apply on Friday
    assert model.reason(chosen, at, "", "chosen", applies, mirrored=True) == ("Playing", "playing")
    assert model.reason(later, at, "", "chosen", applies, mirrored=True)[1] == "overridden"
    targeted = RuleView("t", "p", connector="DP-1")
    assert model.reason(targeted, at, "", None, applies, mirrored=True)[1] == "off"


def test_a_rule_covered_by_higher_rules_never_wins() -> None:
    def applies(rule: RuleView, when: dt.datetime, connector: str) -> bool:
        return stored_rule(rule).matches(when, connector)

    low = RuleView("low", "p", start=9 * 60, end=10 * 60)
    high = RuleView("high", "q")
    assert model.never_winning([low, high], dt.date(2026, 9, 28), [], applies) == {"low"}
    assert model.never_winning([high, low], dt.date(2026, 9, 28), [], applies) == set()


# -- the real adapter: reading ------------------------------------------------------------------


def _rule_record(rule_id: str, playlist: str, *, connector: str | None = None) -> dict[str, object]:
    return {
        "id": rule_id,
        "playlist_id": playlist,
        "playlist": playlist.title(),
        "connector": connector,
        "enabled": True,
        "selected": True,
        "in_force": True,
    }


def test_routes_and_the_winning_rule_are_the_runtimes(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    session = backend.session
    evening = _playlist(session, "Evening", items[0])
    store = session.schedules
    chosen = store.add(evening, start="01:00", end="02:00")  # does not apply at noon
    store.add(evening)  # applies always, and later: the local answer would be this one
    status = two_display_status(extra={"schedules": [_rule_record(chosen.id, evening)]})
    records = status["displays"]
    assert isinstance(records, list)
    for record in records:
        record["schedule_rule_id"] = chosen.id
    backend.status_model.adopt(status)
    adapter, _owner = _scheduling(backend)
    routes = adapter.routes()
    assert [route.connector for route in routes] == ["DP-1", "HDMI-A-1"]
    assert {route.rule_id for route in routes} == {chosen.id}
    assert all(route.route == "schedule" for route in routes)
    truth = adapter.rule_truth(chosen.id)
    assert truth is not None and truth.in_force


def test_nothing_is_said_about_now_without_the_runtime(backend: Publishing) -> None:
    adapter, _owner = _scheduling(backend)
    assert adapter.routes() == ()
    backend.status_model.mark_unavailable(forget=True)
    assert adapter.routes() == ()


def test_known_displays_keep_the_detached_ones(backend: Publishing) -> None:
    session = backend.session
    session.displays.assign("DP-2", "anything")
    session.schedules.add("anything", connector="eDP-1")
    backend.status_model.adopt(two_display_status())
    adapter, _owner = _scheduling(backend)
    known = {display.connector: display.connected for display in adapter.known_displays()}
    assert known == {"DP-1": True, "HDMI-A-1": True, "DP-2": False, "eDP-1": False}


def test_the_adapter_names_what_changed(backend: Publishing, items: tuple[MediaItem, ...]) -> None:
    adapter, _owner = _scheduling(backend)
    view = backend.status_model.view
    assert set(adapter.adopt(view)) == {"routes", "schedule", "displays"}
    assert adapter.adopt(view) == [], "the poll repeats; nothing changed"
    backend.session.schedules.add(_playlist(backend.session, "Edited by ctl", items[0]))
    assert adapter.adopt(view) == ["schedule"], "an edit from elsewhere is noticed"


# -- the real adapter: writing ------------------------------------------------------------------


def test_a_rule_is_saved_in_the_stores_words(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Weekend", items[0])
    adapter, owner = _scheduling(backend)
    seen: list[bool] = []
    draft = RuleView(
        "", pid, weekdays=frozenset({5, 6}), months=frozenset({12}), start=540, end=1140
    )
    adapter.save_rule("", draft, seen.append)
    assert seen == [] and backend.session.schedules.rules == (), "nothing before the lane runs"
    backend.run_authoring()
    assert seen == [True] and "schedule" in owner.topics
    stored = json.loads(schedules.state_path().read_text())["rules"][0]
    assert stored["weekdays"] == ["sat", "sun"]
    assert stored["months"] == [12]
    assert (stored["start"], stored["end"]) == ("09:00", "19:00")
    assert backend.published == 1


def test_a_new_rule_can_start_switched_off(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Later", items[0])
    adapter, _owner = _scheduling(backend)
    adapter.save_rule("", RuleView("", pid, enabled=False))
    backend.run_authoring()
    assert [rule.enabled for rule in backend.session.schedules.rules] == [False]


def test_an_edit_keeps_the_rules_place_name_and_switch(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    store = backend.session.schedules
    first = store.add(pid)
    store.add(pid)
    store.set_name(first.id, "Mornings")
    adapter, _owner = _scheduling(backend)
    view = adapter.schedule_rule(first.id)
    assert view is not None and view.name == "Mornings"
    adapter.save_rule(first.id, replace(view, start=420, end=600))
    backend.run_authoring()
    rule = store.rules[0]
    assert (rule.id, rule.name, rule.start, rule.end) == (first.id, "Mornings", 420, 600)


def test_a_window_that_starts_and_ends_together_never_reaches_the_store(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Same", items[0])
    adapter, _owner = _scheduling(backend)
    seen: list[bool] = []
    adapter.save_rule("", RuleView("", pid, start=600, end=600), seen.append)
    assert seen == [False], "refused before anything was queued"
    assert not backend.authoring
    assert not schedules.state_path().exists()
    assert "same time" in backend.reports[-1]


def test_an_empty_playlist_is_refused_for_a_rule_an_assignment_and_the_default(
    backend: Publishing,
) -> None:
    empty = backend.session.playlists.create("Nothing yet").id
    adapter, _owner = _scheduling(backend)
    results: list[bool] = []
    adapter.save_rule("", RuleView("", empty), results.append)
    adapter.assign_display("DP-1", empty, results.append)
    adapter.choose_default_playlist(empty, results.append)
    assert results == [False, False, False]
    assert not backend.authoring and backend.settings_changes == []
    assert all("nothing to play yet" in report for report in backend.reports)


def test_a_playlist_emptied_while_queued_is_refused_at_the_head_of_the_lane(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    session = backend.session
    pid = _playlist(session, "Soon empty", items[0])
    adapter, owner = _scheduling(backend)
    results: list[bool] = []
    adapter.assign_display("DP-1", pid, results.append)
    adapter.save_rule("", RuleView("", pid), results.append)
    entry = session.playlists.get(pid)
    assert entry is not None
    session.playlists.remove_entry(pid, entry.entries[0].id)
    backend.run_authoring()
    assert results == [False, False]
    assert session.displays.all() == () and session.schedules.rules == ()
    assert "displays" in owner.topics and "schedule" in owner.topics, "the pages reconcile"


def test_a_failed_save_settles_once_and_says_nothing_changed(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    adapter, owner = _scheduling(backend)
    results: list[bool] = []
    adapter.enable_rule("no-such-rule", False, results.append)
    backend.run_authoring()
    assert results == [False]
    assert "nothing changed" in backend.reports[-1]
    assert owner.topics[-1] == "schedule"
    adapter.save_rule("", RuleView("", pid), results.append)
    backend.run_authoring()
    assert results == [False, True]


def test_a_refusal_without_a_report_still_settles(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    """The lane refuses before queuing (shutting down): the control goes back all the same."""
    pid = _playlist(backend.session, "Day", items[0])
    adapter, owner = _scheduling(backend)
    backend.authoring_action_async = lambda *_args, **_kwargs: False  # type: ignore[method-assign]
    results: list[bool] = []
    adapter.save_rule("", RuleView("", pid), results.append)
    assert results == [False] and owner.topics == ["schedule"]


def test_moves_and_copies_follow_the_list(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    store = backend.session.schedules
    a, b, c = store.add(pid), store.add(pid), store.add(pid)
    adapter, _owner = _scheduling(backend)
    adapter.shift_rule(a.id, 1)  # one place up the list
    backend.run_authoring()
    assert [rule.id for rule in store.rules] == [b.id, a.id, c.id]
    adapter.copy_rule(b.id)
    backend.run_authoring()
    ids = [rule.id for rule in store.rules]
    assert ids[0] == b.id and ids[2:] == [a.id, c.id], "the copy sits just above the original"
    assert store.rules[1].playlist == pid


def test_the_default_playlist_is_a_settings_edit(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Default", items[0])
    adapter, _owner = _scheduling(backend)
    adapter.choose_default_playlist(pid)
    adapter.choose_default_playlist(runtime_config.FALLBACK_PLAYLIST_ID)
    assert backend.settings_changes == [{"active_playlist": pid}, {"active_playlist": ""}]
    backend.run_authoring()
    assert adapter.default_playlist == runtime_config.FALLBACK_PLAYLIST_ID


def test_an_assignment_is_a_display_store_write_and_a_republish(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Desk", items[0])
    adapter, _owner = _scheduling(backend)
    results: list[bool] = []
    adapter.assign_display("DP-2", pid, results.append)
    backend.run_authoring()
    assert results == [True] and backend.published == 1
    assert json.loads(displays.state_path().read_text())["displays"] == {"DP-2": pid}
    adapter.assign_display("DP-2", "", results.append)
    backend.run_authoring()
    assert results == [True, True] and adapter.display_assignments() == {}


def test_beating_the_shared_rules_is_written_beside_the_runtime_configuration(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    """runtime.toml stays byte for byte what the same profile compiles without it."""
    session = backend.session
    pid = _playlist(session, "Desk", items[0])
    session.update_settings(
        replace(
            session.settings,
            display_mode=config.DISPLAY_MODE_INDEPENDENT,
            theme_source_connector="DP-1",
        )
    )
    session.displays.assign("DP-1", pid)
    target = backend.target
    runtime_config.write(session.settings, session, target)
    before = target.read_bytes()
    overrides = runtime_config.overrides_path(target)
    assert not overrides.exists()

    adapter, _owner = _scheduling(backend)
    results: list[bool] = []
    adapter.set_beats_global_rules("DP-1", True, results.append)
    backend.run_authoring()
    assert results == [True] and adapter.beats_global_rules("DP-1")
    assert target.read_bytes() == before, "runtime.toml never changes shape"
    document = tomllib.loads(overrides.read_text())
    assert document["displays"] == [{"connector": "DP-1", "beats_global_rules": True}]
    assert json.loads(displays.state_path().read_text())["version"] == displays.PRECEDENCE_VERSION

    adapter.set_beats_global_rules("DP-1", False, results.append)
    backend.run_authoring()
    assert target.read_bytes() == before
    assert not overrides.exists(), "removed once nothing uses it"


def test_a_display_without_its_own_playlist_cannot_beat_the_rules(backend: Publishing) -> None:
    adapter, _owner = _scheduling(backend)
    results: list[bool] = []
    adapter.set_beats_global_rules("DP-1", True, results.append)
    backend.run_authoring()
    assert results == [False] and not adapter.beats_global_rules("DP-1")
    assert "assign one" in backend.reports[-1]


def test_independent_displays_need_a_connected_display(backend: Publishing) -> None:
    adapter, owner = _scheduling(backend)
    results: list[bool] = []
    adapter.choose_display_mode(config.DISPLAY_MODE_INDEPENDENT, results.append)
    assert results == [False] and backend.settings_changes == []
    assert "needs an attached display" in backend.reports[-1]
    assert adapter.saved_display_mode() == config.DISPLAY_MODE_MIRRORED
    assert "displays" in owner.topics, "the toggle goes back"


def test_independent_displays_take_the_first_connected_one_for_colors(
    backend: Publishing,
) -> None:
    backend.status_model.adopt(two_display_status())
    adapter, _owner = _scheduling(backend)
    adapter.choose_display_mode(config.DISPLAY_MODE_INDEPENDENT)
    assert backend.settings_changes == [
        {"display_mode": "independent", "theme_source_connector": "DP-1"}
    ]
    backend.run_authoring()
    assert adapter.saved_display_mode() == "independent"


def test_forgetting_a_detached_display(backend: Publishing, items: tuple[MediaItem, ...]) -> None:
    session = backend.session
    pid = _playlist(session, "Dock", items[0])
    session.displays.assign("DP-2", pid)
    session.schedules.add(pid, connector="DP-2")
    kept = session.schedules.add(pid)
    pick = session.playlists.set_display_singleton("DP-2", items[1].path)  # "Your pick" on DP-2
    backend.status_model.adopt(two_display_status())
    adapter, _owner = _scheduling(backend)
    plan = adapter.forget_plan("DP-2")
    assert (plan.playlist, plan.rules, plan.refused) == (pid, 1, "")
    assert adapter.forget_plan("DP-1").refused, "a connected display is not forgotten"
    results: list[bool] = []
    adapter.forget_display("DP-2", results.append)
    backend.run_authoring()
    assert results == [True]
    assert session.displays.all() == ()
    assert [rule.id for rule in session.schedules.rules] == [kept.id]
    assert session.playlists.get(pick.id) == pick, "a display's pick is never deleted"
    assert "DP-2" not in {display.connector for display in adapter.known_displays()}


def test_the_colors_display_is_not_forgotten_while_displays_run_separately(
    backend: Publishing,
) -> None:
    session = backend.session
    session.update_settings(
        replace(
            session.settings,
            display_mode=config.DISPLAY_MODE_INDEPENDENT,
            theme_source_connector="DP-2",
        )
    )
    adapter, _owner = _scheduling(backend)
    plan = adapter.forget_plan("DP-2")
    assert plan.colors and "choose another display" in plan.refused


def test_a_store_from_a_newer_version_turns_the_writes_off(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    path = schedules.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "rules": []}))
    session = Session(backend.session.settings, owns_playback=False)
    try:
        newer = Publishing(session)
        adapter, _owner = _scheduling(newer)
        blocked = adapter.schedule_blocked()
        assert "schedules.json" in blocked and "newer version" in blocked
        assert adapter.settings_blocked() == blocked
        results: list[bool] = []
        adapter.save_rule("", RuleView("", pid), results.append)
        adapter.assign_display("DP-1", "", results.append)
        assert results == [False, False] and not newer.authoring
        assert json.loads(path.read_text())["version"] == 99, "never rewritten"
    finally:
        session.shutdown()


def test_unknown_settings_turn_the_settings_choices_off(backend: Publishing) -> None:
    backend.unknown_keys = ("from_the_future",)
    adapter, _owner = _scheduling(backend)
    results: list[bool] = []
    adapter.choose_default_playlist(runtime_config.FALLBACK_PLAYLIST_ID, results.append)
    assert results == [False] and backend.settings_changes == []
    assert "read-only" in adapter.settings_blocked()
    assert adapter.schedule_blocked() == "", "rules don't live in settings"


def test_runtime_choices_are_runtime_commands_never_stored(backend: Publishing) -> None:
    session = backend.session
    session.update_settings(
        replace(
            session.settings,
            display_mode=config.DISPLAY_MODE_INDEPENDENT,
            theme_source_connector="DP-1",
        )
    )
    adapter, _owner = _scheduling(backend)
    adapter.set_display_rotation("HDMI-A-1", cycle=False, shuffle=True)
    adapter.use_saved_rotation("HDMI-A-1")
    adapter.play_now("HDMI-A-1", "")
    assert backend.sent == [
        ("HDMI-A-1", "cycle", "off"),
        ("HDMI-A-1", "shuffle", "on"),
        ("HDMI-A-1", "cycle", "default"),
        ("HDMI-A-1", "shuffle", "default"),
        ("HDMI-A-1", "schedule-follow", None),
    ]
    assert not backend.authoring and backend.settings_changes == []
    assert not runtime_config.overrides_path(backend.target).exists()


def test_runtime_choices_wait_for_the_service(backend: Publishing) -> None:
    adapter, _owner = _scheduling(backend, runtime_off="The wallpaper service isn\u2019t running")
    adapter.set_display_rotation("DP-1", cycle=True)
    adapter.play_now("", "")
    assert backend.sent == [] and backend.reports[-1].startswith("The wallpaper service")


def test_the_app_state_offers_scheduling(backend: Publishing) -> None:
    from wall_in_one.ui.next.prefs import UiPrefsKeeper
    from wall_in_one.ui.next.real_state import RealAppState

    keeper = UiPrefsKeeper(backend.window_report)
    state = RealAppState(backend, keeper)
    try:
        assert isinstance(state.scheduling, RealScheduling)
    finally:
        state.close()
        keeper.close()


def test_a_hidden_pick_is_named_not_missing(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    """ "Your pick" playlists are hidden from the lists but a rule can still point at one."""
    session = backend.session
    session.playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, items[0].path)
    session.playlists.set_display_singleton("DP-2", items[1].path)
    adapter, _owner = _scheduling(backend)
    assert adapter.playlist_label(QUICK_CHOICE_ID) == "Your pick"
    display_pick = playlists.display_quick_choice_id("DP-2")
    assert adapter.playlist_label(display_pick) == "Your pick · DP-2"
    assert adapter.playlist_label("gone") is None
    made = _playlist(session, "Evening", items[0])
    assert adapter.playlist_label(made) == "Evening"


def test_settings_and_displays_share_one_rule_for_the_display_mode() -> None:
    """`displays_settings.settings_edit` is what both pages save the mode through."""
    requested = config.Settings()
    independent = {"display_mode": config.DISPLAY_MODE_INDEPENDENT}
    assert settings_edit(requested, independent, []) == NEEDS_AN_ATTACHED_DISPLAY
    assert settings_edit(requested, independent, ["HDMI-A-1", "DP-1"]) == {
        **independent,
        "theme_source_connector": "HDMI-A-1",
    }
    assert settings_edit(requested, {"display_mode": "mirrored"}, []) == {}, "already saved"
    saved = replace(requested, theme_source_connector="DP-2")
    assert settings_edit(saved, independent, []) == independent, "a saved choice is kept"


def test_a_detached_display_is_not_one_to_follow(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    """Only an attached display can be chosen for the colors (classic, DECISIONS)."""
    backend.session.displays.assign("DP-2", _playlist(backend.session, "Dock", items[0]))
    adapter, _owner = _scheduling(backend)
    assert [d.connector for d in adapter.known_displays()] == ["DP-2"]
    results: list[bool] = []
    adapter.choose_display_mode(config.DISPLAY_MODE_INDEPENDENT, results.append)
    assert results == [False] and backend.settings_changes == []
