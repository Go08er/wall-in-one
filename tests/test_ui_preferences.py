"""Settings widgets follow the application's last durable snapshot."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from wall_in_one import config  # noqa: E402
from wall_in_one.ui.preferences import PreferencesPage  # noqa: E402


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
    window = MainWindow(application, application.settings)
    application._window = window
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    try:
        external = replace(application.settings, dynamics_enabled=False)
        application._on_settings_changed(external)

        assert window._settings_page._dynamics.get_active() is False
        window._settings_page._opacity.set_value(0.75)
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
