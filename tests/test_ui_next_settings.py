"""The new interface's Settings page, on the real application.

`NextWindow` over a real `Application` (not run: its authoring lane, settings
batches and rollback are driven by spinning GLib), in the test's own XDG
directories (conftest), with the runtime and Noctalia replaced
(``sandboxed_runtime``) and the runtime-compatibility probe stubbed: it would
otherwise ask systemd about the real session.

Each row is checked against the file it writes; a failure is a real refusal
from ``config.update``, so the rollback is the application's own.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import fields, replace
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.test_settings_v0_1_4_keys import V0_1_4_SETTINGS_KEYS  # noqa: E402
from tests.test_ui_next_window import sandboxed_runtime  # noqa: E402
from wall_in_one import config, paths, runtime_compatibility, ui_prefs  # noqa: E402
from wall_in_one.providers import credentials, registry  # noqa: E402
from wall_in_one.theme import palettes  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next import real_settings  # noqa: E402
from wall_in_one.ui.next.settings import SettingsPage  # noqa: E402
from wall_in_one.ui.next.settings_palettes import PaletteEditor, PalettesDialog  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402

#: The keys classic Settings writes, each now a row here (the folders write `roots`).
CLASSIC_KEYS = frozenset(
    {
        "shuffle",
        "cycle_enabled",
        "cycle_interval",
        "cycle_favourites_only",
        "dynamics_enabled",
        "stop_animations_on_battery",
        "video_when_hidden",
        "display_mode",
        "follow_noctalia_palette",
        "preview_scheme",
        "opacity",
        "scan_workshop",
        "video_muted",
        "video_volume",
        "video_hardware_decode",
        "video_interpolation",
        "own_scene_renderer",
        "scene_fps",
        "scene_scaling",
        "scene_clamp",
    }
)


@pytest.fixture(scope="module", autouse=True)
def toolkit() -> None:
    try:
        Gtk.init()
    except Exception:  # pragma: no cover - only on a headless machine
        pytest.skip("no display")
    Adw.init()


class Opened:
    """The application, its new window and the Settings page, as a user opens them."""

    def __init__(self, application: Application, window: NextWindow) -> None:
        self.application = application
        self.window = window
        self.reports: list[str] = []
        page = window.pages["settings"]
        assert isinstance(page, SettingsPage)
        self.page = page

    def row(self, key: str) -> Any:
        return next(binding.row for binding in self.page._bindings if binding.key == key)

    def settled(self) -> bool:
        application = self.application
        return (
            not application._settings_authoring_running
            and not application._authoring_active
            and not application._authoring_queue
        )

    def wait(self, what: str, until: Callable[[], object] | None = None) -> None:
        spin_until(lambda: self.settled() and (until is None or until()), 10, what=what)


def _open(monkeypatch: pytest.MonkeyPatch) -> Opened:
    application = Application(ui="next")
    application._authoring_migration_ready = True
    monkeypatch.setattr(application, "_publish_runtime_for_context", lambda: True)
    window = NextWindow(application, application.settings)
    application._window = window
    opened = Opened(application, window)
    monkeypatch.setattr(application, "window_report", opened.reports.append)
    window.present()
    window.navigate("settings")
    return opened


def _close(opened: Opened) -> None:
    application = opened.application
    application._window = None
    opened.window.destroy()
    application._stills.shutdown()
    application.session.shutdown()


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Opened]:
    sandboxed_runtime(monkeypatch, tmp_path)
    # Asked only for stop_animations_on_battery; it would consult systemd.
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    shown = _open(monkeypatch)
    try:
        yield shown
    finally:
        _close(shown)


def _another(row: Any, binding_choices: tuple[tuple[str, str], ...], value: Any) -> Any:
    """A different valid value for ``row`` than ``value``."""
    if isinstance(row, Adw.SwitchRow):
        return not value
    if isinstance(row, Adw.SpinRow):
        adjustment = row.get_adjustment()
        candidate = adjustment.get_lower() + adjustment.get_step_increment() * 3
        return round(candidate, row.get_digits()) if row.get_digits() else int(candidate)
    keys = [key for key, _label in binding_choices]
    return keys[(keys.index(value) + 1) % len(keys)]


def _set(row: Any, binding_choices: tuple[tuple[str, str], ...], value: Any) -> None:
    if isinstance(row, Adw.SwitchRow):
        row.set_active(value)
    elif isinstance(row, Adw.SpinRow):
        row.set_value(value)
    else:
        row.set_selected([key for key, _label in binding_choices].index(value))


def test_ctl_open_settings_reaches_the_real_page(opened: Opened) -> None:
    assert opened.window.show_page("settings") is True
    assert opened.window.note_text == ""
    assert opened.window.content_page.get_title() == "Settings"


def test_every_row_saves_its_own_0_1_4_key_and_nothing_else(opened: Opened) -> None:
    page = opened.page
    keys = {binding.key for binding in page._bindings}
    assert keys == CLASSIC_KEYS
    assert keys <= V0_1_4_SETTINGS_KEYS, "settings.toml stays as 0.1.4 reads it"
    for binding in page._bindings:
        if binding.key == "display_mode":
            continue  # needs a display to follow: its own test below
        before = config.load()
        value = getattr(before, binding.key)
        wanted = _another(binding.row, binding.choices, value)
        _set(binding.row, binding.choices, wanted)
        key = binding.key

        def saved(key: str = key, want: Any = wanted) -> bool:
            return bool(getattr(config.load(), key) == want)

        opened.wait(f"{key} to be saved", saved)
        after = config.load()
        assert after == replace(before, **{binding.key: wanted}), binding.key
        assert opened.application.settings == after
        assert page.access is not None and page.access.setting(binding.key) == wanted
    assert opened.reports == [], opened.reports


def test_independent_displays_save_the_display_colours_follow(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(real_settings, "_connected_outputs", lambda: ("DP-3",))
    opened.row("display_mode").set_selected(1)
    opened.wait("independent displays", lambda: config.load().display_mode == "independent")
    assert config.load().theme_source_connector == "DP-3"
    theme = opened.page._theme_row
    assert theme.get_visible() and theme.get_selected() == 0
    assert "follows this display" in (theme.get_subtitle() or "")


def test_a_failed_save_puts_every_row_back_and_the_same_choice_then_saves(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A real refusal (config.update's compatibility check), the application's rollback."""

    def refuse(_schema: int) -> None:
        raise runtime_compatibility.RuntimeCompatibilityError("the service is too old")

    monkeypatch.setattr(runtime_compatibility, "require_schema", refuse)
    target = paths.settings_path()
    saved = target.read_bytes()
    battery = opened.row("stop_animations_on_battery")
    battery.set_active(True)
    opened.wait("the refusal", lambda: opened.reports)
    assert not battery.get_active(), "the row shows what is durable"
    assert target.read_bytes() == saved
    assert opened.reports[0].startswith("Settings were not saved; nothing changed:")
    assert "too old" in opened.reports[0]
    durable = config.load()
    page = opened.page
    assert page.access is not None
    for binding in page._bindings:
        assert page.access.setting(binding.key) == getattr(durable, binding.key), binding.key
    assert len(opened.reports) == 1, "the put-back rows wrote nothing more"

    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    battery.set_active(True)
    opened.wait("the retried save", lambda: config.load().stop_animations_on_battery)
    assert battery.get_active() and opened.page._battery_note.get_visible()


def test_settings_from_a_newer_version_turn_the_rows_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    target = paths.settings_path()
    target.write_text(target.read_text() + 'wallpaper_rules = "from a newer version"\n')
    document = target.read_bytes()
    shown = _open(monkeypatch)
    try:
        page = shown.page
        assert shown.application.settings_unknown_keys == ("wallpaper_rules",)
        assert page._notice.get_visible()
        assert "wallpaper_rules" in (page._notice.get_subtitle() or "")
        assert not shown.row("shuffle").get_sensitive()
        assert not page._add_folder_row.get_sensitive()
        assert not page._theme_row.get_sensitive()
        # These write other files, as in classic Settings.
        assert page._key_entry.get_sensitive()
        assert page.interface_row is not None and page.interface_row.get_sensitive()
        shown.row("shuffle").set_active(True)  # even if reached: refused, put back
        spin_until(lambda: shown.reports, what="the refusal")
        assert not shown.row("shuffle").get_active()
        assert target.read_bytes() == document
    finally:
        _close(shown)


def test_library_folders_are_added_moved_to_the_front_and_removed(
    opened: Opened, tmp_path: Path
) -> None:
    page = opened.page
    first = config.load().roots[0]
    second = tmp_path / "more walls"
    second.mkdir()
    page.add_folder(str(second))
    opened.wait("the added folder", lambda: config.load().roots == (first, second))
    assert opened.reports == ["Scanning more walls"]
    spin_until(lambda: len(page._folder_rows) == 2, what="the rows")
    page.add_folder(str(second))
    assert opened.reports[-1] == "more walls is already in the library"

    page.make_download_folder(str(second))
    opened.wait("the download folder", lambda: config.load().roots == (second, first))
    rows = page._folder_rows
    assert isinstance(rows[0], Adw.ActionRow) and rows[0].get_title() == "more walls"

    dialog = page.confirm_remove(str(first))
    assert dialog is not None
    dialog.emit("response", "cancel")
    assert config.load().roots == (second, first)
    dialog = page.confirm_remove(str(first))
    assert dialog is not None
    dialog.emit("response", "remove")
    opened.wait("the removal", lambda: config.load().roots == (second,))
    dialog.force_close()


def test_a_missing_folder_is_said_and_a_detected_one_only_suggested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    gone = tmp_path / "unplugged drive"
    config.update({"roots": (*config.load().roots, gone)})
    shown = _open(monkeypatch)
    try:
        page = shown.page
        spin_until(
            lambda: len(page._folder_rows) == 2 and page._folder_rows[1].has_css_class("warning"),
            what="the missing folder",
        )
        missing = page._folder_rows[1]
        assert isinstance(missing, Adw.ActionRow)
        assert "not there right now" in (missing.get_subtitle() or "")
        assert config.load().roots[1] == gone, "said, never dropped"
    finally:
        _close(shown)

    config.update({"roots": ()})
    suggested = Path.home() / "Pictures" / "Wallpapers"
    suggested.mkdir(parents=True)
    saved = paths.settings_path().read_bytes()
    shown = _open(monkeypatch)
    try:
        page = shown.page

        def suggested_shown() -> bool:
            row = page._folder_rows[0]
            assert isinstance(row, Adw.ActionRow)
            return f"Suggested folder: {suggested}" in (row.get_subtitle() or "")

        spin_until(suggested_shown, what="the suggestion")
        assert paths.settings_path().read_bytes() == saved, "a suggestion is not consent"
    finally:
        _close(shown)


def test_the_wallhaven_key_is_saved_and_removed_and_never_shown(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(registry.API_KEY_VARIABLE, raising=False)
    page = opened.page
    entry = page._key_entry
    entry.set_text("not a key!")
    page.save_key()
    opened.wait("the refused key", lambda: entry.get_sensitive())
    assert entry.has_css_class("error") and not credentials.key_path().exists()
    assert opened.reports == ["That does not look like a Wallhaven API key"]

    key = "k" * 32
    entry.set_text(key)
    page.save_key()
    opened.wait("the saved key", lambda: credentials.key_path().is_file())
    assert entry.get_text() == "" and not entry.has_css_class("error")
    spin_until(lambda: page._key_remove.get_visible(), what="the key status")
    assert page._key_row.get_subtitle() == f"Saved in {credentials.key_path()}"
    assert all(key not in report for report in opened.reports)

    page.clear_key()
    opened.wait("the removed key", lambda: not credentials.key_path().exists())
    spin_until(lambda: not page._key_remove.get_visible(), what="the cleared status")
    assert opened.reports[-1] == "Wallhaven API key removed"


def test_palettes_are_chosen_duplicated_and_edited_in_the_manager(
    opened: Opened, monkeypatch: pytest.MonkeyPatch
) -> None:
    applied: list[tuple[str, str]] = []

    def apply(palette_source: str, name: str, *, on_complete: Any = None) -> bool:
        applied.append((palette_source, name))
        on_complete("")
        return True

    monkeypatch.setattr(opened.application, "apply_noctalia_palette_async", apply)
    palettes.write_custom(
        "Evening",
        {
            "dark": {key: "#202830" for key, _label in palettes.EDITABLE_KEYS},
            "light": {key: "#f0f0f0" for key, _label in palettes.EDITABLE_KEYS},
        },
    )
    opened.window.open_palette_browser()  # what the window's menu and ctl ask for
    dialog = opened.page._dialogs[-1]
    assert isinstance(dialog, PalettesDialog)
    manager = dialog.manager
    spin_until(lambda: manager.palette_list().phase == "ready", what="the catalogue")
    items = {(item.origin, item.name): item for item in manager.palette_list().items}
    nord, evening = items[("builtin", "Nord")], items[("custom", "Evening")]
    row = dialog.row(nord.key)
    assert row is not None

    manager.apply_catalog_palette(nord.key)
    assert applied == [("builtin", "Nord")] and opened.reports[-1] == "Nord applied"

    dialog.edit(evening, copy=True)
    spin_until(lambda: isinstance(dialog.nav.get_visible_page(), PaletteEditor), what="editor")
    editor = dialog.nav.get_visible_page()
    assert isinstance(editor, PaletteEditor) and editor.name.get_text() == "Evening copy"
    editor.name.set_text("Evening red")
    editor.set_color("dark", "primary", "#aa0000")
    editor.save()
    opened.wait("the saved palette", lambda: palettes.custom_path("Evening red").is_file())
    written = palettes.read_document(palettes.custom_path("Evening red"))
    assert written["dark"]["mPrimary"] == "#aa0000"  # type: ignore[index]

    dialog.views.set_visible_child_name("schemes")
    dialog.use_scheme("soft")
    opened.wait("the default scheme", lambda: config.load().preview_scheme == "soft")
    spin_until(lambda: dialog._cards["soft"].default.get_visible(), what="the default mark")
    assert [scheme for scheme, card in dialog._cards.items() if card.default.get_visible()] == [
        "soft"
    ]
    dialog.force_close()
    spin_until(lambda: not opened.page._dialogs, what="the dialog to close")


def test_open_idle_and_close_write_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    settings_file = paths.settings_path()
    before = settings_file.read_bytes()
    stamp = settings_file.stat().st_mtime_ns
    shown = _open(monkeypatch)
    calls: list[dict[str, Any]] = []
    real = shown.application.update_settings_async

    def counted(**changes: Any) -> bool:
        calls.append(changes)
        return real(**changes)

    monkeypatch.setattr(shown.application, "update_settings_async", counted)
    try:
        page = shown.page
        for section in ("library", "playback", "colors", "appearance", "advanced", "tidy"):
            page.activate(section)
        page.open_palettes("schemes")
        page.demo("search:battery")
        page.demo("")
        shown.window.apply_settings(shown.application.settings)  # an external refresh
        idle_until = time.monotonic() + 1.0
        spin_until(lambda: time.monotonic() > idle_until, 5, what="a second of idling")
    finally:
        _close(shown)
    assert calls == []
    assert settings_file.read_bytes() == before and settings_file.stat().st_mtime_ns == stamp
    assert not paths.ui_prefs_path().exists(), "no ui.toml without a change"
    assert not credentials.key_path().exists()
    custom = palettes.custom_directory()
    assert not custom.exists() or not any(custom.iterdir()), "no palette written"


def test_the_interface_and_gpu_rows_stay_with_the_keeper(opened: Opened) -> None:
    """Classic's launch rows, as before: they save ui.toml once, and leave with the window."""
    page = opened.page
    assert page.interface_row is not None and page.gpu_row is not None
    keeper = opened.window.preferences
    assert len(keeper._subscribers) == 2, "the window's look and the launch rows; no more"
    page.gpu_row.set_active(False)
    keeper.close(wait=True)
    assert ui_prefs.load().prefs.gpu_acceleration is False and keeper.saves == 1


def test_the_settings_keys_are_still_0_1_4s() -> None:
    assert {field.name for field in fields(config.Settings)} == V0_1_4_SETTINGS_KEYS
