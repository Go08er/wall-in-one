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
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytestmark = pytest.mark.gui

gi = pytest.importorskip("gi")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk  # noqa: E402

from tests.gtk_helpers import spin_until  # noqa: E402
from tests.started import started_application  # noqa: E402
from tests.test_ui_next_window import sandboxed_runtime  # noqa: E402
from wall_in_one import config, paths, runtime_compatibility, ui_prefs  # noqa: E402
from wall_in_one.providers import credentials, registry  # noqa: E402
from wall_in_one.theme import noctalia, palettes, template  # noqa: E402
from wall_in_one.ui import palette_browser  # noqa: E402
from wall_in_one.ui.app import Application  # noqa: E402
from wall_in_one.ui.next import real_settings  # noqa: E402
from wall_in_one.ui.next.settings import SettingsPage  # noqa: E402
from wall_in_one.ui.next.settings_palettes import PaletteEditor, PalettesDialog  # noqa: E402
from wall_in_one.ui.next.window import NextWindow  # noqa: E402
from wall_in_one.ui.palette_catalog import PaletteCatalog  # noqa: E402

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
    application = started_application(ui="next")
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


def test_reads_that_failed_say_so_on_the_page_never_checking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The key, the template and the renderer rows, each read failing on the worker."""
    from wall_in_one.ui.next import real_settings

    def failing(*_arguments: object) -> Any:
        raise RuntimeError("injected: the read failed")

    for read in ("describe_key", "read_template", "_check_folders"):
        monkeypatch.setattr(real_settings, read, failing)
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    shown = _open(monkeypatch)
    try:
        page = shown.page
        spin_until(
            lambda: (
                page._renderer.get_subtitle() != "Checking…"
                and page._key_row.get_subtitle() != "Checking…"
                and "Checking" not in (page._template_row.get_subtitle() or "")
            ),
            5,
        )
        assert page._renderer.get_subtitle() == "Couldn't check: injected: the read failed"
        assert page._key_row.get_subtitle() == "Couldn't check the key: injected: the read failed"
        assert page._template_row.get_subtitle() == (
            "Couldn't check Noctalia's settings: injected: the read failed"
        )
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


def test_every_row_saves_its_own_key_and_nothing_else(opened: Opened) -> None:
    page = opened.page
    keys = {binding.key for binding in page._bindings}
    assert keys == CLASSIC_KEYS
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


def test_open_idle_and_close_write_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, asked: list[str]
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr(runtime_compatibility, "require_schema", lambda _schema: None)
    _noctalia_settings(NOCTALIA)  # an Install button on show: still nothing written
    noctalia_before = _noctalia_files()
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
    assert _noctalia_files() == noctalia_before, "Noctalia's settings untouched"
    assert not template.installed_template_path().exists(), "no template copy either"
    assert asked == [], "Noctalia is asked nothing: no reload, no render"


def test_the_interface_and_gpu_rows_stay_with_the_keeper(opened: Opened) -> None:
    """Classic's launch rows, as before: they save ui.toml once, and leave with the window."""
    page = opened.page
    assert page.interface_row is not None and page.gpu_row is not None
    keeper = opened.window.preferences
    assert len(keeper._subscribers) == 2, "the window's look and the launch rows; no more"
    page.gpu_row.set_active(False)
    keeper.close(wait=True)
    assert ui_prefs.load().prefs.gpu_acceleration is False and keeper.saves == 1


def test_the_classic_window_opacity_sits_in_its_own_labelled_group(opened: Opened) -> None:
    """settings.toml's `opacity` changes only the classic window, so it says so."""
    row = opened.row("opacity")
    appearance = opened.page._sections["appearance"]
    [classic] = [group for group in appearance.groups if group.title == "Classic interface"]
    assert any(entry.row is row for entry in classic.entries)
    assert "only when Wall-in-One starts the classic interface" in (
        classic.widget.get_description() or ""
    )
    assert row.get_title() == "Window opacity"
    others = [group for group in appearance.groups if group is not classic]
    assert not any(entry.row is row for group in others for entry in group.entries)
    opened.page._set_query("opacity classic")
    assert row.get_visible() and classic.widget.get_visible(), "search still finds it"
    opened.page._set_query("")


# -- Noctalia's palette template -------------------------------------------------------

NOCTALIA = """\
[theme]
source = "wallpaper"
"""


def _noctalia_settings(body: str) -> Path:
    path = paths.noctalia_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


def _noctalia_files() -> dict[str, bytes]:
    directory = paths.noctalia_settings_path().parent
    if not directory.exists():
        return {}
    return {
        entry.name: entry.read_bytes() if entry.is_file() else b"<directory>"
        for entry in sorted(directory.iterdir())
    }


def _template_shown(opened: Opened) -> tuple[str, str, bool, bool]:
    """(pill text, subtitle, button shown, button on) as the row shows them."""
    page = opened.page
    pill = page._template_pill.get_first_child()
    label = ""
    if pill is not None:
        child = pill.get_last_child()
        label = child.get_label() if isinstance(child, Gtk.Label) else ""
    button = page._template_button
    return (
        label,
        page._template_row.get_subtitle() or "",
        button.get_visible(),
        button.get_sensitive(),
    )


def _template_state(opened: Opened) -> str:
    access = opened.page.access
    assert access is not None
    return access.template_status().state


@pytest.fixture
def asked(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(noctalia, "reload_config", lambda: calls.append("reload"))
    monkeypatch.setattr(noctalia, "apply_templates", lambda: calls.append("render"))
    return calls


def test_the_template_row_shows_each_state_and_its_button(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, asked: list[str]
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    shown = _open(monkeypatch)
    try:
        spin_until(lambda: _template_state(shown) != "checking", what="the first read")
        label, subtitle, has_button, _on = _template_shown(shown)
        assert (label, has_button) == ("Can't install", False)
        assert "weren't found" in subtitle

        _noctalia_settings(NOCTALIA)
        access = shown.page.access
        assert access is not None
        access.check_template()
        spin_until(lambda: _template_state(shown) == "missing", what="missing")
        assert _template_shown(shown)[0] == "Not installed"
        assert shown.page._template_button.get_label() == "Install"
        assert _template_shown(shown)[2:] == (True, True)

        monkeypatch.setattr(real_settings, "upgrade_blocked", lambda: "An upgrade is running.")
        access.check_template()
        spin_until(lambda: "upgrade is running" in _template_shown(shown)[1], what="blocked")
        assert _template_shown(shown)[2:] == (True, False), "shown, but off, with the reason"
        before = _noctalia_files()
        shown.page.install_template()
        assert _noctalia_files() == before

        monkeypatch.setattr(real_settings, "upgrade_blocked", lambda: "")
        stale = str(paths.app_state_dir() / "palette.json.tmpl")
        _noctalia_settings(
            NOCTALIA
            + "# >>> wall-in-one palette template (managed) >>>\n"
            + "[theme.templates.user.wall-in-one]\nenabled = true\n"
            + f'input_path = "{stale}"\noutput_path = "{paths.palette_path()}"\n'
            + "# <<< wall-in-one palette template (managed) <<<\n"
        )
        access.check_template()
        spin_until(lambda: _template_state(shown) == "stale", what="stale")
        label, subtitle, has_button, on = _template_shown(shown)
        assert (label, has_button, on) == ("Needs repair", True, True)
        assert "old template file name" in subtitle
        assert shown.page._template_button.get_label() == "Repair"

        shown.page.install_template()  # the button
        shown.wait("the repair", lambda: _template_state(shown) == "installed")
        label, subtitle, has_button, _on = _template_shown(shown)
        assert (label, has_button) == ("Installed", False)
        text = paths.noctalia_settings_path().read_text(encoding="utf-8")
        assert str(template.installed_template_path()) in text and stale not in text
        assert asked == ["reload"], "repairing never renders"
        assert shown.reports[-1].startswith("The palette template was repaired")

        # Installed, not rendered yet: its own Render now, saying what it does.
        page = shown.page
        assert "the next time its colours change" in _template_shown(shown)[1]
        assert page._render_row.get_visible() and page._render_button.get_sensitive()
        assert "all of its templates, not only Wall-in-One's" in (
            page._render_row.get_subtitle() or ""
        )
        monkeypatch.setattr(real_settings, "upgrade_blocked", lambda: "An upgrade is running.")
        access.check_template()
        spin_until(lambda: not page._render_button.get_sensitive(), what="render blocked")
        page.render_templates()
        assert asked == ["reload"], "refused while the upgrade runs"
        monkeypatch.setattr(real_settings, "upgrade_blocked", lambda: "")
        access.check_template()
        spin_until(lambda: page._render_button.get_sensitive(), what="render back")
        page.render_templates()  # the press
        shown.wait("the render", lambda: "render" in asked)
        assert asked == ["reload", "render"]
        assert shown.reports[-1] == "Noctalia was asked to re-render all of its templates"
        paths.palette_path().write_text("{}", encoding="utf-8")  # as Noctalia would
        access.check_template()
        spin_until(lambda: not page._render_row.get_visible(), what="rendered: no offer")
    finally:
        _close(shown)


def test_the_template_button_is_the_only_writer_and_a_refusal_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, asked: list[str]
) -> None:
    sandboxed_runtime(monkeypatch, tmp_path)
    line = "# " + "x" * 1022 + "\n"
    room = template.MAX_NOCTALIA_SETTINGS_BYTES - len(NOCTALIA) - 64
    oversized = NOCTALIA + line * (room // len(line)) + "#" * (room % len(line)) + "\n"
    _noctalia_settings(oversized)
    before = _noctalia_files()
    shown = _open(monkeypatch)
    try:
        spin_until(lambda: _template_state(shown) == "missing", what="the first read")
        idle_until = time.monotonic() + 0.5
        spin_until(lambda: time.monotonic() > idle_until, 5, what="idling")
        assert _noctalia_files() == before, "showing the row writes nothing"

        shown.page.install_template()  # refused: the edit would pass the size limit
        shown.wait("the refusal", lambda: shown.reports)
        assert shown.reports[0].startswith("The palette template wasn't installed: ")
        assert shown.reports[0].endswith(". Noctalia's settings weren't changed.")
        assert "Nothing was changed" not in shown.reports[0]
        assert _noctalia_files() == before, "byte-identical, no backup, no transaction"
        spin_until(lambda: _template_state(shown) == "missing", what="the re-read")
        assert asked == []

        _noctalia_settings(NOCTALIA)
        access = shown.page.access
        assert access is not None
        access.check_template()
        spin_until(lambda: _template_state(shown) == "missing", what="missing again")
        assert len(_noctalia_files()) == 1
        spin_until(lambda: shown.page._template_button.get_sensitive(), what="the button back")
        shown.page.install_template()
        shown.wait("the install", lambda: _template_state(shown) == "installed")
        files = _noctalia_files()
        backups = [name for name in files if name.startswith("settings.toml.bak-wall-in-one-")]
        assert backups and files[backups[0]] == NOCTALIA.encode(), "backed up before the edit"
        assert "wall-in-one" in files["settings.toml"].decode()
        assert asked == ["reload"], "installing never renders"
    finally:
        _close(shown)


# -- palette names: never competing ---------------------------------------------------

_DOCUMENT = {
    "dark": {key: "#202830" for key, _label in palettes.EDITABLE_KEYS},
    "light": {key: "#f0f0f0" for key, _label in palettes.EDITABLE_KEYS},
}


def test_the_new_editor_names_a_copy_uniquely_and_waits_for_a_free_name(
    opened: Opened,
) -> None:
    palettes.write_custom("Evening", _DOCUMENT)
    palettes.write_custom("Evening copy", _DOCUMENT)
    dialog = opened.page.open_palettes()
    assert dialog is not None
    manager = dialog.manager
    spin_until(lambda: manager.palette_list().phase == "ready", what="the catalogue")
    evening = next(i for i in manager.palette_list().items if i.name == "Evening")
    dialog.edit(evening, copy=True)
    spin_until(lambda: isinstance(dialog.nav.get_visible_page(), PaletteEditor), what="editor")
    editor = dialog.nav.get_visible_page()
    assert isinstance(editor, PaletteEditor)
    assert editor.name.get_text() == "Evening copy 2", "never a name already there"
    assert editor.save_button.get_sensitive() and not editor.problem.get_visible()

    for taken, owner in (("evening", "one of your palettes"), ("NORD", "a built-in palette")):
        editor.name.set_text(taken)
        assert not editor.save_button.get_sensitive()
        assert editor.problem.get_visible() and owner in editor.problem.get_label()
        editor.save()  # Save is off; even called, nothing is written
    editor.name.set_text("")
    assert not editor.save_button.get_sensitive() and "needs a name" in editor.problem.get_label()
    assert sorted(path.name for path in palettes.custom_directory().iterdir()) == [
        "Evening copy.json",
        "Evening.json",
    ]

    editor.name.set_text("Evening blue")
    assert editor.save_button.get_sensitive() and not editor.problem.get_visible()
    editor.save()
    opened.wait("the new palette", lambda: palettes.custom_path("Evening blue").is_file())
    dialog.force_close()


def test_the_classic_browser_suggests_a_free_name_and_never_writes_over_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classic's silent overwrite is gone: the shared write path refuses, the editor says so."""
    original = palettes.write_custom("Evening", _DOCUMENT)
    assert original.path is not None
    kept = original.path.read_bytes()
    discovery = palettes.discover()
    catalog = PaletteCatalog(initial=discovery)
    reports: list[str] = []

    def authoring_action_async(work: Any, finish: Any, *, failure: Any = None) -> bool:
        try:
            result = work()
        except Exception as error:
            failure(str(error))
        else:
            finish(result)
        return True

    application = SimpleNamespace(
        session=SimpleNamespace(library=SimpleNamespace(stills=()), cursor=None),
        authoring_action_async=authoring_action_async,
    )
    editors: list[Any] = []
    real_editor = palette_browser._PaletteEditor

    class Recorded(real_editor):  # type: ignore[misc, valid-type]
        def __init__(self, **keywords: Any) -> None:
            super().__init__(**keywords)
            editors.append(self)

    monkeypatch.setattr(palette_browser, "_PaletteEditor", Recorded)
    dialog = palette_browser.PaletteBrowserDialog(cast(Any, application), palette_catalog=catalog)
    monkeypatch.setattr(dialog, "report", reports.append)
    root = Gtk.Window()
    root.present()
    dialog.present(root)
    try:
        evening = discovery.find(palettes.Origin.CUSTOM, "Evening")
        assert evening is not None
        dialog._open_editor(evening, in_place=False)
        editor = editors[-1]
        assert editor._name.get_text() == "Evening copy"
        editor._name.set_text("EVENING")
        editor._on_save(editor._save)
        assert original.path.read_bytes() == kept, "never written over"
        assert reports == ["“EVENING” is already one of your palettes; choose another name"]
        assert editor._name.has_css_class("error") and editor._save.get_sensitive()
        assert sorted(p.name for p in palettes.custom_directory().iterdir()) == ["Evening.json"]
    finally:
        for editor in editors:
            editor.force_close()
        dialog.force_close()
        root.destroy()
