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

from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_ui_authoring_permanence import _session as _permanence_session  # noqa: E402
from tests.test_ui_next_displays import LAYOUT  # noqa: E402
from tests.test_ui_next_editing import (  # noqa: E402
    durable,
    first_scan,
    inspector_of,
    item_at,
    wallpaper,
)
from tests.test_ui_next_settings import (  # noqa: E402
    NOCTALIA,
    Opened,
    _noctalia_settings,
    _template_state,
)
from tests.test_ui_next_settings import _close as _close_settings  # noqa: E402
from tests.test_ui_next_settings import _open as _open_settings  # noqa: E402
from tests.test_ui_next_slice import finish, library_page, run  # noqa: E402
from tests.test_ui_next_window import (  # noqa: E402,F401
    FakeRuntime,
    Step,
    sandboxed_runtime,
    settled,
    toolkit,
)
from tests.test_ui_pairing_picker import (  # noqa: E402
    QuietPreviewLoader,
    QuietThumbnailLoader,
    _mode_index,
)
from tests.test_ui_playlists_polish import QuietLoader  # noqa: E402
from tests.test_ui_runtime import FakeWindow, _application, _attach, _close  # noqa: E402
from tests.test_ui_tidy import _idle, section  # noqa: E402,F401
from wall_in_one import config, paths, runtime_config  # noqa: E402
from wall_in_one.control.protocol import Response  # noqa: E402
from wall_in_one.library import displays, pairings, playlists, schedules  # noqa: E402
from wall_in_one.library.model import Kind, Library, MediaItem  # noqa: E402
from wall_in_one.theme import noctalia, palettes  # noqa: E402
from wall_in_one.theme.palette import PaletteError  # noqa: E402
from wall_in_one.ui import pairings_page, playlists_page  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next import real_schedule, real_settings  # noqa: E402
from wall_in_one.ui.next.displays import DisplaysPage  # noqa: E402
from wall_in_one.ui.next.playlists import PlaylistPage  # noqa: E402
from wall_in_one.ui.next.real_settings import RealSettings, _colors, _Editing  # noqa: E402
from wall_in_one.ui.next.schedule import SchedulePage  # noqa: E402
from wall_in_one.ui.next.schedule_editor import RuleEditor  # noqa: E402
from wall_in_one.ui.next.settings_palettes import PaletteEditor, PalettesDialog  # noqa: E402
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


# -- AA-3b: the classic pages put their controls back before a call that can raise ---------


def _rule_shapes(rules: Sequence[schedules.Rule]) -> list[tuple[object, ...]]:
    return [
        (rule.playlist, rule.months, rule.weekdays, rule.start, rule.end, rule.connector)
        for rule in rules
    ]


def test_aa3b_classic_rule_save_whose_publication_raised_cannot_save_it_twice(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor was cleared after publishing: left filled in "new rule" mode when that
    raised, so a second Save added the same rule again. Remove's list, likewise."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        session = application.session
        dock = _dock(application, tmp_path)
        yield "the setup to be published", lambda: settled(application)
        page = SchedulesPage(application)
        page.refresh(session)
        choices = session.playlists.all()
        page._rule_playlist.set_selected([p.id for p in choices].index(dock.id))
        page._weekdays[0].set_active(True)
        calls = _raises_once(application, "schedule_edited", monkeypatch)
        page._rule_commit.emit("clicked")
        yield "the rule to save", lambda: settled(application) and bool(calls)
        assert len(schedules.Store.open().rules) == 1
        cleared = not any(button.get_active() for button in page._weekdays)

        page._rule_commit.emit("clicked")
        yield "a second Save", lambda: settled(application)
        shapes = _rule_shapes(schedules.Store.open().rules)
        # Before: still filled in, in "new rule" mode, so this saved it again.
        assert len(set(shapes)) == len(shapes), f"a duplicate rule: {shapes}"
        assert cleared, "the editor was cleared once the rule saved"

        rows = len(page._rule_rows)
        (first, *_rest) = schedules.Store.open().rules
        calls = _raises_once(application, "schedule_edited", monkeypatch)
        page._make_remove(first.id)(Gtk.Button())
        yield "the removal", lambda: settled(application) and bool(calls)
        assert first.id not in [rule.id for rule in schedules.Store.open().rules]
        assert len(page._rule_rows) == rows - 1 or not schedules.Store.open().rules, (
            "the list shows what is saved"
        )
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def _pairings_page(application: Application, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(pairings_page, "ThumbnailLoader", QuietThumbnailLoader)
    monkeypatch.setattr(pairings_page, "SchemePreviewLoader", QuietPreviewLoader)
    page = pairings_page.PairingsPage(application, lambda: None)
    (item, *_rest) = application.session.library.items
    page.edit(application.session, item)
    return page, item


def test_aa3b_classic_colour_not_saved_is_put_back_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's second nit: it reported, then put the colour controls back."""
    application = Application(ui="next")
    lanes: list[object] = []
    wallpaper(tmp_path, "dawn.png")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        page, item = _pairings_page(application, monkeypatch)
        keep, light = _mode_index(pairings.Mode.KEEP), _mode_index(pairings.Mode.LIGHT)
        assert page._mode_row.get_selected() == keep
        refused = _raises_once(pairings.Store, "choose_mode", monkeypatch)
        reported = _raises_once(application, "window_report", monkeypatch)
        page._mode_row.set_selected(light)
        yield "the refusal", lambda: settled(application) and bool(reported)
        assert refused and page._palette_intent == {}
        # Before: Light stayed selected, though nothing was saved.
        assert page._mode_row.get_selected() == keep, "put back on what is saved"

        page._mode_row.set_selected(light)
        yield "the next choice", lambda: settled(application) and not page._palette_intent
        saved = pairings.Store.open().get(pairings.Identity.of(item))
        assert saved is not None and saved.palette.mode is pairings.Mode.LIGHT
        page.shutdown()
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_aa3b_a_refusal_whose_report_raised_still_lets_the_gesture_settle(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The lane's own refusal (authoring not open) reported through the window: when
    that raised, it raised past the caller, which never settled its queued choice."""
    application = Application(ui="next")
    lanes: list[object] = []
    wallpaper(tmp_path, "dawn.png")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        page, _item = _pairings_page(application, monkeypatch)
        keep, light = _mode_index(pairings.Mode.KEEP), _mode_index(pairings.Mode.LIGHT)
        monkeypatch.setattr(application, "_authoring_migration_ready", False)
        reported = _raises_once(application, "window_report", monkeypatch)
        page._mode_row.set_selected(light)
        assert reported, "the refusal was said, and that raised"
        # Before: the queued choice stayed, and Light with it.
        assert page._palette_intent == {}, page._palette_intent
        assert page._mode_row.get_selected() == keep
        monkeypatch.setattr(application, "_authoring_migration_ready", True)
        page.shutdown()
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_aa3b_a_settings_refusal_whose_report_raised_still_reaches_its_caller(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same refusal on the settings lane: the caller's ``on_error`` puts its control
    back, but saying why raised first, so it never ran."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        _dock(application, tmp_path)
        yield "the setup to be published", lambda: settled(application)
        page = SchedulesPage(application)
        page.refresh(application.session)
        monkeypatch.setattr(application, "_authoring_migration_ready", False)
        reported = _raises_once(application, "window_report", monkeypatch)
        _default_row(page).set_selected(1)
        assert reported, "the refusal was said, and that raised"
        # Before: the row kept showing a default that was never saved.
        assert _default_row(page).get_selected() == 0, "put back on what is saved"
        assert application.settings.active_playlist == ""
        monkeypatch.setattr(application, "_authoring_migration_ready", True)
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def _playback_row(page: SchedulesPage, application: Application, choices: Any) -> Any:
    row = Adw.ComboRow(model=Gtk.StringList.new(["Schedule", *(p.name for p in choices)]))
    changed = page._make_playback_changed(choices)
    return row, changed, "activate_playlist_async", lambda: row.set_selected(1), row.get_selected


def _display_row(page: SchedulesPage, application: Application, choices: Any) -> Any:
    row = Adw.ComboRow(model=Gtk.StringList.new(["Schedule", *(p.name for p in choices)]))
    changed = page._make_display_playback_changed("DP-1", choices)
    return row, changed, "activate_playlist_on_async", lambda: row.set_selected(1), row.get_selected


def _mode_button(page: SchedulesPage, application: Application, choices: Any) -> Any:
    button = Gtk.ToggleButton()
    changed = page._make_display_mode_changed("DP-1", "shuffle")
    return (
        button,
        changed,
        "runtime_action_on_async",
        lambda: button.set_active(True),
        lambda: int(button.get_active()),
    )


@pytest.mark.parametrize(
    "control",
    [
        pytest.param(_playback_row, id="playback"),
        pytest.param(_display_row, id="display-playback"),
        pytest.param(_mode_button, id="display-shuffle"),
    ],
)
def test_aa3b_classic_runtime_controls_go_back_whatever_starting_the_command_did(
    runtime: FakeRuntime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    control: Callable[[SchedulesPage, Application, Any], Any],
) -> None:
    """Runtime truth owns these: each is put back at once, even if the call raised."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        _dock(application, tmp_path)
        yield "the setup to be published", lambda: settled(application)
        page = SchedulesPage(application)
        page.refresh(application.session)
        widget, changed, call, click, shown = control(
            page, application, application.session.playlists.all()
        )
        before = shown()
        calls = _raises_once(application, call, monkeypatch)
        click()
        with pytest.raises(RuntimeError, match="injected"):
            changed(widget, None) if not isinstance(widget, Gtk.ToggleButton) else changed(widget)
        assert calls
        # Before: the optimistic choice stayed, claiming what the runtime never took.
        assert shown() == before, "put back on runtime truth"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_aa3b_a_classic_entry_drag_ends_whatever_queuing_its_move_did(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refusal said through a window that raised left the list mid-drag."""
    monkeypatch.setattr(playlists_page, "ThumbnailLoader", QuietLoader)
    session, playlist, items = _permanence_session(tmp_path)
    session.playlists.add(playlist.id, items[1].path, entry_id="second")

    def refuse(*_arguments: object, **_keywords: object) -> bool:
        raise RuntimeError("injected: the refusal could not be said")

    app = SimpleNamespace(
        session=session, authoring_action_async=refuse, window_report=lambda _m: None
    )
    page = playlists_page.PlaylistsPage(app)  # type: ignore[arg-type]
    page.refresh(session)
    row = page._entry_rows["first"]
    page._begin_drag()
    with pytest.raises(RuntimeError, match="injected"):
        page._commit_row_drag(row, 0, 1)
    # Before: still dragging, so the list refused every later change.
    assert not page._dragging

    def publish_raises() -> None:
        raise RuntimeError("injected: playlists_changed failed")

    app.playlists_changed = publish_raises
    page._begin_drag()
    page._playlist_change_after_drag = True
    page._health_refresh_after_drag = True
    with pytest.raises(RuntimeError, match="injected"):
        page._finish_drag()
    # Before: the health refresh it deferred during the drag was never done.
    assert not page._health_refresh_after_drag
    session.shutdown()


# -- AB-6: the new window's failure paths show what is true before they report -------------
#
# Each failure or refusal said something through the window, then put the
# visible control back (or told the page). A report that raised left the
# control showing what was never saved. One raise is injected in the report;
# what is shown must already be the durable truth, and the next gesture is taken.


def test_ab6_an_inspector_colour_not_saved_goes_back_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra's first repro: disk at the default, the inspector and its view still Light."""
    application = Application(ui="next")
    lanes: list[object] = []
    dusk = wallpaper(tmp_path, "dusk.jpg")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        library_page(window).inspect(window.state.wallpaper(dusk))
        item = item_at(application, dusk)
        theme = inspector_of(window).theme_mode
        assert theme is not None
        before = theme.get_active_name()
        assert before != "light"
        refused = _raises_once(pairings.Store, "choose_mode", monkeypatch)
        reported = _raises_once(application, "window_report", monkeypatch)
        theme.set_active_name("light")
        assert window.state.wallpaper(dusk).theme_mode == "light", "queued: shown until it settles"
        yield "the refused save", lambda: settled(application) and bool(reported)
        assert refused and durable(item) == pairings.PalettePolicy(), "nothing was saved"
        # Before: the inspector and its cached view still said Light.
        assert window.state.wallpaper(dusk).theme_mode == before
        theme = inspector_of(window).theme_mode
        assert theme is not None and theme.get_active_name() == before

        theme.set_active_name("light")
        yield (
            "the next choice",
            lambda: settled(application) and durable(item).mode is pairings.Mode.LIGHT,
        )
        theme = inspector_of(window).theme_mode
        assert theme is not None and theme.get_active_name() == "light"
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_ab6_an_inspector_colour_refused_outright_goes_back_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same control, refused before anything is queued (edits are off)."""
    application = Application(ui="next")
    lanes: list[object] = []
    dusk = wallpaper(tmp_path, "dusk.jpg")

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        library_page(window).inspect(window.state.wallpaper(dusk))
        item = item_at(application, dusk)
        editing = window.state.editing
        assert editing is not None
        theme = inspector_of(window).theme_mode
        assert theme is not None
        before = theme.get_active_name()
        allowed = editing.editing_blocked
        monkeypatch.setattr(editing, "editing_blocked", lambda: "Changes to wallpapers are off")
        reported = _raises_once(application, "window_report", monkeypatch)
        theme.set_active_name("dark")  # its handler raises with the report: GTK logs it
        assert reported and durable(item) == pairings.PalettePolicy()
        # Before: the clicked choice stayed selected.
        theme = inspector_of(window).theme_mode
        assert theme is not None and theme.get_active_name() == before

        monkeypatch.setattr(editing, "editing_blocked", allowed)
        theme.set_active_name("dark")
        yield (
            "the next choice",
            lambda: settled(application) and durable(item).mode is pairings.Mode.DARK,
        )
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def _template_page(monkeypatch: pytest.MonkeyPatch) -> tuple[Opened, list[str]]:
    """The new window's Settings over a Noctalia that has no palette template yet."""
    asked: list[str] = []
    monkeypatch.setattr(noctalia, "reload_config", lambda: asked.append("reload"))

    def apply_templates() -> None:
        if "render refused" not in asked:
            asked.append("render refused")
            raise noctalia.NoctaliaError("its socket went away.")
        asked.append("render")

    monkeypatch.setattr(noctalia, "apply_templates", apply_templates)
    _noctalia_settings(NOCTALIA)
    shown = _open_settings(monkeypatch)
    page = shown.page
    spin_until(
        lambda: _template_state(shown) == "missing" and page._template_button.get_sensitive(),
        10,
        what="the template row's first read",
    )
    return shown, asked


def test_ab6_a_template_install_refused_gives_install_back_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Astra's second repro: the busy bit cleared, Install still off with its spinner."""
    shown, _asked = _template_page(monkeypatch)
    try:
        page = shown.page
        _raises_once(real_settings, "install_template", monkeypatch)
        reported = _raises_once(shown.application, "window_report", monkeypatch)
        page.install_template()  # the button
        assert page._template_spinner.get_visible() and not page._template_button.get_sensitive()
        shown.wait("the refusal", lambda: reported)
        # Before: off, with its spinner, until some other settings signal.
        assert page._template_button.get_sensitive(), "Install is offered again"
        assert not page._template_spinner.get_visible()

        page.install_template()
        shown.wait("the install", lambda: _template_state(shown) == "installed")
    finally:
        _close_settings(shown)


def test_ab6_a_template_render_refused_gives_render_back_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The render sibling: Noctalia refuses, and saying so raises."""
    shown, asked = _template_page(monkeypatch)
    try:
        page = shown.page
        page.install_template()
        shown.wait("the install", lambda: _template_state(shown) == "installed")
        spin_until(
            lambda: page._render_row.get_visible() and page._render_button.get_sensitive(),
            10,
            what="Render now, offered",
        )
        reported = _raises_once(shown.application, "window_report", monkeypatch)
        page.render_templates()  # the button
        assert page._template_spinner.get_visible() and not page._render_button.get_sensitive()
        shown.wait("the refusal", lambda: reported)
        assert asked == ["reload", "render refused"]
        # Before: off, with the spinner, until some other settings signal.
        assert page._render_button.get_sensitive(), "Render now is offered again"
        assert not page._template_spinner.get_visible()

        page.render_templates()
        shown.wait("the render", lambda: "render" in asked)
    finally:
        _close_settings(shown)


def test_ab6_a_playlist_removal_not_saved_releases_its_rows_before_the_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Playlists page holds what it asked to remove, and "Saving…", until it hears."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        dock = _dock(application, tmp_path)
        application.session.playlists.add(dock.id, Path(wallpaper(tmp_path, "dusk.jpg")))
        application.playlists_changed()
        yield "the playlist to be published", lambda: settled(application)
        window.navigate(f"playlist:{dock.id}")
        page = window.pages["playlist"]
        assert isinstance(page, PlaylistPage)
        first, second = application.session.playlists.find(dock.id).entries
        refused = _raises_once(playlists.Store, "remove_entries", monkeypatch)
        reported = _raises_once(application, "window_report", monkeypatch)
        page.remove_entries([first.id])
        assert page._removing == {first.id} and page._saving.get_visible()
        yield "the refused removal", lambda: settled(application) and bool(reported)
        assert refused and len(playlists.Store.open().find(dock.id).entries) == 2
        # Before: the row stayed "removing" under "Saving…", and asking again did nothing.
        assert page._removing == set(), page._removing
        assert not page._saving.get_visible()

        page.remove_entries([first.id])
        yield (
            "the next removal",
            lambda: (
                settled(application)
                and [entry.id for entry in playlists.Store.open().find(dock.id).entries]
                == [second.id]
            ),
        )
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_ab6_a_refused_default_playlist_settles_the_schedule_page_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write refused before it is queued (settings from a newer version): the page's
    queued choice was settled only after the report."""
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
        before = page.fallback_row.get_selected()
        wanted = page._fallback_ids.index(dock.id)
        assert wanted != before
        monkeypatch.setattr(application, "_settings_unknown_keys", ("from_a_newer_version",))
        reported = _raises_once(application, "window_report", monkeypatch)
        page.fallback_row.set_selected(wanted)  # its handler raises with the report
        assert reported and application.settings.active_playlist != dock.id
        # Before: the queued choice stayed for good, and the row with it.
        assert page._pending_default == {}, page._pending_default
        page.flush()
        assert page.fallback_row.get_selected() == before

        monkeypatch.setattr(application, "_settings_unknown_keys", ())
        page.fallback_row.set_selected(wanted)
        yield (
            "the next choice",
            lambda: settled(application) and config.load_strict().active_playlist == dock.id,
        )
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)


def test_ab6_a_refused_settings_switch_goes_back_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings from a newer version: refused before anything is queued, so nothing
    else puts the row back."""
    target = paths.settings_path()
    document = target.read_text()
    target.write_text(document + 'wallpaper_rules = "from a newer version"\n')
    shown = _open_settings(monkeypatch)
    try:
        row = shown.row("shuffle")
        before = row.get_active()
        reported = _raises_once(shown.application, "window_report", monkeypatch)
        row.set_active(not before)  # its handler raises with the report: GTK logs it
        assert reported
        # Before: the switch stayed where it was clicked, though nothing was saved.
        assert row.get_active() == before

        target.write_text(document)
        monkeypatch.setattr(shown.application, "_settings_unknown_keys", ())
        row.set_active(not before)
        shown.wait("the next change", lambda: config.load().shuffle == (not before))
    finally:
        _close_settings(shown)


def test_ab6_a_palette_save_refused_outright_releases_the_editor_before_its_report(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The editor holds its Save until it hears; a refusal said first never told it."""
    shown = _open_settings(monkeypatch)
    try:
        palettes.write_custom(
            "Evening",
            {
                "dark": {key: "#202830" for key, _label in palettes.EDITABLE_KEYS},
                "light": {key: "#f0f0f0" for key, _label in palettes.EDITABLE_KEYS},
            },
        )
        shown.window.open_palette_browser()
        dialog = shown.page._dialogs[-1]
        assert isinstance(dialog, PalettesDialog)
        manager = dialog.manager
        spin_until(lambda: manager.palette_list().phase == "ready", 10, what="the catalogue")
        (evening,) = [item for item in manager.palette_list().items if item.name == "Evening"]
        dialog.edit(evening, copy=True)
        spin_until(
            lambda: isinstance(dialog.nav.get_visible_page(), PaletteEditor), 10, what="the editor"
        )
        editor = dialog.nav.get_visible_page()
        assert isinstance(editor, PaletteEditor)
        editor.name.set_text("Evening red")
        with_overrides = palettes.with_overrides
        refused: list[int] = []

        def refuse_once(*arguments: Any, **keywords: Any) -> Any:
            if not refused:
                refused.append(1)
                raise PaletteError("that is not a colour")
            return with_overrides(*arguments, **keywords)

        monkeypatch.setattr(palettes, "with_overrides", refuse_once)
        reported = _raises_once(shown.application, "window_report", monkeypatch)
        with pytest.raises(RuntimeError, match="injected"):
            editor.save()
        assert refused and reported
        # Before: "saving" for good, Save off, and save() did nothing.
        assert not editor._saving and editor.save_button.get_sensitive()

        editor.save()
        shown.wait("the saved palette", lambda: palettes.custom_path("Evening red").is_file())
    finally:
        _close_settings(shown)


def test_ab6_the_player_bars_toggles_go_back_whatever_the_command_did(
    runtime: FakeRuntime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Runtime truth owns Shuffle and Cycle: a refusal that raised left them as clicked."""
    application = Application(ui="next")
    lanes: list[object] = []

    def scenario() -> Iterator[Step]:
        window = application._window
        assert isinstance(window, NextWindow)
        yield first_scan(application, window)
        bar = window.playerbar
        for toggle in (bar._shuffle, bar._rotate):
            before = toggle.get_active()
            reported = _raises_once(application, "window_report", monkeypatch)
            toggle.set_active(not before)  # no runtime here: refused, and saying so raises
            assert reported
            # Before: it stayed as clicked, claiming what the runtime never took.
            assert toggle.get_active() == before
            toggle.set_active(not before)  # the next click: refused and said, as usual
            assert len(reported) >= 2 and toggle.get_active() == before
        yield from finish(application, window, lanes)

    run(application, scenario(), lanes)
