"""Display authoring keeps detached choices visible and honest."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, GLib, Gtk  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.library import schedules  # noqa: E402
from wall_in_one.ui import preferences, schedules_page  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class _PreferencesApp:
    def __init__(self, root: Path) -> None:
        self.settings = config.Settings(
            roots=(root,),
            display_mode=config.DISPLAY_MODE_INDEPENDENT,
            theme_source_connector="DP-9",
        )
        self.session = SimpleNamespace(
            displays=SimpleNamespace(all=lambda: (("DP-9", "evening"),)),
            schedules=SimpleNamespace(rules=()),
        )
        self.resolved_palette = None
        self.runtime_status: dict[str, object] | None = None
        self.reports: list[str] = []

    def update_settings(self, **changes: Any) -> config.Settings:
        self.settings = replace(self.settings, **changes).validated()
        return self.settings

    @property
    def requested_settings(self) -> config.Settings:
        return self.settings

    def update_settings_async(
        self,
        *,
        on_success: Any = None,
        on_error: Any = None,
        **changes: Any,
    ) -> bool:
        try:
            saved = self.update_settings(**changes)
        except config.ConfigError as error:
            if on_error is not None:
                on_error(str(error))
            return False
        if on_success is not None:
            on_success(saved)
        return True

    def window_report(self, message: str) -> None:
        self.reports.append(message)

    def open_palette_browser(self) -> None: ...

    def reload_palette(self) -> None: ...


def _model_strings(row: Adw.ComboRow) -> tuple[str, ...]:
    model = row.get_model()
    assert isinstance(model, Gtk.StringList)
    return tuple(model.get_string(index) or "" for index in range(model.get_n_items()))


def test_theme_source_preserves_a_detached_choice_across_hotplug(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    connected = ["eDP-1"]
    monkeypatch.setattr(preferences, "_connected_outputs", lambda: tuple(connected))
    root = tmp_path / "library"
    root.mkdir()
    application = _PreferencesApp(root)
    page = preferences.PreferencesPage(application)  # type: ignore[arg-type]

    assert page._selected_theme_connector() == "DP-9"
    assert all("Automatic" not in label for label in _model_strings(page._theme_source))
    assert "DP-9 (not attached)" in _model_strings(page._theme_source)
    assert "temporarily follow eDP-1" in (page._theme_source.get_subtitle() or "")

    connected.append("DP-9")
    page._on_outputs_changed()

    assert page._selected_theme_connector() == "DP-9"
    assert "DP-9" in _model_strings(page._theme_source)
    assert "not attached" not in (page._theme_source.get_subtitle() or "")
    assert application.settings.theme_source_connector == "DP-9"


def test_enabling_independent_mode_selects_the_first_live_colour_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preferences, "_connected_outputs", lambda: ("eDP-1", "DP-2"))
    root = tmp_path / "library"
    root.mkdir()
    application = _PreferencesApp(root)
    application.settings = replace(
        application.settings,
        display_mode=config.DISPLAY_MODE_MIRRORED,
        theme_source_connector="",
    )
    application.session.displays = SimpleNamespace(all=lambda: ())
    page = preferences.PreferencesPage(application)  # type: ignore[arg-type]

    page._display_mode.set_selected(1)

    assert application.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
    assert application.settings.theme_source_connector == "eDP-1"
    assert page._selected_theme_connector() == "eDP-1"


def test_independent_colour_source_uses_runtime_connector_when_gdk_has_no_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preferences, "_connected_outputs", lambda: ())
    root = tmp_path / "library"
    root.mkdir()
    application = _PreferencesApp(root)
    application.settings = replace(
        application.settings,
        display_mode=config.DISPLAY_MODE_MIRRORED,
        theme_source_connector="",
    )
    application.session.displays = SimpleNamespace(all=lambda: ())
    application.session.schedules = SimpleNamespace(rules=())
    application.runtime_status = _display_runtime_status()
    page = preferences.PreferencesPage(application)  # type: ignore[arg-type]

    assert _model_strings(page._theme_source) == ("DP-9",)
    page._display_mode.set_selected(1)

    assert application.settings.display_mode == config.DISPLAY_MODE_INDEPENDENT
    assert application.settings.theme_source_connector == "DP-9"


def test_enabling_independent_mode_without_a_live_display_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preferences, "_connected_outputs", lambda: ())
    root = tmp_path / "library"
    root.mkdir()
    application = _PreferencesApp(root)
    application.settings = replace(
        application.settings,
        display_mode=config.DISPLAY_MODE_MIRRORED,
        theme_source_connector="",
    )
    application.session.displays = SimpleNamespace(all=lambda: ())
    page = preferences.PreferencesPage(application)  # type: ignore[arg-type]

    page._display_mode.set_selected(1)

    assert application.settings.display_mode == config.DISPLAY_MODE_MIRRORED
    assert page._display_mode.get_selected() == 0
    assert application.reports == [
        "Independent display control needs an attached display for Colours follow. "
        "Connect a display and try again."
    ]


class _Displays:
    def __init__(self) -> None:
        self.values = (("DP-9", "evening"),)

    def all(self) -> tuple[tuple[str, str], ...]:
        return self.values

    def __len__(self) -> int:
        return len(self.values)


def _schedule_app(mode: str) -> SimpleNamespace:
    playlist = SimpleNamespace(id="evening", name="Evening")
    rule = schedules.Rule(id="dock", playlist="evening", connector="DP-9")
    session = SimpleNamespace(
        settings=config.Settings(
            active_playlist="evening",
            display_mode=mode,
            theme_source_connector="DP-9",
        ),
        manual_playlist=None,
        playlists=SimpleNamespace(all=lambda: (playlist,)),
        displays=_Displays(),
        schedules=SimpleNamespace(rules=(rule,)),
    )
    return SimpleNamespace(session=session, runtime_status=None)


def _group_titles(page: schedules_page.SchedulesPage) -> tuple[str, ...]:
    titles: list[str] = []
    child = page._content.get_first_child()
    while child is not None:
        if isinstance(child, Adw.PreferencesGroup):
            titles.append(child.get_title())
        child = child.get_next_sibling()
    return tuple(titles)


def test_mirrored_mode_hides_assignment_clutter_but_keeps_rule_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("eDP-1",))
    application = _schedule_app(config.DISPLAY_MODE_MIRRORED)
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]
    page.refresh(application.session)

    assert "Screens" not in _group_titles(page)
    assert "Independent display setup" in _group_titles(page)
    assert page._rule_target.get_sensitive() is False

    rule = application.session.schedules.rules[0]
    page._edit_rule(rule)
    assert page._selected_rule_connector() == "DP-9"

    application.session.settings = replace(
        application.session.settings,
        display_mode=config.DISPLAY_MODE_INDEPENDENT,
    )
    page.refresh(application.session)

    assert "Screens" in _group_titles(page)
    assert page._rule_target.get_sensitive() is True
    assert page._playback_row is None
    assert page._display_playback_rows == {}


def _display_runtime_status() -> dict[str, object]:
    return {
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
        "displays": [
            {
                "connector": "DP-9",
                "connected": True,
                "assignment_source": "explicit",
                "assigned_playlist_id": "evening",
                "assigned_playlist": "Evening",
                "playlist_id": "evening",
                "playlist": "Evening",
                "entry_id": "entry-1",
                "entry_taboo": False,
                "kind": "still",
                "still": "/library/evening.png",
                "motion_active": False,
                "route_source": "manual",
                "manual_override": True,
                "schedule_rule_id": None,
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
        ],
    }


def test_independent_playback_uses_atomic_display_truth_and_targeted_controls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("DP-9",))
    application = _schedule_app(config.DISPLAY_MODE_INDEPENDENT)
    application.runtime_status = _display_runtime_status()
    calls: list[tuple[str, str]] = []

    def activate(connector: str, playlist: str) -> bool:
        calls.append((connector, playlist))
        return True

    def follow(connector: str) -> bool:
        calls.append((connector, "schedule-follow"))
        return True

    def action(connector: str, verb: str, argument: str | None = None) -> bool:
        calls.append((connector, f"{verb} {argument}" if argument is not None else verb))
        return True

    application.activate_playlist_on_async = activate
    application.resume_schedule_on_async = follow
    application.runtime_action_on_async = action
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]

    page.refresh(application.session)

    controls = page._display_playback_rows["DP-9"]
    row = controls.playlist
    assert row.get_selected() == 1
    assert "Manual override" in (row.get_subtitle() or "")
    assert "Colours source" not in (row.get_subtitle() or "")
    palette_row = page._playback_widgets[0]
    assert isinstance(palette_row, Adw.ActionRow)
    assert "temporarily follow eDP-1" in (palette_row.get_subtitle() or "")

    row.set_selected(0)

    assert calls == [("DP-9", "schedule-follow")]
    # The service-owned snapshot, not the optimistic ComboRow gesture, remains
    # visible until the next accepted status response.
    assert row.get_selected() == 1

    controls.play.emit("clicked")
    controls.stop.emit("clicked")
    controls.shuffle.set_active(True)
    controls.cycle.set_active(False)

    assert calls[-4:] == [
        ("DP-9", "pause"),
        ("DP-9", "stop"),
        ("DP-9", "shuffle on"),
        ("DP-9", "cycle off"),
    ]
    assert controls.shuffle.get_active() is False
    assert controls.cycle.get_active() is True


def test_independent_modes_show_provenance_and_reset_both_runtime_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("DP-9",))
    application = _schedule_app(config.DISPLAY_MODE_INDEPENDENT)
    status = _display_runtime_status()
    raw_displays = status["displays"]
    assert isinstance(raw_displays, list)
    display = raw_displays[0]
    assert isinstance(display, dict)
    display.update(
        {
            "shuffle": True,
            "shuffle_default": False,
            "shuffle_source": "manual",
            "cycle_enabled": False,
            "cycle_default": True,
            "cycle_source": "manual",
        }
    )
    application.runtime_status = status
    calls: list[tuple[str, str, str | None]] = []
    resets: list[str] = []

    def action(connector: str, verb: str, argument: str | None = None) -> bool:
        calls.append((connector, verb, argument))
        return True

    application.runtime_action_on_async = action

    def reset(connector: str) -> bool:
        resets.append(connector)
        return True

    application.reset_display_modes_on_async = reset
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]

    page.refresh(application.session)

    controls = page._display_playback_rows["DP-9"]
    subtitle = controls.modes.get_subtitle() or ""
    assert "Cycle off — manual (saved default on)" in subtitle
    assert "Shuffle on — manual (saved default off)" in subtitle
    assert controls.mode_defaults.get_visible() is True

    controls.mode_defaults.emit("clicked")

    assert resets == ["DP-9"]
    assert calls == []
    # The accepted atomic status, not the click, owns the visible mode state.
    assert controls.cycle.get_active() is False
    assert controls.shuffle.get_active() is True
    assert controls.mode_defaults.get_visible() is True


def test_route_failure_keeps_the_schedule_row_compact_but_preserves_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("DP-9",))
    application = _schedule_app(config.DISPLAY_MODE_INDEPENDENT)
    calls: list[tuple[str, str, str | None]] = []
    presented: list[str] = []

    def action(connector: str, verb: str, argument: str | None = None) -> bool:
        calls.append((connector, verb, argument))
        return True

    application.runtime_action_on_async = action
    application.present_page = presented.append
    status = _display_runtime_status()
    raw_displays = status["displays"]
    assert isinstance(raw_displays, list)
    display = raw_displays[0]
    assert isinstance(display, dict)
    diagnostic = "renderer start " + "x" * 12_000 + " attributable tail"
    display["renderer_failed"] = True
    display["last_error"] = diagnostic
    application.runtime_status = status
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]

    page.refresh(application.session)

    controls = page._display_playback_rows["DP-9"]
    row = controls.row
    subtitle = row.get_subtitle() or ""
    assert len(subtitle) < 280
    assert subtitle.startswith("Renderer stopped · Retry available · Evening · renderer start")
    assert subtitle.endswith("attributable tail")
    assert " … " in subtitle
    assert row.get_tooltip_text() == diagnostic
    assert controls.play.get_icon_name() == "media-playback-start-symbolic"
    assert controls.play.get_tooltip_text() == "Retry motion on this display"
    assert controls.play.get_sensitive(), "a transient renderer failure keeps one-shot Retry"

    controls.play.emit("clicked")

    assert calls == [("DP-9", "play", None)]

    # An equivalent occurrence may have crashed elsewhere, and an old record
    # may be outside the bounded diagnostic inventory. Exact route truth must
    # still remove Play even though this display itself never failed.
    display["renderer_failed"] = False
    display["entry_taboo"] = True
    status["taboo_entries"] = []
    status["taboo_entries_omitted"] = 7
    page.runtime_status_changed(application.session)

    assert "Borked · playback disabled" in (controls.row.get_subtitle() or "")
    assert controls.play.get_icon_name() == "dialog-warning-symbolic"
    assert "Borked wallpaper" in (controls.play.get_tooltip_text() or "")
    assert "Media/Pairings" in (controls.play.get_tooltip_text() or "")
    assert not controls.play.get_sensitive()

    controls.play.emit("clicked")

    assert calls == [("DP-9", "play", None)], "taboo entries cannot be retried by Play"
    assert presented == ["media"]


def test_status_poll_reuses_display_controls_and_preserves_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ("DP-9",))
    application = _schedule_app(config.DISPLAY_MODE_INDEPENDENT)
    application.runtime_status = _display_runtime_status()
    application.activate_playlist_on_async = lambda *_args: True
    application.resume_schedule_on_async = lambda *_args: True
    application.runtime_action_on_async = lambda *_args: True
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]
    host = Gtk.Window(child=page)
    host.present()
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)
    try:
        page.refresh(application.session)
        before = page._display_playback_rows["DP-9"]
        assert before.playlist.grab_focus()
        while context.pending():
            context.iteration(False)
        focus = host.get_focus()
        assert focus is not None

        changed = _display_runtime_status()
        raw_displays = changed["displays"]
        assert isinstance(raw_displays, list)
        display = raw_displays[0]
        assert isinstance(display, dict)
        display["entry_id"] = "entry-2"
        display["playback_state"] = "paused"
        display["paused"] = True
        application.runtime_status = changed
        page.runtime_status_changed(application.session)

        after = page._display_playback_rows["DP-9"]
        assert after is before
        assert after.row is before.row
        assert after.playlist is before.playlist
        assert host.get_focus() is focus
        assert "Paused" in (after.row.get_subtitle() or "")
    finally:
        host.destroy()


def test_status_tick_cannot_absorb_external_authoring_or_hotplug_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connected = ["eDP-1"]
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: tuple(connected))
    application = _schedule_app(config.DISPLAY_MODE_INDEPENDENT)
    application.runtime_status = _display_runtime_status()
    application.activate_playlist_on_async = lambda *_args: True
    application.resume_schedule_on_async = lambda *_args: True
    application.runtime_action_on_async = lambda *_args: True
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]
    host = Gtk.Window(child=page)
    host.present()
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)
    try:
        page.refresh(application.session)
        page._months[7].set_active(True)
        page._time_enabled.set_active(True)
        page._start_hour.set_selected(22)
        page._start_minute.set_selected(30)
        assert page._start_hour.grab_focus()
        while context.pending():
            context.iteration(False)

        application.session.displays.values += (("DP-2", "evening"),)
        application.session.schedules.rules += (
            schedules.Rule(id="late", playlist="evening", connector="DP-2"),
        )
        connected.append("HDMI-A-1")
        changed = _display_runtime_status()
        raw_displays = changed["displays"]
        assert isinstance(raw_displays, list)
        display = raw_displays[0]
        assert isinstance(display, dict)
        display["entry_id"] = "status-tick"
        application.runtime_status = changed

        page.runtime_status_changed(application.session)
        page.refresh(application.session)

        assert len(page._rule_rows) == 2
        assert "DP-2" in page._rule_connectors
        assert "HDMI-A-1" in page._rule_connectors
        assert page._months[7].get_active() is True
        assert page._time_enabled.get_active() is True
        assert page._start_hour.get_selected() == 22
        assert page._start_minute.get_selected() == 30
        focused = host.get_focus()
        assert focused is not None and (
            focused is page._start_hour or focused.is_ancestor(page._start_hour)
        )
    finally:
        host.destroy()


def test_schedule_targets_include_live_runtime_connector_when_gdk_is_unnamed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(schedules_page, "_connected_outputs", lambda: ())
    application = _schedule_app(config.DISPLAY_MODE_INDEPENDENT)
    application.session.displays.values = ()
    application.session.schedules.rules = ()
    application.session.settings = replace(
        application.session.settings,
        theme_source_connector="eDP-1",
    )
    status = _display_runtime_status()
    raw_displays = status["displays"]
    assert isinstance(raw_displays, list)
    display = raw_displays[0]
    assert isinstance(display, dict)
    display["connector"] = "niri-DP-7"
    application.runtime_status = status
    page = schedules_page.SchedulesPage(application)  # type: ignore[arg-type]

    page.refresh(application.session)

    assert "niri-DP-7" in page._rule_connectors
