"""The new interface's Schedule page over the real adapter and a real session.

The page is the one the app ships (`ui.next.schedule`), over `RealAppState`
with the recording application of ``test_next_state``: rules in a real
schedule store under the test's own XDG directories, runtime answers
through the real status model. Each write waits on the application's lane
until the test runs it, so what the page shows before and after the store
answers can be told apart.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_next_schedule_mapping import Publishing, _playlist  # noqa: E402
from tests.test_next_state import _library, _session  # noqa: E402
from tests.test_next_status_line import two_display_status  # noqa: E402
from wall_in_one import config  # noqa: E402
from wall_in_one.library import schedules  # noqa: E402
from wall_in_one.library.model import MediaItem  # noqa: E402
from wall_in_one.session import QUICK_CHOICE_ID, QUICK_CHOICE_NAME, Session  # noqa: E402
from wall_in_one.ui.next import schedule  # noqa: E402
from wall_in_one.ui.next.prefs import UiPrefsKeeper  # noqa: E402
from wall_in_one.ui.next.real_state import RealAppState  # noqa: E402
from wall_in_one.ui.next.schedule import SchedulePage  # noqa: E402
from wall_in_one.ui.next.schedule_editor import RuleEditor  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


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


class Shown:
    """One Schedule page in a window, over the real adapter."""

    def __init__(self, backend: Publishing) -> None:
        self.backend = backend
        self.keeper = UiPrefsKeeper(backend.window_report)
        self.state = RealAppState(backend, self.keeper)
        page = schedule.create(self.state)
        assert isinstance(page, SchedulePage)
        self.page = page
        self.window = Adw.Window(content=page.widget)
        self.window.set_default_size(1400, 1000)
        self.window.present()
        spin_until(page.widget.get_mapped, 5, what="the page on screen")

    def settle(self) -> None:
        """Run the lane, then what the page queued for after it."""
        self.backend.run_authoring()
        self.page.flush()

    def close(self) -> None:
        self.window.destroy()
        self.state.close()
        self.keeper.close()


@pytest.fixture
def shown(backend: Publishing) -> Iterator[Shown]:
    view = Shown(backend)
    try:
        yield view
    finally:
        view.close()


def _editor(page: SchedulePage) -> RuleEditor | None:
    """The editor open now (read afresh: mypy would keep an earlier answer)."""
    return page.editor


def _not_today() -> frozenset[int]:
    return frozenset({(dt.datetime.now().weekday() + 3) % 7})


def _status_choosing(rule_id: str, playlist: str) -> dict[str, object]:
    status = two_display_status(
        extra={
            "display_mode": "mirrored",
            "schedules": [
                {
                    "id": rule_id,
                    "playlist_id": playlist,
                    "playlist": "Chosen",
                    "connector": None,
                    "enabled": True,
                    "selected": True,
                    "in_force": True,
                }
            ],
        }
    )
    records = status["displays"]
    assert isinstance(records, list)
    for record in records:
        record["schedule_rule_id"] = rule_id
        record["playlist_id"] = playlist
        record["playlist"] = "Chosen"
    return status


def test_the_list_shows_the_highest_priority_first(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    store = backend.session.schedules
    a, b, c = store.add(pid), store.add(pid), store.add(pid)
    view = Shown(backend)
    try:
        assert [row.rule.id for row in view.page.rule_rows] == [c.id, b.id, a.id]
        view.page.move_rule(a.id, 1)  # "Move up": one place higher
        view.settle()
        assert [rule.id for rule in store.rules] == [b.id, a.id, c.id]
        assert [row.rule.id for row in view.page.rule_rows] == [c.id, a.id, b.id]
    finally:
        view.close()


def test_why_names_the_runtimes_rule_not_the_local_answer(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    """Local matching would pick ``later``; the runtime says ``chosen``, and so does the page."""
    session = backend.session
    pid = _playlist(session, "Chosen", items[0])
    store = session.schedules
    chosen = store.add(pid, weekdays=[schedules.WEEKDAY_NAMES[d] for d in _not_today()])
    later = store.add(pid)  # always applies, and last: the local winner
    assert schedules.resolve_rule(store.rules, dt.datetime.now()) == later
    view = Shown(backend)
    try:
        backend.status_model.adopt(_status_choosing(chosen.id, pid))
        view.page.flush()
        page = view.page
        page.build_why()
        reasons = {rule_id: (text, kind) for rule_id, text, kind in page.why_entries if rule_id}
        assert reasons[chosen.id] == ("Playing", "playing")
        assert reasons[later.id] == ("Overridden", "overridden")
        row = page.row(chosen.id)
        assert row is not None and row.now_pill.get_visible()
        other = page.row(later.id)
        assert other is not None and not other.now_pill.get_visible()
    finally:
        view.close()


def test_the_now_card_waits_for_the_runtime(shown: Shown) -> None:
    assert shown.page.outcomes() == []
    assert not shown.page.why_button.get_sensitive()


def test_a_rule_for_one_display_is_kept_and_marked_inactive_while_mirrored(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Dock", items[0])
    rule = backend.session.schedules.add(pid, connector="DP-2")
    assert backend.session.settings.display_mode == config.DISPLAY_MODE_MIRRORED
    view = Shown(backend)
    try:
        row = view.page.row(rule.id)
        assert row is not None
        assert row.note.get_visible()
        assert row.note.get_label() == "Only while displays run separately"
        assert row.has_css_class("off")
        assert not row.warning.get_visible()
    finally:
        view.close()


def test_a_drag_with_no_width_makes_no_rule_and_a_whole_day_is_all_day(shown: Shown) -> None:
    page = shown.page
    page.create_from_calendar([2], 600, 600)
    assert _editor(page) is None
    page.create_from_calendar([2], 0, 24 * 60)
    editor = _editor(page)
    assert editor is not None
    assert (editor.draft.start, editor.draft.end) == (None, None)
    assert editor.draft.weekdays == {2}
    editor.force_close()


def test_the_editor_refuses_a_window_that_starts_and_ends_together(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    _playlist(backend.session, "Day", items[0])
    view = Shown(backend)
    try:
        page = view.page
        page.new_rule(start=540, end=600)
        editor = page.editor
        assert editor is not None and not editor.problem
        editor.to_row.set_value(540)
        assert "same time" in editor.problem
        assert not editor.save_button.get_sensitive()
        editor.commit()
        assert not backend.authoring and backend.session.schedules.rules == ()
        editor.force_close()
    finally:
        view.close()


def test_the_editor_saves_and_closes_only_once_saved(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    view = Shown(backend)
    try:
        page = view.page
        page.new_rule(weekdays=frozenset({0, 1, 2, 3, 4}), start=540, end=1020)
        editor = page.editor
        assert editor is not None and editor.draft.playlist == pid
        editor.commit()
        assert page.editor is editor, "open while the save is queued"
        assert not editor.save_button.get_sensitive()
        view.settle()
        spin_until(lambda: page.editor is None, 5, what="the editor to close")
        (rule,) = backend.session.schedules.rules
        assert (rule.weekdays, rule.start, rule.end) == (frozenset(range(5)), 540, 1020)
        assert [row.rule.id for row in page.rule_rows] == [rule.id]
    finally:
        view.close()


def test_the_editor_refuses_a_playlist_with_nothing_to_play(
    backend: Publishing,
) -> None:
    empty = backend.session.playlists.create("Nothing yet").id
    view = Shown(backend)
    try:
        page = view.page
        page.new_rule()
        editor = page.editor
        assert editor is not None
        editor.playlist_row.set_selected(editor._playlist_ids.index(empty))
        assert "nothing to play yet" in editor.problem
        assert not editor.save_button.get_sensitive()
        editor.force_close()
    finally:
        view.close()


def test_a_failed_switch_goes_back_to_what_is_saved(
    backend: Publishing, items: tuple[MediaItem, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    rule = backend.session.schedules.add(pid)
    view = Shown(backend)
    try:
        page = view.page
        row = page.row(rule.id)
        assert row is not None and row.switch.get_active()

        def refuse(_rule_id: str, _enabled: bool) -> schedules.Rule:
            raise schedules.ScheduleError("local-io", "the disk is full")

        monkeypatch.setattr(backend.session.schedules, "set_enabled", refuse)
        row.switch.set_active(False)
        page.flush()
        shown = page.row(rule.id)
        assert shown is not None and not shown.switch.get_active(), "the queued choice shows"
        view.settle()
        shown = page.row(rule.id)
        assert shown is not None and shown.switch.get_active(), "back on what is saved"
        assert backend.session.schedules.rules[0].enabled
        assert "nothing changed" in backend.reports[-1]
    finally:
        view.close()


def test_the_last_queued_switch_decides_what_shows(
    backend: Publishing, items: tuple[MediaItem, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Off then on, the first failing: the second still saves, and the switch shows it."""
    pid = _playlist(backend.session, "Day", items[0])
    store = backend.session.schedules
    rule = store.add(pid)
    store.set_enabled(rule.id, False)
    view = Shown(backend)
    try:
        page = view.page
        real = store.set_enabled
        calls: list[bool] = []

        def first_fails(rule_id: str, enabled: bool) -> schedules.Rule:
            calls.append(enabled)
            if len(calls) == 1:
                raise schedules.ScheduleError("local-io", "the disk is full")
            return real(rule_id, enabled)

        monkeypatch.setattr(store, "set_enabled", first_fails)
        row = page.row(rule.id)
        assert row is not None
        row.switch.set_active(True)
        row.switch.set_active(False)
        row.switch.set_active(True)
        view.settle()
        assert store.rules[0].enabled
        shown = page.row(rule.id)
        assert shown is not None and shown.switch.get_active()
    finally:
        view.close()


def test_the_default_goes_back_when_its_save_fails(
    backend: Publishing, items: tuple[MediaItem, ...], monkeypatch: pytest.MonkeyPatch
) -> None:
    pid = _playlist(backend.session, "Default", items[0])
    view = Shown(backend)
    try:
        page = view.page
        durable = backend.session.settings.active_playlist
        assert page._fallback_ids[page.fallback_row.get_selected()] == "all-media"

        def fail(**changes: object) -> bool:
            on_error = changes.pop("on_error")
            changes.pop("on_success", None)
            backend.authoring.append(lambda: on_error("settings.toml is read-only"))  # type: ignore[operator]
            return True

        monkeypatch.setattr(backend, "update_settings_async", fail)
        page.fallback_row.set_selected(page._fallback_ids.index(pid))
        assert page._fallback_ids[page.fallback_row.get_selected()] == pid, "queued"
        view.settle()
        assert backend.session.settings.active_playlist == durable
        assert page._fallback_ids[page.fallback_row.get_selected()] == "all-media"
    finally:
        view.close()


def test_a_newer_schedule_file_turns_the_page_read_only(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    path = schedules.state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 99, "rules": []}))
    session = Session(backend.session.settings, owns_playback=False)
    newer = Publishing(session)
    view = Shown(newer)
    try:
        page = view.page
        assert page.blocked_label.get_visible()
        assert "newer version" in page.blocked_label.get_label()
        assert not page.add_button.get_sensitive()
    finally:
        view.close()
        session.shutdown()


def test_the_editor_keeps_a_named_rule_s_name(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    pid = _playlist(backend.session, "Day", items[0])
    store = backend.session.schedules
    rule = store.add(pid)
    store.set_name(rule.id, "School days")
    view = Shown(backend)
    try:
        row = view.page.row(rule.id)
        assert row is not None
        view.page.edit_rule(row.rule)
        editor = view.page.editor
        assert editor is not None
        editor.all_day.set_active(False)
        editor.from_row.set_value(480)
        editor.to_row.set_value(900)
        editor.commit()
        view.settle()
        (saved,) = store.rules
        assert (saved.name, saved.start, saved.end) == ("School days", 480, 900)
    finally:
        view.close()


def test_a_rule_on_your_pick_is_named_not_missing(
    backend: Publishing, items: tuple[MediaItem, ...]
) -> None:
    """A profile's disabled rule on the hidden "Your pick" playlist reads as such."""
    session = backend.session
    session.playlists.set_singleton(QUICK_CHOICE_ID, QUICK_CHOICE_NAME, items[0].path)
    rule = session.schedules.add(QUICK_CHOICE_ID)
    session.schedules.set_enabled(rule.id, False)
    view = Shown(backend)
    try:
        page = view.page
        row = page.row(rule.id)
        assert row is not None
        assert not row.note.get_visible(), row.note.get_label()
        assert not row.switch.get_active()
        page.edit_rule(row.rule)
        editor = page.editor
        assert editor is not None
        assert editor._name.get_label() == "Your pick"
        assert QUICK_CHOICE_ID in editor._playlist_ids
        editor.force_close()
    finally:
        view.close()
