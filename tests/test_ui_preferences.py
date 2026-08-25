"""Settings widgets follow the application's last durable snapshot."""

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

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.ui.preferences import PreferencesPage  # noqa: E402
from wall_in_one.wallpaper import scenes  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class SettingsApp:
    def __init__(self, root: Path) -> None:
        self.settings = config.Settings(roots=(root,), dynamics_enabled=True, opacity=1.0)
        self.resolved_palette = None
        self.reports: list[str] = []
        self.fail = False
        self.changes: list[dict[str, Any]] = []

    def update_settings(self, **changes: Any) -> config.Settings:
        self.changes.append(changes)
        if self.fail:
            raise config.ConfigError("disk full")
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

    def reload_palette(self) -> None:
        return None


def test_external_settings_refresh_widgets_before_the_next_local_edit(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    application = SettingsApp(root)
    page = PreferencesPage(application)  # type: ignore[arg-type]

    external = replace(application.settings, dynamics_enabled=False)
    application.settings = external
    page.apply_settings(external)
    page._opacity.set_value(0.75)

    assert application.settings.dynamics_enabled is False
    assert application.changes[-1]["dynamics_enabled"] is False
    assert application.settings.opacity == 0.75


def test_application_side_settings_refresh_the_open_preferences_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exercise the application/window seam used by non-Preferences updates."""
    from wall_in_one.ui.app import Application
    from wall_in_one.ui.window import MainWindow

    application = Application()
    application._authoring_migration_ready = True
    window = MainWindow(application, application.settings)
    application._window = window
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    try:
        external = replace(application.settings, dynamics_enabled=False)
        application._on_settings_changed(external)

        assert window._settings_page._dynamics.get_active() is False
        window._settings_page._opacity.set_value(0.75)
        context = GLib.MainContext.default()
        while application.settings.opacity != 0.75:
            context.iteration(True)
        assert application.settings.dynamics_enabled is False
        assert application.settings.opacity == 0.75
    finally:
        application._window = None
        window.destroy()
        application._stills.shutdown()
        application.session.shutdown()


def test_failed_settings_write_restores_every_control_and_reports(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    application = SettingsApp(root)
    page = PreferencesPage(application)  # type: ignore[arg-type]
    application.fail = True

    page._dynamics.set_active(False)

    assert application.settings.dynamics_enabled is True
    assert page._dynamics.get_active() is True
    assert len(application.reports) == 1
    assert "not saved" in application.reports[0]
    assert "nothing changed" in application.reports[0]


def test_failed_add_folder_does_not_claim_that_a_scan_started(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    added = tmp_path / "second-library"
    added.mkdir()
    application = SettingsApp(root)
    page = PreferencesPage(application)  # type: ignore[arg-type]
    application.fail = True
    dialog = SimpleNamespace(select_folder_finish=lambda _result: Gio.File.new_for_path(str(added)))

    page._on_root_chosen(dialog, None)  # type: ignore[arg-type]

    assert application.settings.roots == (root,)
    assert application.reports == ["Library folders were not saved; nothing changed: disk full"]


def test_scene_presentation_choices_are_saved_and_failed_writes_restore_them(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    application = SettingsApp(root)
    page = PreferencesPage(application)  # type: ignore[arg-type]

    page._scene_scaling.set_selected(scenes.SCALING_CHOICES.index("fill"))
    page._scene_clamp.set_selected(scenes.CLAMP_CHOICES.index("border"))
    assert application.settings.scene_scaling == "fill"
    assert application.settings.scene_clamp == "border"

    application.fail = True
    page._scene_scaling.set_selected(scenes.SCALING_CHOICES.index("fit"))
    assert application.settings.scene_scaling == "fill"
    assert page._scene_scaling.get_selected() == scenes.SCALING_CHOICES.index("fill")
