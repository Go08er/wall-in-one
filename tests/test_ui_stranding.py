"""A saved write whose window follow-up raises never strands what the gesture holds.

Since Z-1 a task whose write landed but whose ``finish`` raised (the window,
say) is said as "Saved, but it couldn't be applied" and is never given to
the caller's failure path. A caller that released what its gesture holds (a
pending flag, a queued intent, a waiting control) only there, or in
``finish`` after a call that can raise, kept it for good: the Quick choice
refused every later Apply as "A playback command is already in progress"
until restart (the review's repro, `test_ui_quick_choice_*`). Each such site
now settles first, before anything that reaches the window.

For each site that holds in-flight state: one raise is injected in a window
call its ``finish`` reaches; nothing may stay held, and the next gesture is
accepted.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_next_displays import LAYOUT  # noqa: E402
from tests.test_ui_next_editing import first_scan, wallpaper  # noqa: E402
from tests.test_ui_next_slice import finish, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    sandboxed_runtime,
    settled,
    toolkit,
)
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from tests.test_ui_tidy import _idle, section  # noqa: E402,F401
from wall_in_one import config, runtime_config  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import displays, playlists, schedules  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.theme import palettes  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next import real_schedule  # noqa: E402
from wall_in_one.ui.next.displays import DisplaysPage  # noqa: E402
from wall_in_one.ui.next.real_settings import RealSettings, _colors, _Editing  # noqa: E402
from wall_in_one.ui.next.schedule import SchedulePage  # noqa: E402
from wall_in_one.ui.next.schedule_editor import RuleEditor  # noqa: E402
from wall_in_one.ui.next.settings_state import KEY_SAVED, PaletteDraft  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.schedules_page import SchedulesPage  # noqa: E402


class _PlaylistsChangedFailsOnce(FakeWindow):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def playlists_changed(self, _session: object) -> None:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("the window could not redraw the playlists")


def _raises_once(
    target: object, name: str, monkeypatch: pytest.MonkeyPatch, *, on_call: int = 1
) -> list[int]:
    """``target.name`` raises once, on call ``on_call``; otherwise it does what it did."""
    real: Callable[..., Any] = getattr(target, name)
    calls: list[int] = []

    def once(*arguments: Any, **keywords: Any) -> Any:
        calls.append(1)
        if len(calls) == on_call:
            raise RuntimeError(f"injected: {name} failed")
        return real(*arguments, **keywords)

    monkeypatch.setattr(target, name, once)
    return calls


def _two_wallpapers(application: Application, tmp_path: Path) -> tuple[MediaItem, MediaItem]:
    root = tmp_path / "walls"
    root.mkdir()
    first = MediaItem(root / "first.png", Kind.STILL, 1, 1)
    second = MediaItem(root / "second.png", Kind.STILL, 1, 1)
    for item in (first, second):
        item.path.write_bytes(b"x")
    application.session.adopt_library(Library((root,), (first, second)))
    return first, second


def _runtime_answers(application: Application, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(application, "_publish_runtime_async", lambda: True)
    monkeypatch.setattr(application, "_execute_gui_runtime_call", lambda work: (work(), True))
    monkeypatch.setattr(application, "_send_gui_runtime", lambda *_a, **_k: Response.success())
    monkeypatch.setattr(application, "_send_gui_runtime_on", lambda *_a, **_k: Response.success())


@pytest.mark.parametrize("display", [None, "DP-1"], ids=["all-displays", "one-display"])
def test_ui_quick_choice_whose_finish_raised_is_not_left_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, display: str | None
) -> None:
    """The review's repro, for both Quick choices."""
    application = _application(tmp_path, monkeypatch)
    window = _PlaylistsChangedFailsOnce()
    _attach(application, window)
    first, second = _two_wallpapers(application, tmp_path)
    _runtime_answers(application, monkeypatch)

    def play(item: MediaItem) -> bool:
        if display is None:
            return application.play_item_async(item)
        return application.play_item_on_async(item, display)

    try:
        assert play(first), window.reports
        spin_until(lambda: not application._authoring_active, 5)
        assert any("couldn't be applied" in report for report in window.reports), window.reports
        # Before: held for good, every later Apply refused.
        assert not application._quick_choice_pending, "Quick choice stays pending for good"
        assert play(second), "the next Apply is accepted"
        spin_until(
            lambda: not application._authoring_active and not application._runtime_action_pending, 5
        )
        assert not application._quick_choice_pending
    finally:
        _close(application)


# -- the new window's scheduling adapter and the classic Schedules page --------------------


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeRuntime:
    return sandboxed_runtime(monkeypatch, tmp_path)


Gesture = Callable[[Application, NextWindow, Callable[[bool], None]], None]


def _enable_rule(application: Application, window: NextWindow, settled: Any) -> None:
    rule = application.session.schedules.rules[0]
    scheduling = window.state.scheduling
    assert scheduling is not None
    scheduling.enable_rule(rule.id, not rule.enabled, settled)


def _default_playlist(application: Application, window: NextWindow, settled: Any) -> None:
    scheduling = window.state.scheduling
    assert scheduling is not None
    dock = application.session.playlists.by_name("Dock")
    assert dock is not None
    current = application.settings.active_playlist
    fallback = runtime_config.FALLBACK_PLAYLIST_ID
    scheduling.choose_default_playlist(fallback if current == dock.id else dock.id, settled)


def _assign_display(application: Application, window: NextWindow, settled: Any) -> None:
    scheduling = window.state.scheduling
    assert scheduling is not None
    dock = application.session.playlists.by_name("Dock")
    assert dock is not None
    assigned = dict(application.session.displays.all()).get("DP-1")
    scheduling.assign_display("DP-1", "" if assigned == dock.id else dock.id, settled)


def _forget_display(application: Application, window: NextWindow, settled: Any) -> None:
    scheduling = window.state.scheduling
    assert scheduling is not None
    dock = application.session.playlists.by_name("Dock")
    assert dock is not None
    application.session.displays.assign("DP-2", dock.id)
    scheduling.forget_display("DP-2", settled)


ADAPTER_SITES: list[tuple[str, Gesture, str]] = [
    ("next-rule-switch", _enable_rule, "schedule_edited"),
    ("next-default-playlist", _default_playlist, "schedule_edited"),
    ("next-display-playlist", _assign_display, "runtime_config_changed"),
    ("next-forget-display", _forget_display, "schedule_edited"),
]


@pytest.mark.parametrize(
    ("gesture", "raising"),
    [pytest.param(gesture, raising, id=name) for name, gesture, raising in ADAPTER_SITES],
)
def test_a_scheduling_write_whose_window_follow_up_raised_is_settled(
    runtime: FakeRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gesture: Gesture,
    raising: str,
) -> None:
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        dock = application.session.playlists.create(
            "Dock",
            entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
        )
        application.session.schedules.add(dock.id, weekdays=("mon",))
        application.schedule_edited()
        yield "the setup to be published", lambda: settled(application)
        calls = _raises_once(application, raising, monkeypatch)
        outcomes: list[bool] = []
        gesture(application, window, outcomes.append)
        yield "the write to land", lambda: settled(application) and bool(outcomes)
        assert calls, f"{raising} was reached, and raised"
        # Before: never settled, the control waiting for good.
        assert len(outcomes) == 1, outcomes
        gesture(application, window, outcomes.append)
        yield "the next gesture", lambda: settled(application) and len(outcomes) == 2
        assert outcomes[1] is True, "the next gesture is accepted and saves"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_classic_schedules_display_and_rule_switch_settle_when_their_follow_up_raised(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic page's two writes that hold a queued intent per control."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        session = application.session
        dock = session.playlists.create(
            "Dock",
            entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
        )
        rule = session.schedules.add(dock.id, weekdays=("mon",))
        application.schedule_edited()
        yield "the setup to be published", lambda: settled(application)
        page = SchedulesPage(application)
        page.refresh(session)

        choices = (dock,)
        row = Adw.ComboRow(model=Gtk.StringList.new(["Default", dock.name]))
        assigned = page._make_display_changed("DP-1", choices)
        calls = _raises_once(application, "runtime_config_changed", monkeypatch)
        row.set_selected(1)
        assigned(row, None)
        yield "the assignment to land", lambda: settled(application)
        assert calls, "runtime_config_changed was reached, and raised"
        # Before: the row's queued intent stayed, so it never showed what is saved again.
        assert page._pending_assignments == {}, page._pending_assignments
        row.set_selected(0)
        assigned(row, None)
        yield "the next assignment", lambda: settled(application) and not page._pending_assignments
        assert dict(displays.Store.open().all()) == {}

        switch = Adw.SwitchRow(active=False)
        enabled = page._make_enabled(rule.id)
        calls = _raises_once(application, "schedule_edited", monkeypatch)
        enabled(switch, None)
        yield "the switch to land", lambda: settled(application)
        assert calls, "schedule_edited was reached, and raised"
        assert page._pending_enabled == {}, page._pending_enabled
        switch.set_active(True)
        enabled(switch, None)
        yield "the next switch", lambda: settled(application) and not page._pending_enabled
        assert schedules.Store.open().rules[0].enabled
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


# -- the new window's Settings: palette editor and API key ---------------------------------


def test_a_palette_save_and_a_key_save_whose_report_raised_are_settled(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Their ``done`` came after a report (which reaches the window): the editor kept
    "Saving…" and the key row stayed disabled when that report raised."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        access = window.state.settings_access
        assert isinstance(access, RealSettings)
        document = {
            "dark": {key: "#203040" for key, _label in palettes.EDITABLE_KEYS},
            "light": {key: "#eeeeee" for key, _label in palettes.EDITABLE_KEYS},
        }
        draft = PaletteDraft(
            title="New palette",
            name="Mine",
            renamable=True,
            colors=_colors(document),
            origin=_Editing(document),
        )
        manager = access.palette_manager
        manager.open_palettes()

        def listed(name: str) -> Callable[[], bool]:
            return lambda: (
                manager.palette_list().phase == "ready"
                and any(item.name == name for item in manager.palette_list().items)
            )

        yield "the catalogue", listed("Nord")
        saved: list[bool] = []
        calls = _raises_once(application, "window_report", monkeypatch)
        manager.save_palette_draft(draft, "Mine", draft.colors, saved.append)
        yield "the palette to save", lambda: settled(application) and bool(saved)
        assert calls and saved == [True], saved
        yield "the catalogue after the save", listed("Mine")
        manager.save_palette_draft(draft, "Mine again", draft.colors, saved.append)
        yield "the next save", lambda: settled(application) and len(saved) == 2
        assert saved == [True, True]

        keys: list[str] = []
        calls = _raises_once(application, "window_report", monkeypatch)
        access.save_key("a" * 32, keys.append)
        yield "the key to save", lambda: settled(application) and bool(keys)
        assert calls and keys == [KEY_SAVED], keys
        access.save_key("b" * 32, keys.append)
        yield "the next key", lambda: settled(application) and len(keys) == 2
        assert keys == [KEY_SAVED, KEY_SAVED]
        manager.close_palettes()
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


# -- the settings batch and the runtime call -----------------------------------------------


class _AdoptionFailsOnce(FakeWindow):
    def __init__(self) -> None:
        super().__init__()
        self.applied = 0

    def apply_settings(self, _settings: config.Settings) -> None:
        self.applied += 1
        if self.applied == 1:
            raise RuntimeError("the window could not take it")


def test_a_saved_batch_settles_its_callers_before_publishing_what_was_saved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Adoption fails, then publishing what was saved fails too: callers still hear it."""
    application = _application(tmp_path, monkeypatch)
    window = _AdoptionFailsOnce()
    _attach(application, window)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    # Its first call is adoption's own; the second, what was saved being shown.
    calls = _raises_once(application, "_publish_runtime_for_context", monkeypatch, on_call=2)
    outcomes: list[tuple[str, object]] = []
    try:
        assert application.update_settings_async(
            opacity=0.7,
            on_success=lambda saved: outcomes.append(("saved", saved.opacity)),
            on_error=lambda message: outcomes.append(("failed", message)),
        )
        spin_until(lambda: not application._settings_authoring_running, 5)
        assert len(calls) >= 2, "publishing what was saved was reached, and raised"
        assert outcomes == [("saved", 0.7)], outcomes
        assert application.update_settings_async(
            opacity=0.4, on_success=lambda saved: outcomes.append(("saved", saved.opacity))
        )
        spin_until(lambda: not application._settings_authoring_running, 5)
        assert outcomes[-1] == ("saved", 0.4)
    finally:
        _close(application)


def test_a_batch_that_did_not_save_settles_its_callers_before_the_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused write, then the window can't take the settings put back: callers hear it."""
    application = _application(tmp_path, monkeypatch)
    window = _AdoptionFailsOnce()
    _attach(application, window)
    real_update = config.update
    refusals: list[int] = []

    def update(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
        if not refusals:
            refusals.append(1)
            raise config.ConfigError("cannot save settings.toml: No space left on device")
        return real_update(changes, *arguments, **keywords)

    monkeypatch.setattr(config, "update", update)
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    outcomes: list[str] = []
    try:
        assert application.update_settings_async(
            opacity=0.7,
            on_success=lambda _saved: outcomes.append("saved"),
            on_error=lambda _message: outcomes.append("failed"),
        )
        spin_until(lambda: not application._settings_authoring_running and bool(outcomes), 5)
        assert outcomes == ["failed"] and window.applied == 1
        assert application.update_settings_async(
            opacity=0.4, on_success=lambda _saved: outcomes.append("saved")
        )
        spin_until(lambda: not application._settings_authoring_running, 5)
        assert outcomes == ["failed", "saved"]
    finally:
        _close(application)


class _ShownFailsOnce(FakeWindow):
    """The redraw after the runtime answered (``shown``) fails once."""

    def __init__(self) -> None:
        super().__init__()
        self.calls = 0

    def playlists_changed(self, _session: object) -> None:
        self.calls += 1
        if self.calls == 2:
            raise RuntimeError("the window could not redraw the playlists")


def test_a_runtime_call_whose_success_redraw_raised_still_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``on_complete`` and the busy flag came after ``on_success``, which reaches the window."""
    application = _application(tmp_path, monkeypatch)
    window = _ShownFailsOnce()
    _attach(application, window)
    first, second = _two_wallpapers(application, tmp_path)
    _runtime_answers(application, monkeypatch)
    try:
        assert application.play_item_async(first)
        spin_until(lambda: not application._authoring_active and window.calls >= 2, 5)
        assert not application._runtime_action_pending and not application._quick_choice_pending
        assert not application.status_model.view.busy
        assert application.play_item_async(second), "the next Apply is accepted"
    finally:
        _close(application)


# -- AA-3: the native pages' own completion callbacks --------------------------------------
#
# The adapters settle first (above); each page's own callback is a follow-up
# too. These go through the real page, injecting one raise in its toast.


def _dock(application: Application, tmp_path: Path) -> playlists.Playlist:
    dock = application.session.playlists.create(
        "Dock",
        entries=[playlists.Entry(playlists.new_id(), wallpaper(tmp_path, "dawn.png"))],
    )
    application.playlists_changed()
    return dock


def _editor(page: SchedulePage) -> RuleEditor | None:
    """The editor open now (read afresh: mypy would keep an earlier answer)."""
    return page.editor


def test_aa3_a_rule_editor_whose_added_toast_raised_is_released(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra's repro: Add a valid rule, the first success toast raises once."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        _dock(application, tmp_path)
        yield "the playlist to be published", lambda: settled(application)
        page = window.pages["schedule"]
        assert isinstance(page, SchedulePage)
        page.new_rule(weekdays=frozenset({0, 1, 2, 3, 4}), start=540, end=1020)
        editor = page.editor
        assert editor is not None
        calls = _raises_once(window.state, "toast", monkeypatch)
        editor.commit()
        yield "the rule to save", lambda: settled(application) and bool(calls)
        assert len(schedules.Store.open().rules) == 1, "the rule was saved"
        # Before: "Saving…" for good, Save off, and commit() did nothing.
        assert not editor._saving, "the editor still holds the save"
        assert _editor(page) is None, "a saved rule's editor closes"

        page.new_rule(weekdays=frozenset({5, 6}), start=600, end=660)
        again = _editor(page)
        assert again is not None
        again.commit()
        yield "the next rule", lambda: settled(application) and _editor(page) is None
        assert len(schedules.Store.open().rules) == 2
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_aa3_the_schedule_fallback_whose_toast_raised_is_settled(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The page's own queued default choice, released before its toast."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        dock = _dock(application, tmp_path)
        yield "the playlist to be published", lambda: settled(application)
        page = window.pages["schedule"]
        assert isinstance(page, SchedulePage)
        page.flush()
        before = page._fallback_ids[page.fallback_row.get_selected()]
        calls = _raises_once(window.state, "toast", monkeypatch)
        page.fallback_row.set_selected(page._fallback_ids.index(dock.id))
        yield "the default to save", lambda: settled(application) and bool(calls)
        assert application.settings.active_playlist == dock.id
        # Before: the queued choice stayed, so the row never showed what is saved again.
        assert page._pending_default == {}, page._pending_default

        page.fallback_row.set_selected(page._fallback_ids.index(before))
        yield "the next default", lambda: settled(application) and not page._pending_default
        page.flush()
        assert page._fallback_ids[page.fallback_row.get_selected()] == before
        assert config.load_strict().active_playlist != dock.id, "the next choice saved"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def _displays_mode(page: DisplaysPage, first: bool) -> None:
    page.mode_group.set_active_name("independent" if first else "mirrored")


def _displays_plays(page: DisplaysPage, first: bool) -> None:
    page.demo("select:HDMI-A-1")
    dock = next(pid for pid in page._plays_ids if pid and pid != page._plays_ids[0])
    page.plays_row.set_selected(page._plays_ids.index(dock) if first else 0)


def _displays_colors(page: DisplaysPage, first: bool) -> None:
    page.use_for_colors("HDMI-A-1" if first else "DP-1")


DISPLAYS_SITES: list[tuple[str, Callable[[DisplaysPage, bool], None], str, bool]] = [
    ("mode", _displays_mode, "_pending_mode", False),
    ("plays", _displays_plays, "_pending_assign", True),
    ("colors", _displays_colors, "_pending_colors", True),
]


@pytest.mark.parametrize(
    ("gesture", "pending", "independent"),
    [pytest.param(g, p, i, id=name) for name, g, p, i in DISPLAYS_SITES],
)
def test_aa3_a_displays_control_whose_toast_raised_is_settled(
    runtime: FakeRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gesture: Callable[[DisplaysPage, bool], None],
    pending: str,
    independent: bool,
) -> None:
    """Astra's unproved siblings: each Displays control's queued choice, before its toast."""
    monkeypatch.setattr(real_schedule, "_monitor_displays", lambda: list(LAYOUT))
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        _dock(application, tmp_path)
        if independent:
            application.update_settings(
                display_mode=config.DISPLAY_MODE_INDEPENDENT, theme_source_connector="DP-1"
            )
        yield "the setup to be published", lambda: settled(application)
        page = window.pages["displays"]
        assert isinstance(page, DisplaysPage)
        page.refresh()
        page.flush()
        before = (config.load_strict(), dict(displays.Store.open().all()))
        calls = _raises_once(window.state, "toast", monkeypatch)
        gesture(page, True)
        yield "the choice to save", lambda: settled(application) and bool(calls)
        assert (config.load_strict(), dict(displays.Store.open().all())) != before, "saved"
        # Before: the queued choice stayed, so the control never showed what is saved again.
        assert getattr(page, pending) == {}, getattr(page, pending)

        saved = (config.load_strict(), dict(displays.Store.open().all()))
        gesture(page, False)
        yield "the next choice", lambda: settled(application) and not getattr(page, pending)
        assert (config.load_strict(), dict(displays.Store.open().all())) != saved, "accepted"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def _widgets(root: Gtk.Widget) -> Iterator[Gtk.Widget]:
    child = root.get_first_child()
    while child is not None:
        yield child
        yield from _widgets(child)
        child = child.get_next_sibling()


def _default_row(page: SchedulesPage) -> Adw.ComboRow:
    """The Default rotation group's playlist row (rebuilt by each refresh)."""
    (group,) = [
        widget
        for widget in _widgets(page)
        if isinstance(widget, Adw.PreferencesGroup) and widget.get_title() == "Default rotation"
    ]
    (row,) = [widget for widget in _widgets(group) if isinstance(widget, Adw.ComboRow)]
    return row


def test_aa3_classic_default_playlist_not_saved_is_put_back_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The classic Schedules page said a refused default before putting its row back."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        session = application.session
        _dock(application, tmp_path)
        yield "the setup to be published", lambda: settled(application)
        page = SchedulesPage(application)
        page.refresh(session)
        real_update = config.update
        refusals: list[int] = []

        def update(changes: dict[str, Any], *arguments: Any, **keywords: Any) -> config.Settings:
            if not refusals:
                refusals.append(1)
                raise config.ConfigError("cannot save settings.toml: No space left on device")
            return real_update(changes, *arguments, **keywords)

        monkeypatch.setattr(config, "update", update)
        calls = _raises_once(application, "window_report", monkeypatch)
        _default_row(page).set_selected(1)
        yield "the refusal", lambda: settled(application) and bool(calls)
        assert application.settings.active_playlist == ""
        # Before: the row kept showing the refused choice as the default.
        assert _default_row(page).get_selected() == 0, "put back on what is saved"

        _default_row(page).set_selected(1)
        yield (
            "the next choice",
            lambda: settled(application) and bool(application.settings.active_playlist),
        )
        assert config.load_strict().active_playlist == session.playlists.by_name("Dock").id  # type: ignore[union-attr]
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
