"""Settings widgets follow the application's last durable snapshot."""

from __future__ import annotations

from collections.abc import Iterator
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
from wall_in_one.library import pairings  # noqa: E402
from wall_in_one.theme.noctalia import ALL_SCHEMES  # noqa: E402
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


def _buttons(widget: Gtk.Widget) -> Iterator[Gtk.Button]:
    if isinstance(widget, Gtk.Button):
        yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _buttons(child)
        child = child.get_next_sibling()


@pytest.mark.parametrize("primary_missing", [False, True])
def test_folder_labels_preserve_paths_order_and_missing_destination(
    tmp_path: Path, primary_missing: bool
) -> None:
    roots = tuple(tmp_path / name for name in ("z-downloads & stills", "a-library", "m-library"))
    for index, root in enumerate(roots):
        if index != 0 or not primary_missing:
            root.mkdir()
            (root / "wallpaper.png").write_bytes(b"existing wallpaper")
    application = SettingsApp(roots[0])
    application.settings = replace(application.settings, roots=roots)
    settings_path = config.save(application.settings, tmp_path / "settings.toml")
    saved = settings_path.read_bytes()

    page = PreferencesPage(application)  # type: ignore[arg-type]
    page.apply_settings(application.settings)

    assert application.settings.roots == roots
    assert application.changes == []
    assert settings_path.read_bytes() == saved
    assert config.load(settings_path).roots == roots
    assert len(page._root_rows) == len(roots)
    for index, (row, root) in enumerate(zip(page._root_rows, roots, strict=True)):
        assert isinstance(row, Adw.ActionRow)
        assert not row.get_use_markup()
        if index == 0:
            assert row.get_title() == f"{root.name} · Downloads & generated stills"
        else:
            assert row.get_title() == root.name
        missing = index == 0 and primary_missing
        assert row.get_subtitle() == (f"{root} -- not there right now" if missing else str(root))
        assert row.has_css_class("warning") == missing
        remove = [
            button for button in _buttons(row) if button.get_icon_name() == "list-remove-symbolic"
        ]
        assert len(remove) == 1
        assert remove[0].get_tooltip_text() == "Remove from Library"
        if missing:
            assert not root.exists()
        else:
            assert (root / "wallpaper.png").read_bytes() == b"existing wallpaper"


def test_adaptive_default_copy_and_saved_setting_preserve_explicit_pairing_choices(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    roots = (tmp_path / "z-downloads", tmp_path / "a-library")
    for root in roots:
        root.mkdir()
    application = SettingsApp(roots[0])
    application.settings = replace(
        application.settings, roots=roots, preview_scheme="m3-tonal-spot"
    )
    settings_path = config.save(application.settings, tmp_path / "settings.toml")

    def persist(**changes: Any) -> config.Settings:
        application.changes.append(changes)
        application.settings = config.update(changes, settings_path)
        return application.settings

    monkeypatch.setattr(application, "update_settings", persist)
    policies = tuple(
        pairings.PalettePolicy.decode(raw)
        for raw in ("adaptive", "adaptive:muted", "builtin:Nord", "keep")
    )
    explicit_before = tuple(policy.selection("m3-tonal-spot") for policy in policies[1:])
    encoded_before = tuple(policy.encode() for policy in policies)
    page = PreferencesPage(application)  # type: ignore[arg-type]
    assert application.changes == []
    assert page._scheme.get_title() == "Default adaptive colour scheme"
    assert page._scheme.get_subtitle() == (
        "For previews and adaptive pairings without a chosen scheme. "
        "Explicit pairing colours stay unchanged"
    )
    assert page._follow_palette.get_title() == "Follow Noctalia for app colours"
    assert page._follow_palette.get_subtitle() == (
        "Update this app's appearance only; pairing colour choices stay unchanged"
    )

    page._scheme.set_selected(ALL_SCHEMES.index("soft"))
    page._follow_palette.set_active(False)
    page._opacity.set_value(0.75)
    saved = config.load(settings_path)
    assert saved == application.settings
    assert saved.preview_scheme == "soft"
    assert not saved.follow_noctalia_palette
    assert saved.opacity == 0.75
    assert saved.roots == roots
    assert 'preview_scheme = "soft"' in settings_path.read_text()
    inherited = policies[0].selection(saved.preview_scheme)
    assert inherited is not None
    assert (inherited.source, inherited.name) == ("wallpaper", "soft")
    assert (
        tuple(policy.selection(saved.preview_scheme) for policy in policies[1:]) == explicit_before
    )
    assert tuple(policy.encode() for policy in policies) == encoded_before


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


def test_battery_option_survives_external_refresh_and_unrelated_edits(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    application = SettingsApp(root)
    page = PreferencesPage(application)  # type: ignore[arg-type]
    assert not page._battery_animations.get_active()
    page._battery_animations.set_active(True)
    assert application.settings.stop_animations_on_battery
    assert application.settings.dynamics_enabled

    external = replace(application.settings, dynamics_enabled=False)
    application.settings = external
    page.apply_settings(external)
    page._opacity.set_value(0.75)
    assert application.settings.stop_animations_on_battery
    assert not application.settings.dynamics_enabled


def test_failed_battery_setting_save_restores_switch(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    application = SettingsApp(root)
    page = PreferencesPage(application)  # type: ignore[arg-type]
    application.fail = True
    page._battery_animations.set_active(True)
    assert not application.settings.stop_animations_on_battery
    assert not page._battery_animations.get_active()


def test_old_runtime_refusal_restores_native_switch_and_preserves_saved_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from wall_in_one.control import client
    from wall_in_one.control.protocol import Response

    class DurableSettingsApp(SettingsApp):
        def update_settings(self, **changes: Any) -> config.Settings:
            self.settings = config.update(changes)
            return self.settings

    root = tmp_path / "Original Library"
    root.mkdir()
    application = DurableSettingsApp(root)
    target = config.save(application.settings)
    before = target.read_bytes()
    monkeypatch.setattr(
        client,
        "send_runtime",
        lambda *_args, **_kwargs: Response(True, '{"status_version":2}'),
    )
    page = PreferencesPage(application)  # type: ignore[arg-type]
    page._battery_animations.set_active(True)
    assert not page._battery_animations.get_active()
    assert not application.settings.stop_animations_on_battery
    assert target.read_bytes() == before
    assert len(application.reports) == 1
    assert "running wallpaper service does not support" in application.reports[0]


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
